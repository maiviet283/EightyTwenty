"""
Maintenance that runs on a clock: resend failed emails, remind tables booked for tomorrow,
clear out expired temporary rows.

Render's free plan has no cron, but the API is pinged around the clock. So the web process runs
the jobs itself (JobsMiddleware): at most once a minute per process it asks the database whether
the jobs are due, and the one request that claims the slot (an atomic UPDATE on a JobClock row,
so two workers never both win) starts them in a background thread every JOB_INTERVAL. The same
jobs can still be triggered by hand: POST /api/v1/jobs/{retry-emails,booking-reminders,cleanup}.
"""

import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections
from django.utils import timezone

from .models import JobClock, RateLimitBucket

logger = logging.getLogger(__name__)

JOB_INTERVAL = timedelta(minutes=15)
CHECK_EVERY_SECONDS = 60
CLOCK = "maintenance"
_last_check = None  # time.monotonic() can be under a minute on a freshly booted machine


def retry_emails():
    from apps.notifications.services import retry_emails

    return retry_emails(limit=10)


def booking_reminders():
    from apps.bookings.services import send_reminders

    return send_reminders()


def cleanup():
    from apps.notifications.models import EmailMessage
    from apps.pos.models import CommandReceipt

    call_command("flushexpiredtokens")
    now = timezone.now()
    count, _ = RateLimitBucket.objects.filter(expires_at__lt=now - timedelta(days=1)).delete()
    # Till command receipts only need to outlive the till's retries.
    count += CommandReceipt.objects.filter(created_at__lt=now - timedelta(days=30)).delete()[0]
    # Account emails contain short-lived reset/verification links; purge their body.
    count += (
        EmailMessage.objects.filter(sent_at__lt=now - timedelta(days=7))
        .exclude(body="")
        .update(body="")
    )
    return count


JOBS = {"retry-emails": retry_emails, "booking-reminders": booking_reminders, "cleanup": cleanup}


def run_all():
    """Every job once; one failing job never stops the others. Returns {job: count | None}."""
    results = {}
    for name, job in JOBS.items():
        try:
            results[name] = job()
        except Exception:
            logger.exception("Scheduled job %s failed", name)
            results[name] = None
    return results


def claim(now=None):
    """True for exactly one caller per JOB_INTERVAL, across processes."""
    now = now or timezone.now()
    try:
        JobClock.objects.get_or_create(name=CLOCK, defaults={"last_run_at": now - 2 * JOB_INTERVAL})
    except IntegrityError:
        pass  # another worker created it first
    return bool(
        JobClock.objects.filter(name=CLOCK, last_run_at__lte=now - JOB_INTERVAL).update(
            last_run_at=now
        )
    )


def _run_in_thread():
    try:
        results = run_all()
        logger.info("Scheduled jobs: %s", results)
    finally:
        close_old_connections()


def kick():
    """Called after each request: start the jobs in the background when they are due."""
    global _last_check
    if not settings.JOBS_IN_PROCESS:
        return
    now = time.monotonic()
    if _last_check is not None and now - _last_check < CHECK_EVERY_SECONDS:
        return
    _last_check = now
    try:
        due = claim()
    except Exception:
        logger.exception("Could not check the job clock")
        return
    if due:
        threading.Thread(target=_run_in_thread, daemon=True, name="bos-jobs").start()


class JobsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        kick()
        return response

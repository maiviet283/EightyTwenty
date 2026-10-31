"""Scheduled jobs run inside the web process (Render free has no cron)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core import jobs
from apps.core.models import JobClock

pytestmark = pytest.mark.django_db


def test_only_one_claim_per_interval():
    now = timezone.now()
    assert jobs.claim(now) is True
    assert jobs.claim(now) is False  # a second worker in the same window
    assert jobs.claim(now + timedelta(minutes=14)) is False
    assert jobs.claim(now + timedelta(minutes=15)) is True
    assert JobClock.objects.get().last_run_at == now + timedelta(minutes=15)


def test_one_failing_job_does_not_stop_the_others():
    def broken():
        raise RuntimeError("down")

    with patch.dict(jobs.JOBS, {"retry-emails": broken}):
        results = jobs.run_all()
    assert results["retry-emails"] is None
    assert results["booking-reminders"] == 0 and isinstance(results["cleanup"], int)


def test_requests_start_the_jobs_when_due(settings, monkeypatch):
    settings.JOBS_IN_PROCESS = True
    monkeypatch.setattr(jobs, "_last_check", None)
    started = []

    class Thread:
        def __init__(self, target, **kwargs):
            started.append(target)

        def start(self):
            pass

    with patch.object(jobs.threading, "Thread", Thread):
        APIClient().get("/health")
        APIClient().get("/health")  # within a minute: not even checked
        monkeypatch.setattr(jobs, "_last_check", None)
        APIClient().get("/health")  # checked again, but the 15 minutes are not up
    assert started == [jobs._run_in_thread]


def test_first_request_checks_even_right_after_boot(settings, monkeypatch):
    settings.JOBS_IN_PROCESS = True
    monkeypatch.setattr(jobs, "_last_check", None)
    monkeypatch.setattr(jobs.time, "monotonic", lambda: 5.0)  # machine up for 5 seconds
    with patch.object(jobs, "claim", return_value=False) as claim:
        APIClient().get("/health")
    claim.assert_called_once()


def test_off_by_default_in_tests_and_dev(settings, monkeypatch):
    monkeypatch.setattr(jobs, "_last_check", None)
    with patch.object(jobs, "claim") as claim:
        APIClient().get("/health")
    claim.assert_not_called()
    assert settings.JOBS_IN_PROCESS is False


def test_job_endpoints_still_work_by_hand(settings):
    client = APIClient()
    for job in ("retry-emails", "booking-reminders", "cleanup"):
        assert client.post(f"/api/v1/jobs/{job}").status_code == 403
        response = client.post(f"/api/v1/jobs/{job}", HTTP_X_JOB_TOKEN=settings.JOB_TOKEN)
        assert response.status_code == 200 and "processed" in response.json()

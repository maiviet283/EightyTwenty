"""
Till rules: orders rung up at the counter, tenders, refunds/voids/cancellations, kitchen progress,
tables, cash shifts and till records; customers, coffee cards, sold-out switches, reservations and
catering enquiries edited at the till (the same records the website, the app and BOS use). The till works offline and sends each change later as a
command (POS src/services/commands.ts), so a sale that already happened is recorded even when
its prices no longer match the menu; the differences are noted on the order for a manager.
"""

import ipaddress
import json
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.throttling import BaseThrottle

from apps.accounts.models import CustomerProfile, User
from apps.bookings.models import Booking
from apps.bookings.services import update_booking
from apps.core.models import AuditLog
from apps.core.permissions import scoped_locations
from apps.core.services import audit, snapshot
from apps.enquiries.models import CateringPackage, Enquiry
from apps.locations.models import Location
from apps.loyalty import services as loyalty
from apps.loyalty.models import STAMPS_PER_REWARD, GiftCard, GiftCardActivity
from apps.menu.catalog import menu_items
from apps.menu.models import Category, LocationModifierAvailability, MenuItem
from apps.menu.services import set_availability
from apps.mobile.errors import AppError, invalid, not_found
from apps.orders import services as order_services
from apps.orders.models import Order, OrderAdjustment, OrderLine, OrderPayment

from .models import CashShift, Promotion, TillSettings, TillTransaction
from .staff_sessions import require_staff_permission

# POS OrderStatus ↔ stored status. "new" is confirmed (accepted) for till orders; an app order
# waiting for the café is "received" and shows on the till as new + accepted: false.
FROM_TILL = {
    "new": "confirmed",
    "preparing": "preparing",
    "ready": "ready",
    "completed": "completed",
    "cancelled": "cancelled",
    "refunded": "refunded",
    "failed": "failed",
}
TO_TILL = {
    "received": "new",
    "confirmed": "new",
    "preparing": "preparing",
    "ready": "ready",
    "completed": "completed",
    "declined": "cancelled",
    "cancelled": "cancelled",
    "refunded": "refunded",
    "failed": "failed",
}
METHOD_FROM_TILL = {
    "dinein": "dine_in",
    "pickup": "pickup",
    "takeaway": "takeaway",
    "delivery": "delivery",
}
FINAL = {"completed", "declined", "cancelled", "refunded"}
PREPARABLE = {"confirmed", "preparing", "ready"}
LIVE = ["received", "confirmed", "preparing", "ready", "failed"]
TOTAL_TOLERANCE_CENTS = 1  # the till rounds in dollars, the server in cents


def cents(value):
    """AUD dollars (as the till sends them) → integer cents, half-up."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(value):
    return f"${value / 100:,.2f}"


def terminal_of(request):
    return request.headers.get("X-Terminal-Id", "").strip()[:60]


def till_locations(user):
    return scoped_locations(user).filter(is_published=True).order_by("name")


def location_for(user, slug):
    location = till_locations(user).filter(slug=slug).first()
    if location is None:
        raise not_found("That café isn’t available on this till.")
    return location


def default_location(user):
    location = till_locations(user).exclude(status="tbc").first()
    if location is None:
        raise not_found("This account isn’t assigned to a café yet.")
    return location


def orders_for(user):
    return Order.objects.filter(location__in=till_locations(user))


def order_for(user, number, lock=False):
    qs = orders_for(user)
    if lock:
        qs = qs.select_for_update(of=("self",))
    order = qs.filter(number=number).select_related("location", "customer").first()
    if order is None:
        raise not_found(f"Order {number} was not found.")
    return order


def note_history(order, text):
    if text:
        order.history = [*order.history, {"at": timezone.localtime().isoformat(), "what": text}]


def touch(order, request, *fields):
    order.updated_by = request.user
    order.save(update_fields=[*fields, "updated_by", "updated_at"])


# ---------------------------------------------------------------- orders


def menu_check(location, lines):
    """Compare till prices with the menu. Returns (menu items by slug, review notes)."""
    entries = {
        e.item.slug: e for e in menu_items(location, slugs={line["itemId"] for line in lines})
    }
    notes = []
    for line in lines:
        entry = entries.get(line["itemId"])
        if entry is None:
            notes.append(f"{line['name']} is not on the {location.name} menu.")
            continue
        unit, groups = entry.price_cents, {g.code: dict(g.options) for g in entry.groups}
        for code, labels in (line.get("sel") or {}).items():
            for label in labels:
                if label not in groups.get(code, {}):
                    notes.append(f"{line['name']}: option “{label}” is not on the menu.")
                else:
                    unit += groups[code][label]
        price = cents(line["price"])
        if price != unit:
            notes.append(f"{line['name']} rung up at {money(price)} (menu {money(unit)}).")
        elif not entry.available:
            notes.append(f"{line['name']} was sold while marked sold out.")
    return entries, notes


def till_totals(data, location):
    subtotal = sum(cents(line["price"]) * line["qty"] for line in data["lines"])
    discount = sum(cents(row["amount"]) for row in data.get("discounts") or [])
    if discount > subtotal:
        raise invalid("The discount is more than the order.", discounts="invalid")
    delivery_rate, _ = order_services.order_rules()
    delivery = (
        order_services.apply_rate(subtotal - discount, delivery_rate)
        if data["type"] == "delivery"
        else 0
    )
    total = cents(data["total"])
    expected = subtotal - discount + delivery
    if abs(total - expected) > TOTAL_TOLERANCE_CENTS:
        raise invalid(
            f"The order total {money(total)} does not add up: lines {money(subtotal)} − "
            f"discounts {money(discount)} + delivery {money(delivery)} = {money(expected)}.",
            total="invalid",
        )
    return subtotal, discount, delivery, total


PROMO_WORD = re.compile(r"[A-Za-z0-9]{3,30}")


def promo_notes(discounts, location, subtotal, day):
    """Promo codes on a till order checked against BOS: problems are noted, never refused."""
    promos = None
    notes = []
    for row in discounts:
        if promos is None:
            promos = {p.code.upper(): p for p in Promotion.objects.select_related("location")}
        code = (row.get("code") or "").strip().upper()
        if not code:
            words = {word.upper() for word in PROMO_WORD.findall(row.get("label") or "")}
            code = next((word for word in sorted(words) if word in promos), "")
            if not code:
                continue  # a staff discount or the free coffee, not a promo code
        promo = promos.get(code)
        problem = promo_problem(promo, location, day)
        if problem:
            notes.append(f"Promo code {code} {problem}.")
            continue
        if promo.kind == "pct":
            allowed = (subtotal * (promo.percent or 0) + 50) // 100
        else:
            allowed = min(promo.amount_cents or 0, subtotal)
        taken = cents(row["amount"])
        if taken > allowed + TOTAL_TOLERANCE_CENTS:
            notes.append(f"Promo code {code} took {money(taken)} off; it allows {money(allowed)}.")
    return notes


def promo_problem(promo, location, day):
    if promo is None:
        return "is not in BOS"
    if not promo.is_active:
        return "is switched off in BOS"
    if promo.location_id and promo.location_id != location.pk:
        return f"is for {promo.location.name} only"
    if promo.starts_on and day < promo.starts_on:
        return f"only starts on {date_format(promo.starts_on, 'j M Y')}"
    if promo.ends_on and day > promo.ends_on:
        return f"ended on {date_format(promo.ends_on, 'j M Y')}"
    return ""


def customer_of(customer_id):
    if not customer_id:
        return None
    try:
        return User.objects.filter(pk=customer_id, role="customer", is_active=True).first()
    except (ValueError, TypeError, ValidationError):
        return None


def till_gift_card(card_id):
    """The gift card a `gift` payment is taken from, locked until the payment is saved."""
    if not card_id:
        raise invalid("Choose the customer's gift card.", giftCardId="required")
    try:
        card = (
            GiftCard.objects.select_for_update()
            .filter(pk=card_id, user__role="customer", user__is_active=True)
            .first()
        )
    except (ValueError, TypeError, ValidationError):
        card = None
    if card is None:
        raise invalid("That gift card was not found.", giftCardId="invalid")
    return card


def restore_gift_cards(order, adjustment, refundable):
    """Puts a refund back on the gift cards it was paid from (once per adjustment and payment)."""
    payments = {
        payment.reference: payment
        for payment in order.payments.filter(gift_card__isnull=False).select_related("gift_card")
    }
    if not payments or not adjustment.amount_cents:
        return
    rows = adjustment.allocation
    if not rows:
        if adjustment.amount_cents < refundable:
            raise invalid(
                "A partial refund on a gift card payment needs the payment allocation.",
                alloc="required",
            )
        used = {}
        for previous in order.adjustments.exclude(pk=adjustment.pk):
            for row in previous.allocation:
                used[row.get("ref")] = used.get(row.get("ref"), 0) + (row.get("amount") or 0)
        rows = [
            {"ref": ref, "amount": payment.amount_cents - used.get(ref, 0)}
            for ref, payment in payments.items()
        ]
    for row in rows:
        payment = payments.get(row.get("ref"))
        amount = row.get("amount") or 0
        if payment is None or amount <= 0:
            continue
        card = GiftCard.objects.select_for_update().get(pk=payment.gift_card_id)
        card.balance_cents += amount
        card.save(update_fields=["balance_cents", "updated_at"])
        GiftCardActivity.objects.create(
            card=card,
            kind="added",
            description=f"Refunded from order {order.number}",
            amount_cents=amount,
            idempotency_key=f"pos-refund:{adjustment.pk}:{payment.pk}",
        )


@transaction.atomic
def create_order(request, data):
    user = request.user
    location = location_for(user, data["cafe"])

    managed_loyalty = data.get("serverLoyalty", False)
    if managed_loyalty:
        require_staff_permission(request, "pos", location)

    customer = customer_of(data.get("customerId"))
    if managed_loyalty and data.get("customerId") and customer is None:
        raise invalid("The selected customer is no longer available.", customerId="invalid")

    if data["channel"] != "pos":
        raise invalid("Only till orders can be created here.", channel="invalid")
    if Order.objects.filter(number=data["id"]).exists():
        raise AppError(
            "CONFLICT",
            f"Order {data['id']} already exists. Each till must number its orders uniquely.",
            409,
        )
    subtotal, discount, delivery, total = till_totals(data, location)
    entries, notes = menu_check(location, data["lines"])
    status = FROM_TILL[data["status"]]
    if status not in {"confirmed", "preparing", "ready", "completed"}:
        raise invalid("A new till order starts as new, preparing, ready or completed.")
    placed = data.get("placedAt") or timezone.now()
    notes += promo_notes(
        data.get("discounts") or [], location, subtotal, timezone.localdate(placed)
    )
    table = (data.get("table") or "").strip()
    order = Order.objects.create(
        number=data["id"],
        channel="pos",
        customer=customer,
        location=location,
        method=METHOD_FROM_TILL[data["type"]],
        status=status,
        table_number=table[:20],
        tables=data.get("tables") or ([table] if table else []),
        subtotal_cents=subtotal,
        delivery_cents=delivery,
        discount_cents=discount,
        discounts=data.get("discounts") or [],
        total_cents=total,
        reward_redeemed=data.get("rewardUsed", False),
        placed_at=placed,
        note=data.get("note") or "",
        staff_name=data.get("staff") or "",
        terminal_id=terminal_of(request),
        history=data.get("history") or [],
        review_note="\n".join(notes),
        handled_by=user,
        handled_at=timezone.now(),
        created_by=user,
        updated_by=user,
        pos_loyalty_managed=data.get("serverLoyalty", False),
    )
    for position, line in enumerate(data["lines"]):
        entry = entries.get(line["itemId"])
        sel = line.get("sel") or {}
        OrderLine.objects.create(
            order=order,
            menu_item=entry.item if entry else None,
            item_slug=line["itemId"],
            name=line["name"],
            qty=line["qty"],
            unit_price_cents=cents(line["price"]),
            selections=sel,
            modifiers_label=", ".join(label for labels in sel.values() for label in labels),
            note=line.get("note") or "",
            position=position,
            key=line["key"],
            adjust=line.get("adjust") or [],
            prep=line.get("prep") or "",
        )
    if order.pos_loyalty_managed and order.reward_redeemed:
        order = reserve_pos_order_reward(request, order)

    for payment in data.get("payments") or []:
        order = record_payment(request, order, payment, save=False)

    return settle_pos_order_loyalty(request, order)


def till_total_cents(order):
    """The order total as the till sees it: an app order's gift card share counts, its surcharge not."""
    if order.channel == "app":
        return order.total_cents + order.gift_card_cents - order.surcharge_cents
    return order.total_cents


def paid_cents(order):
    if order.channel == "app":  # paid in full in the app (presenters.app_payments)
        return till_total_cents(order)
    return order.payments.aggregate(total=Sum("amount_cents"))["total"] or 0


def refunded_cents(order):
    return order.adjustments.aggregate(total=Sum("amount_cents"))["total"] or 0


def resolve_cash_shift(request, location, reference):
    if reference is None:
        return None

    shift = (
        CashShift.objects.select_for_update()
        .filter(
            reference=reference,
            location=location,
            terminal_id=terminal_of(request),
        )
        .first()
    )

    if shift is None:
        raise AppError(
            "CONFLICT", "This cash shift does not belong to the order location and terminal.", 409
        )
    if shift.status != "open":
        raise AppError("CONFLICT", "This cash shift is already closed.", 409)

    return shift


@transaction.atomic
def record_payment(request, order, data, save=True):
    """Records a tender already taken (cash counted, card approved on the terminal)."""
    staff = require_staff_permission(request, "pos", order.location)
    actor = staff.get_full_name().strip() or staff.email
    data = {**data, "by": actor}
    gift = None
    if data["method"] == "gift":
        require_staff_permission(request, "gift.redeem", order.location)
        gift = till_gift_card(data.get("giftCardId"))
        if data.get("giftLast4") and data["giftLast4"] != gift.last4:
            raise invalid("The gift card number doesn’t match this card.", giftLast4="invalid")
        data["giftLast4"] = gift.last4
    existing = order.payments.filter(reference=data["ref"]).first()

    if existing is not None:
        expected = {
            "method": data["method"],
            "amount_cents": data["amount"],
            "tip_cents": data.get("tip") or 0,
            "surcharge_cents": data.get("surcharge") or 0,
            "gift_last4": data.get("giftLast4") or "",
            "gift_card_id": gift.pk if gift else None,
            "split_mode": data.get("mode") or "",
            "items": data.get("items") or {},
            "cash_paid_cents": data.get("cashPaid"),
            "rounding_cents": data.get("rounding") or 0,
            "change_cents": data.get("change"),
            "staff_name": data.get("by") or "",
            "taken_at": data.get("at") or "",
            "terminal_id": terminal_of(request),
        }

        saved_shift = existing.shift.reference if existing.shift_id else None
        shift_changed = saved_shift != data.get("shiftId")

        if shift_changed or any(
            getattr(existing, field) != value for field, value in expected.items()
        ):
            raise AppError(
                "CONFLICT",
                "This payment reference was already used with different payment details.",
                409,
            )

        return order
    if order.status in {"declined", "cancelled", "refunded"}:
        raise AppError(
            "CONFLICT", f"Order {order.number} is {order.status}; no payment taken.", 409
        )
    due = till_total_cents(order) - paid_cents(order)
    if data["amount"] > due:
        raise invalid(
            f"{money(data['amount'])} is more than the {money(max(0, due))} due on {order.number}.",
            amount="invalid",
        )

    shift = None
    if data.get("shiftId"):
        shift = resolve_cash_shift(request, order.location, data.get("shiftId"))

        if shift is None:
            raise AppError(
                "CONFLICT",
                "This cash shift does not belong to the order location and terminal.",
                409,
            )
        if shift.status != "open":
            raise AppError("CONFLICT", "This cash shift is already closed.", 409)
    if gift is not None and gift.balance_cents < data["amount"]:
        raise AppError(
            "CONFLICT",
            f"Gift card ·· {gift.last4} has {money(gift.balance_cents)} left, "
            f"less than the {money(data['amount'])} asked.",
            409,
        )
    payment = OrderPayment.objects.create(
        order=order,
        shift=shift,
        method=data["method"],
        amount_cents=data["amount"],
        tip_cents=data.get("tip") or 0,
        surcharge_cents=data.get("surcharge") or 0,
        reference=data["ref"],
        gift_last4=data.get("giftLast4") or "",
        gift_card=gift,
        split_mode=data.get("mode") or "",
        items=data.get("items") or {},
        cash_paid_cents=data.get("cashPaid"),
        rounding_cents=data.get("rounding") or 0,
        change_cents=data.get("change"),
        staff_name=data.get("by") or "",
        taken_at=data.get("at") or "",
        terminal_id=terminal_of(request),
    )
    if gift is not None:
        gift.balance_cents -= payment.amount_cents
        gift.save(update_fields=["balance_cents", "updated_at"])
        GiftCardActivity.objects.create(
            card=gift,
            kind="spent",
            description=f"Used at {order.location.name} · order {order.number}",
            amount_cents=payment.amount_cents,
            idempotency_key=f"pos-pay:{payment.pk}",
        )
    if data.get("surcharge"):
        order.surcharge_cents += data["surcharge"]
    if save:
        touch(order, request, "surcharge_cents")
    elif data.get("surcharge"):
        order.save(update_fields=["surcharge_cents"])
    return settle_pos_order_loyalty(request, order)


def change_status(request, order, status, text):
    """Stores a new status, notes it, and tells an app customer what happened."""
    previous = order.status
    order.status = status
    order.handled_by = request.user
    order.handled_at = timezone.now()
    note_history(order, text)
    touch(order, request, "status", "handled_by", "handled_at", "history")
    if previous != status:
        if order.channel == "app":
            audit(request, order, {"status": previous}, "pos_status", {"status": status})
        order_services.after_status_change(order, previous)


def set_status(request, order, till_status, text):
    if till_status not in {"new", "preparing", "ready", "completed"}:
        raise invalid("Use adjustments to cancel or refund an order.", status="invalid")
    target = FROM_TILL[till_status]
    if order.status == target or (order.status == "received" and target == "confirmed"):
        note_history(order, text)
        touch(order, request, "history")
        return order
    if order.status in FINAL or order.status == "failed":
        raise AppError("CONFLICT", f"Order {order.number} is already {order.status}.", 409)
    change_status(request, order, target, text)
    return order


def accept(request, order):
    if order.status in FINAL or order.status == "failed":
        raise AppError("CONFLICT", f"Order {order.number} is already {order.status}.", 409)
    if order.status in {"received", "confirmed"}:
        change_status(request, order, "preparing", "Accepted at the café")
    return order


def set_prep(request, order, data):
    if order.status not in PREPARABLE:
        state = "waiting to be accepted" if order.status == "received" else order.status
        raise AppError("CONFLICT", f"Order {order.number} is {state}; no change made.", 409)
    if data["status"] not in {"new", "preparing", "ready"}:
        raise invalid("Kitchen progress sets new, preparing or ready.", status="invalid")
    lines = {line.key or str(line.pk): line for line in order.lines.all()}
    unknown = [row["key"] for row in data["lines"] if row["key"] not in lines]
    if unknown:
        raise invalid(f"Order {order.number} has no line {unknown[0]}.", lines="invalid")
    for row in data["lines"]:
        line = lines[row["key"]]
        if line.prep != row["prep"]:
            line.prep = row["prep"]
            line.save(update_fields=["prep", "updated_at"])
    target = FROM_TILL[data["status"]]
    if target != order.status:
        change_status(request, order, target, data.get("note") or "")
    else:
        note_history(order, data.get("note") or "")
        touch(order, request, "history")
    return order


def validate_refund_allocations(order, change):
    allocations = change.get("alloc") or []
    if not allocations:
        return

    payments = {
        payment.reference: {
            "method": payment.method,
            "amount": payment.amount_cents,
            "giftLast4": payment.gift_last4 or None,
        }
        for payment in order.payments.all()
    }

    if order.channel == "app":
        charged = order.total_cents - order.surcharge_cents
        if charged > 0:
            payments[f"app-{order.pk}"] = {
                "method": "wallet" if order.payment_choice == "apple_pay" else "card",
                "amount": charged,
                "giftLast4": None,
            }
        if order.gift_card_cents:
            payments[f"app-gift-{order.pk}"] = {
                "method": "gift",
                "amount": order.gift_card_cents,
                "giftLast4": None,
            }

    used = {}
    for previous in order.adjustments.all():
        if previous.amount_cents and not previous.allocation:
            raise invalid(
                "An earlier refund has no payment allocation. Reconcile it before issuing another allocated refund.",
                alloc="invalid",
            )

        for row in previous.allocation:
            reference = row.get("ref")
            amount = row.get("amount")
            if reference not in payments or type(amount) is not int or amount < 0:
                raise invalid(
                    "An earlier refund has an invalid allocation. Reconcile it before continuing.",
                    alloc="invalid",
                )
            used[reference] = used.get(reference, 0) + amount

    requested = {}
    for row in allocations:
        reference = row["ref"]
        payment = payments.get(reference)

        if payment is None:
            raise invalid("The referenced payment does not belong to this order.", alloc="invalid")
        if row["method"] != payment["method"]:
            raise invalid("The refund method must match the original payment.", alloc="invalid")
        if (row.get("giftLast4") or None) != payment["giftLast4"]:
            raise invalid("The gift card details must match the original payment.", alloc="invalid")

        requested[reference] = requested.get(reference, 0) + row["amount"]
        remaining = payment["amount"] - used.get(reference, 0)
        if requested[reference] > remaining:
            raise invalid(
                "The refund exceeds the remaining amount of the original payment.", alloc="invalid"
            )


@transaction.atomic
def adjust(request, order, data):
    staff = require_staff_permission(request, "orders.refund", order.location)
    actor = staff.get_full_name().strip() or staff.email
    change = {**data["adjustment"], "by": actor, "approver": actor}
    existing = order.adjustments.filter(key=change["key"]).first()

    if existing is not None:
        expected = {
            "mode": change["mode"],
            "amount_cents": change["amount"],
            "allocation": change.get("alloc") or [],
            "items": change.get("items") or {},
            "reason": change.get("reason") or "",
            "staff_name": change.get("by") or "",
            "approver_name": change.get("approver") or "",
            "made_at": change.get("at") or "",
            "terminal_id": terminal_of(request),
        }
        saved_shift = existing.shift.reference if existing.shift_id else None
        changed = any(getattr(existing, field) != value for field, value in expected.items())

        if changed or saved_shift != change.get("shiftId"):
            raise AppError(
                "CONFLICT",
                "This adjustment reference was already used with different details.",
                409,
            )
        return order
    status = FROM_TILL.get(data["status"])
    if status is None or status in {"failed"}:
        raise invalid("That status can’t follow a refund, void or cancellation.", status="invalid")
    if order.status in {"declined", "cancelled", "refunded"}:
        raise AppError("CONFLICT", f"Order {order.number} is already {order.status}.", 409)

    refundable = paid_cents(order) - refunded_cents(order)
    if change["amount"] > refundable:
        raise invalid(
            f"{money(change['amount'])} is more than the {money(refundable)} that can be returned.",
            amount="invalid",
        )
    total = cents(data["total"])
    if total > till_total_cents(order):
        raise invalid(
            "A refund, void or cancellation can’t raise the order total.", total="invalid"
        )

    if (
        order.channel == "pos"
        and order.pos_loyalty_managed
        and order.pos_loyalty_processed_at is not None
        and order.reward_redeemed
        and change["mode"] in {"refund", "void"}
        and change["amount"] < refundable
    ):
        raise AppError(
            "CONFLICT", "Partial refunds for orders using a reward need manager review.", 409
        )

    validate_pos_loyalty_adjustment(order, data)
    validate_refund_allocations(order, change)
    shift = resolve_cash_shift(request, order.location, change.get("shiftId"))
    lines = {line.key or str(line.pk): line for line in order.lines.all()}
    kept = {row["key"]: row["qty"] for row in data["lines"]}
    unknown = [key for key in kept if key not in lines]
    if unknown:
        raise invalid(f"Order {order.number} has no line {unknown[0]}.", lines="invalid")
    for key, line in lines.items():
        qty = kept.get(key, 0)
        if qty > line.qty:
            raise invalid(f"{line.name}: a void can only lower the quantity.", lines="invalid")
        if qty != line.qty:
            line.qty = qty
            line.save(update_fields=["qty", "updated_at"])
    before = snapshot(order)
    adjustment = OrderAdjustment.objects.create(
        order=order,
        shift=shift,
        key=change["key"],
        mode=change["mode"],
        amount_cents=change["amount"],
        allocation=change.get("alloc") or [],
        items=change.get("items") or {},
        reason=change.get("reason") or "",
        staff_name=change.get("by") or "",
        approver_name=change.get("approver") or "",
        made_at=change.get("at") or "",
        terminal_id=terminal_of(request),
    )
    restore_gift_cards(order, adjustment, refundable)
    if (
        order.channel == "pos"
        and order.pos_loyalty_managed
        and order.pos_loyalty_processed_at is not None
        and change["mode"] in {"refund", "void"}
        and status not in {"refunded", "cancelled", "declined"}
    ):
        order = reverse_partial_pos_loyalty(request, order, adjustment)
    if order.channel != "app":  # an app order keeps what the app charged (refunded on cancel)
        order.total_cents = total
        touch(order, request, "total_cents")
    label = {"cancel": "Cancelled", "void": "Items voided", "refund": "Refunded"}[change["mode"]]
    text = f"{label} at the till · {change.get('reason') or 'no reason given'}"
    if status == "cancelled" and order.channel == "app" and order.status == "received":
        status = "declined"  # the app shows a café refusing an order as declined
    if status != order.status:
        change_status(request, order, status, text)
    else:
        note_history(order, text)
        touch(order, request, "history")
    if (
        order.channel == "pos"
        and order.pos_loyalty_managed
        and order.status in {"cancelled", "declined"}
        and order.pos_reward_reserved_at is not None
        and order.pos_loyalty_processed_at is None
    ):
        order = return_unpaid_pos_order_reward(request, order)
    if (
        order.channel == "pos"
        and order.pos_loyalty_managed
        and order.pos_loyalty_processed_at is not None
        and order.status in {"refunded", "cancelled"}
        and refunded_cents(order) >= paid_cents(order)
    ):
        order = reverse_fully_refunded_pos_loyalty(request, order)
    audit(request, order, before, f"pos_{change['mode']}")
    return order


def set_tables(request, order, data):
    table = (data.get("table") or "").strip()[:20]
    order.table_number = table
    order.tables = [str(t)[:20] for t in data.get("tables") or []]
    if data.get("cleared") is not None:
        order.table_cleared = data["cleared"]
    touch(order, request, "table_number", "tables", "table_cleared")
    return order


def changed_since(user, location, since):
    """Orders at a café changed since a cursor, oldest change first, and the next cursor."""
    qs = orders_for(user).filter(location=location)
    if since is None:
        today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        qs = qs.filter(updated_at__gte=today) | qs.filter(status__in=LIVE)
    else:
        # A little overlap so a change committed just after the last poll is never missed;
        # the till merges by order id, so repeats are harmless.
        qs = qs.filter(updated_at__gte=since - timedelta(seconds=10))
    rows = list(qs.distinct().order_by("updated_at")[:500])
    cursor = rows[-1].updated_at if rows else (since or timezone.now())
    return rows, cursor


def parse_since(value):
    if not value:
        return None
    parsed = parse_datetime(value.replace(" ", "+"))
    if parsed is None:
        raise invalid("`since` must be the cursor from the previous response.", since="invalid")
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


# ---------------------------------------------------------------- cash & records


def open_shift(request, data):
    location = location_for(request.user, data["cafe"])
    staff = require_staff_permission(request, "pos", location)
    actor = staff.get_full_name().strip() or staff.email
    existing = CashShift.objects.filter(reference=data["id"]).first()
    if existing:
        raise AppError("CONFLICT", f"Shift {data['id']} already exists.", 409)
    terminal = terminal_of(request)
    if (
        terminal
        and CashShift.objects.filter(
            location=location, terminal_id=terminal, status="open"
        ).exists()
    ):
        raise AppError("CONFLICT", "A shift is already open on this till. Close it first.", 409)
    return CashShift.objects.create(
        reference=data["id"],
        location=location,
        terminal_id=terminal,
        staff_ref=str(staff.pk),
        staff_name=actor,
        opened_label=data.get("openedAt") or "",
        float_cents=data["float"],
        created_by=staff,
        updated_by=staff,
    )


def shift_for(user, reference, lock=False):
    qs = CashShift.objects.filter(reference=reference, location__in=till_locations(user))
    shift = (qs.select_for_update() if lock else qs).first()
    if shift is None:
        raise not_found(f"Shift {reference} was not found.")
    return shift


def require_shift_terminal(request, shift):
    terminal = terminal_of(request)
    if not terminal or terminal != shift.terminal_id:
        raise AppError("CONFLICT", "This cash shift belongs to another terminal.", 409)


def cash_move(request, shift, move):
    require_shift_terminal(request, shift)
    staff = require_staff_permission(request, "cash.out", shift.location)
    actor = staff.get_full_name().strip() or staff.email
    move = {**move, "by": actor, "approver": actor}
    existing = next((row for row in shift.moves if row.get("id") == move["id"]), None)

    if existing is not None:
        if existing != move:
            raise AppError(
                "CONFLICT",
                "This cash movement reference was already used with different details.",
                409,
            )
        return shift

    if shift.status != "open":
        raise AppError("CONFLICT", "This shift is already closed.", 409)

    shift.moves = [*shift.moves, move]
    shift.updated_by = staff
    shift.save(update_fields=["moves", "updated_by", "updated_at"])
    return shift


def expected_cash(shift_float, sales, refunds, moves):
    moved = sum(m["amount"] if m["type"] == "in" else -m["amount"] for m in moves)
    return shift_float + sales - refunds + moved


def cash_shift_totals(shift):
    sales = sum(
        payment.amount_cents + payment.rounding_cents
        for payment in shift.payments.filter(method="cash")
    )
    refunds = sum(
        row["amount"]
        for adjustment in shift.adjustments.all()
        for row in adjustment.allocation
        if row.get("method") == "cash"
    )
    return sales, refunds


def close_shift(request, shift, data):
    require_shift_terminal(request, shift)
    staff = require_staff_permission(request, "cash.out", shift.location)
    actor = staff.get_full_name().strip() or staff.email
    if shift.status == "closed":
        if shift.counted_cents == data["counted"]:
            return shift
        raise AppError("CONFLICT", "This shift is already closed.", 409)

    sales, refunds = cash_shift_totals(shift)
    if data["cashSales"] != sales or data["cashRefunds"] != refunds:
        raise invalid(
            f"Cash totals do not match server records: sales {money(sales)}, refunds {money(refunds)}. Sync all transactions before closing.",
            cashSales="invalid",
            cashRefunds="invalid",
        )

    known = {row.get("id"): row for row in shift.moves}
    moves = list(shift.moves)

    for move in data.get("moves") or []:
        existing = known.get(move["id"])
        if existing is None:
            raise AppError("CONFLICT", "Sync all cash movements before closing this shift.", 409)

        fields = ("type", "amount", "reason", "at")
        if any(existing.get(field) != move.get(field) for field in fields):
            raise AppError("CONFLICT", "A cash movement reference has different details.", 409)

    expected = expected_cash(shift.float_cents, sales, refunds, moves)
    if data["expected"] != expected or data["variance"] != data["counted"] - expected:
        raise invalid(
            "The expected cash or variance does not match server calculations.", expected="invalid"
        )

    shift.moves = moves
    shift.cash_sales_cents = sales
    shift.cash_refunds_cents = refunds
    shift.status = "closed"
    shift.closed_at = timezone.now()
    shift.closed_label = data.get("closedAt") or ""
    shift.closed_by = actor
    shift.counted_cents = data["counted"]
    shift.expected_cents = expected
    shift.variance_cents = data["counted"] - expected
    shift.note = data.get("note") or ""
    shift.approved_by = actor
    shift.updated_by = staff
    shift.save()
    return shift


def record_transaction(request, data):
    location = location_for(request.user, data["cafeId"])
    row, _ = TillTransaction.objects.get_or_create(
        location=location,
        kind=data["kind"],
        reference=data["reference"],
        defaults={
            "order_number": data.get("orderId") or "",
            "terminal_id": data.get("terminalId") or terminal_of(request),
            "amount_cents": data["amountC"],
            "gst_cents": data["gstC"],
            "occurred_at": data["at"],
            "staff_name": data.get("staff") or "",
            "created_by": request.user,
            "updated_by": request.user,
        },
    )
    return row


AUDIT_TYPE = "pos.audit"


def client_ip(request):
    try:
        return str(ipaddress.ip_address(BaseThrottle().get_ident(request)))
    except ValueError:
        return None


def append_audit(request, entry):
    """The till's own activity log (who did what, approvals) kept in the BOS activity log."""
    location_for(request.user, entry["cafe"])
    if AuditLog.objects.filter(entity_type=AUDIT_TYPE, entity_id=entry["id"]).exists():
        return
    AuditLog.objects.create(
        actor=request.user,
        action=f"pos: {entry['action']}"[:60],
        entity_type=AUDIT_TYPE,
        entity_id=entry["id"],
        after={**entry, "terminal": terminal_of(request)},
        ip=client_ip(request),
    )


# ---------------------------------------------------------------- customers & coffee cards


def customers():
    return User.objects.filter(role="customer", is_active=True)


def customer_for(customer_id):
    customer = customer_of(customer_id)
    if customer is None:
        raise not_found("That customer was not found.")
    return customer


def till_balance(card):
    """The app's card (9 stamps = free coffee ready) as the till's stamps + rewards."""
    if card is None:
        return 0, 0
    rewards, stamps = divmod(card.stamps, STAMPS_PER_REWARD)
    return stamps, rewards


def card_stamps(stamps, rewards):
    """Encode the till's progress and accumulated rewards as unspent stamp units."""
    return rewards * STAMPS_PER_REWARD + stamps


def update_customer(request, customer, patch):
    require_staff_permission(request, "customers")
    before = snapshot(customer)
    profile = CustomerProfile.objects.select_for_update().filter(
        user=customer
    ).first() or CustomerProfile.objects.create(user=customer)
    if "name" in patch:
        first, _, last = patch["name"].strip().partition(" ")
        customer.first_name, customer.last_name = first[:100], last.strip()[:100]
    if "phone" in patch:
        customer.phone = patch["phone"].strip()
    if patch.get("email") and patch["email"].strip().lower() != customer.email:
        email = patch["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=customer.pk).exists():
            raise AppError("CONFLICT", "Another customer already uses that email.", 409)
        customer.email = email
    customer.save()
    if "homeCafe" in patch:
        profile.preferred_location = (
            Location.objects.filter(slug=patch["homeCafe"], is_published=True).first()
            if patch["homeCafe"]
            else None
        )
    if "dietary" in patch:
        profile.app_preferences = {**(profile.app_preferences or {}), "dietary": patch["dietary"]}
    if "notes" in patch:
        profile.notes = patch["notes"]
    profile.save()
    audit(request, customer, before, "pos_customer_update")
    return customer


def set_card_balance(request, customer, stamps, rewards):
    """The till set a customer's card (stamp at the counter, reward redeemed, order refunded)."""
    card = loyalty.card_for(customer, lock=True)
    before = card.stamps
    card.stamps = card_stamps(stamps, rewards)
    if card.stamps != before:
        card.updated_by = request.user
        card.save(update_fields=["stamps", "updated_by", "updated_at"])
        where = default_location(request.user).name
        loyalty.record_stamp_change(card, before, location_name=where)
    return customer


# ---------------------------------------------------------------- sold out


def item_for(slug):
    item = MenuItem.objects.filter(slug=slug).first()
    if item is None:
        raise not_found("That menu item was not found.")
    return item


def set_sold_out(request, item_slug, sold_out):
    """Sold out "at every café" = at every café this till account works at."""
    item = item_for(item_slug)
    for location in till_locations(request.user):
        set_availability(request, location.slug, [item.slug], {"is_available": not sold_out})


def set_cafe_availability(request, cafe, item_slug, available):
    location = location_for(request.user, cafe)
    set_availability(
        request, location.slug, [item_for(item_slug).slug], {"is_available": available}
    )


# ---------------------------------------------------------------- reservations

FROM_TILL_BOOKING = {
    "pending": "requested",
    "confirmed": "confirmed",
    "completed": "seated",
    "cancelled": "cancelled",
}


def upsert_reservation(request, reference, data):
    if data["id"] != reference:
        raise invalid("The reservation id does not match the address.", id="invalid")
    location = location_for(request.user, data["cafe"])
    fields = {
        "location": location,
        "date": data["date"],
        "time": data["time"],
        "party_size": data["guests"],
        "name": data["name"],
        "phone": data.get("phone") or "",
        "email": data.get("email") or "",
        "notes": data.get("notes") or "",
    }
    if data.get("seating") in ("indoor", "outdoor"):
        fields["seating"] = data["seating"]
    target = FROM_TILL_BOOKING[data["status"]]
    booking = (
        Booking.objects.select_for_update()
        .filter(reference=reference, location__in=till_locations(request.user))
        .first()
    )
    if booking is None:
        if Booking.objects.filter(reference=reference).exists():
            raise AppError("CONFLICT", f"Reservation {reference} belongs to another café.", 409)
        booking = Booking.objects.create(
            reference=reference,
            source="staff",
            status=target,
            customer=customer_of(data.get("customerId")),
            handled_by=request.user,
            handled_at=timezone.now(),
            created_by=request.user,
            updated_by=request.user,
            **fields,
        )
        audit(request, booking, None, "pos_create")
        return booking
    changes = {key: value for key, value in fields.items() if getattr(booking, key) != value}
    if target == booking.status:
        return update_booking(booking, changes, request) if changes else booking
    # The till seats a guest straight from a request; BOS keeps request → confirmed → seated.
    steps = (
        ["confirmed", target] if booking.status == "requested" and target == "seated" else [target]
    )
    for status in steps:
        booking = update_booking(booking, {**changes, "status": status}, request)
        changes = {}
    return booking


# ---------------------------------------------------------------- catering

FROM_TILL_ENQUIRY = {
    "quoted": "quoted",
    "confirmed": "won",
    "delivered": "closed",
    "cancelled": "lost",
}


def catering_scope_location(location):
    return Enquiry.objects.filter(kind="catering").filter(
        Q(location=location) | Q(location__isnull=True)
    )


def catering_scope(user):
    return Enquiry.objects.filter(kind="catering").filter(
        Q(location__in=till_locations(user)) | Q(location__isnull=True)
    )


def upsert_catering(request, reference, data):
    if data["id"] != reference:
        raise invalid("The enquiry id does not match the address.", id="invalid")
    location = location_for(request.user, data["cafe"]) if data.get("cafe") else None
    enquiry = catering_scope(request.user).select_for_update().filter(reference=reference).first()
    if enquiry is None and Enquiry.objects.filter(reference=reference).exists():
        raise AppError("CONFLICT", f"Enquiry {reference} belongs to another café.", 409)
    status = FROM_TILL_ENQUIRY.get(data["status"])
    if status is None:  # "enquiry": keep "in progress" if the team already started on it
        status = enquiry.status if enquiry and enquiry.status in ("new", "in_progress") else "new"
    package = CateringPackage.objects.filter(name=data.get("packageId") or "").first()
    payload = {
        **(enquiry.payload if enquiry else {}),
        "time": data.get("time") or "",
        "fulfil": data.get("fulfil") or "pickup",
        "address": data.get("address") or "",
        "contact": data.get("contact") or "",
        "dietary": data.get("dietary") or [],
        "occasion": data.get("occasion") or "",
        "quote": data.get("quote") or 0,
        **({} if enquiry else {"source": "pos"}),
    }
    fields = {
        "name": data["name"],
        "email": data.get("email") or "",
        "phone": data.get("phone") or "",
        "location": location,
        "event_date": parse_date(data.get("date") or ""),
        "headcount": data.get("guests") or None,
        "package": package,
        "message": data.get("notes") or "",
        "payload": payload,
        "status": status,
    }
    before = snapshot(enquiry)
    if enquiry is None:
        enquiry = Enquiry.objects.create(
            kind="catering",
            reference=reference,
            customer=customer_of(data.get("customerId")),
            created_by=request.user,
            updated_by=request.user,
            **fields,
        )
    else:
        for key, value in fields.items():
            setattr(enquiry, key, value)
        enquiry.updated_by = request.user
        enquiry.save()
    audit(request, enquiry, before, "pos_create" if before is None else "pos_update")
    return enquiry


def set_category_order(request, cafe, ids):
    location = location_for(request.user, cafe)

    if request.user.role not in {"owner", "admin", "manager"}:
        raise AppError("FORBIDDEN", "A manager account is required to change category order.", 403)

    valid = set(
        Category.objects.filter(is_active=True, slug__in=ids).values_list("slug", flat=True)
    )
    if any(category not in valid for category in ids):
        raise invalid("One or more categories are unknown or inactive.", ids="invalid")

    TillSettings.objects.update_or_create(location=location, defaults={"category_order": ids})


def set_item_price(request, cafe, item_slug, price):
    location = location_for(request.user, cafe)

    if request.user.role not in {"owner", "admin"}:
        raise AppError("FORBIDDEN", "Only owner/admin accounts can change menu prices.", 403)

    item = item_for(item_slug)
    if item.location_id is not None and item.location_id != location.pk:
        raise not_found("This item does not belong to the selected cafe.")

    set_availability(
        request, location.slug, [item.slug], {"price_override_cents": int(price * 100)}
    )


def set_modifier_availability(request, cafe, label, available):
    location = location_for(request.user, cafe)

    if request.user.role not in {"owner", "admin", "manager"}:
        raise AppError(
            "FORBIDDEN", "A manager account is required to change modifier availability.", 403
        )

    entries = menu_items(location, include_unavailable_modifiers=True)
    labels = {name for entry in entries for group in entry.groups for name, price in group.options}
    if label not in labels:
        raise not_found("This modifier is not on the selected cafe menu.")

    row = LocationModifierAvailability.objects.filter(location=location, label=label).first()
    before = snapshot(row) if row else None
    row, created = LocationModifierAvailability.objects.update_or_create(
        location=location,
        label=label,
        defaults={"available": available, "updated_by": request.user},
        create_defaults={
            "available": available,
            "created_by": request.user,
            "updated_by": request.user,
        },
    )
    audit(request, row, before, "create" if created else "update")


def update_till_toggles(request, cafe, toggles):
    location = location_for(request.user, cafe)

    if request.user.role not in {"owner", "admin"}:
        raise AppError("FORBIDDEN", "Only owner/admin accounts can change till settings.", 403)

    settings, created = TillSettings.objects.select_for_update().get_or_create(
        location=location,
        defaults={"created_by": request.user, "updated_by": request.user},
    )
    before = None if created else snapshot(settings)
    settings.toggles = {**settings.toggles, **toggles}
    settings.updated_by = request.user
    settings.save(update_fields=["toggles", "updated_by", "updated_at"])
    audit(request, settings, before, "create" if created else "update")


##### Loyalty #####
@transaction.atomic
def apply_loyalty_action(request, customer, data):
    location = location_for(request.user, data["cafe"])
    staff = require_staff_permission(request, "loyalty.stamp", location)

    # Serialize POS operations, including creation of a missing card.
    User.objects.select_for_update().get(pk=customer.pk)
    card = loyalty.card_for(customer, lock=True)
    before = card.stamps

    if data["action"] == "stamp":
        card.stamps = before + 1
    else:
        if before < STAMPS_PER_REWARD:
            raise AppError("CONFLICT", "This card has no reward ready to redeem.", 409)
        card.stamps -= STAMPS_PER_REWARD

    card.updated_by = staff
    card.save(update_fields=["stamps", "updated_by", "updated_at"])
    loyalty.record_stamp_change(card, before, location_name=location.name)
    return customer


@transaction.atomic
def award_pos_order_loyalty(request, order):
    order = Order.objects.select_for_update().get(pk=order.pk)
    staff = require_staff_permission(request, "pos", order.location)

    if order.channel != "pos":
        raise invalid("Only POS orders can use this loyalty operation.")
    if order.pos_loyalty_processed_at is not None:
        return order
    if order.status in {"declined", "cancelled", "refunded"}:
        raise AppError("CONFLICT", "Loyalty cannot be awarded for this order status.", 409)
    if not order.customer_id:
        raise invalid("Attach a customer before processing loyalty.")
    if order.reward_redeemed and (
        order.pos_reward_reserved_at is None or order.pos_reward_returned_at is not None
    ):
        raise AppError("CONFLICT", "This order has no valid reward reservation.", 409)
    if paid_cents(order) < till_total_cents(order):
        raise AppError("CONFLICT", "The order must be fully paid before awarding stamps.", 409)

    User.objects.select_for_update().get(pk=order.customer_id)
    card = loyalty.card_for(order.customer, lock=True)
    settings = TillSettings.objects.filter(location=order.location).first()
    auto_stamp = (settings.toggles or {}).get("autoStamp", True) if settings else True

    coffee_count = (
        order.lines.filter(
            qty__gt=0,
            menu_item__category__slug="coffee",
        ).aggregate(total=Sum("qty"))["total"]
        or 0
    )

    before = card.stamps
    earning_units = max(0, coffee_count - int(order.reward_redeemed))
    after, earned = loyalty.stamps_after_order(
        before,
        earning_units if auto_stamp else 0,
        False,
    )

    if after != before:
        card.stamps = after
        card.updated_by = staff
        card.save(update_fields=["stamps", "updated_by", "updated_at"])
        loyalty.record_stamp_change(card, before, location_name=order.location.name)

    order.pos_loyalty_lines = pos_loyalty_line_snapshot(order, earned)
    order.stamps_earned = earned
    order.pos_loyalty_processed_at = timezone.now()
    order.updated_by = staff
    order.save(
        update_fields=[
            "stamps_earned",
            "pos_loyalty_lines",
            "pos_loyalty_processed_at",
            "updated_by",
            "updated_at",
        ]
    )
    return order


@transaction.atomic
def reserve_pos_order_reward(request, order):
    order = Order.objects.select_for_update().get(pk=order.pk)
    staff = require_staff_permission(request, "loyalty.stamp", order.location)

    if order.channel != "pos" or not order.reward_redeemed:
        raise invalid("This operation requires a POS order using a reward.")
    if order.pos_reward_returned_at is not None:
        raise AppError("CONFLICT", "This order's reward has already been returned.", 409)
    if order.pos_reward_reserved_at is not None:
        return order
    if order.status in {"declined", "cancelled", "refunded"}:
        raise AppError("CONFLICT", "A reward cannot be reserved for this order status.", 409)
    if not order.customer_id:
        raise invalid("Attach a customer before redeeming a reward.")
    if order.payments.exists():
        raise AppError("CONFLICT", "Reserve the reward before recording payment.", 409)

    eligible = (
        order.lines.filter(
            qty__gt=0,
            menu_item__category__slug="coffee",
            unit_price_cents__gt=0,
        )
        .order_by("unit_price_cents")
        .first()
    )
    if eligible is None:
        raise invalid("A reward requires an eligible coffee or hot drink.")

    reward_value = eligible.unit_price_cents
    if len(order.discounts or []) != 1 or order.discount_cents != reward_value:
        raise invalid(
            "The reward must discount the cheapest eligible drink, without another discount."
        )

    User.objects.select_for_update().get(pk=order.customer_id)
    card = loyalty.card_for(order.customer, lock=True)
    if card.stamps < STAMPS_PER_REWARD:
        raise AppError("CONFLICT", "This customer has no reward available.", 409)

    before = card.stamps
    card.stamps -= STAMPS_PER_REWARD
    card.updated_by = staff
    card.save(update_fields=["stamps", "updated_by", "updated_at"])
    loyalty.record_stamp_change(card, before, location_name=order.location.name)

    order.reward_cents = reward_value
    order.pos_reward_reserved_at = timezone.now()
    order.updated_by = staff
    order.save(
        update_fields=[
            "reward_cents",
            "pos_reward_reserved_at",
            "updated_by",
            "updated_at",
        ]
    )
    return order


@transaction.atomic
def return_unpaid_pos_order_reward(request, order):
    order = Order.objects.select_for_update().get(pk=order.pk)
    staff = require_staff_permission(request, "orders.refund", order.location)

    if order.channel != "pos":
        raise invalid("Only POS orders can use this reward operation.")
    if order.pos_reward_reserved_at is None:
        return order
    if order.pos_reward_returned_at is not None:
        return order
    if order.status not in {"declined", "cancelled"}:
        raise AppError("CONFLICT", "Cancel the unpaid order before returning its reward.", 409)
    if order.pos_loyalty_processed_at is not None:
        raise AppError("CONFLICT", "Processed orders require the refund loyalty flow.", 409)
    if refunded_cents(order) < paid_cents(order):
        raise AppError("CONFLICT", "Return all payments before restoring the reward.", 409)

    User.objects.select_for_update().get(pk=order.customer_id)
    card = loyalty.card_for(order.customer, lock=True)
    before = card.stamps
    card.stamps = before + STAMPS_PER_REWARD
    card.updated_by = staff
    card.save(update_fields=["stamps", "updated_by", "updated_at"])
    loyalty.record_stamp_change(card, before, location_name=order.location.name)

    order.pos_reward_returned_at = timezone.now()
    order.updated_by = staff
    order.save(update_fields=["pos_reward_returned_at", "updated_by", "updated_at"])
    return order


@transaction.atomic
def reverse_fully_refunded_pos_loyalty(request, order):
    order = Order.objects.select_for_update().get(pk=order.pk)
    staff = require_staff_permission(request, "orders.refund", order.location)

    if order.channel != "pos":
        raise invalid("Only POS orders can use this loyalty operation.")
    if order.pos_loyalty_reversed_at is not None:
        return order
    if order.pos_loyalty_processed_at is None:
        raise AppError("CONFLICT", "This order's loyalty has not been processed.", 409)
    if order.status not in {"refunded", "cancelled"} or refunded_cents(order) < paid_cents(order):
        raise AppError("CONFLICT", "Fully refund the order before reversing its loyalty.", 409)
    if not order.customer_id:
        raise invalid("This order has no customer.")

    User.objects.select_for_update().get(pk=order.customer_id)
    card = loyalty.card_for(order.customer, lock=True)
    before = card.stamps
    restore_reward = (
        order.reward_redeemed
        and order.pos_reward_reserved_at is not None
        and order.pos_reward_returned_at is None
    )
    restored = STAMPS_PER_REWARD if restore_reward else 0
    already_reversed = sum(
        row.get("reversed_stamps", 0) for row in order.pos_loyalty_lines.values()
    )
    remaining_earned = max(0, order.stamps_earned - already_reversed)
    card.stamps = max(0, before - remaining_earned + restored)

    if card.stamps != before:
        card.updated_by = staff
        card.save(update_fields=["stamps", "updated_by", "updated_at"])
        loyalty.record_stamp_change(card, before, location_name=order.location.name)

    now = timezone.now()
    order.pos_loyalty_reversed_at = now
    if restore_reward:
        order.pos_reward_returned_at = now
    order.updated_by = staff
    order.save(
        update_fields=[
            "pos_loyalty_reversed_at",
            "pos_reward_returned_at",
            "updated_by",
            "updated_at",
        ]
    )
    return order


def pos_loyalty_line_snapshot(order, earned):
    lines = list(
        order.lines.filter(
            qty__gt=0,
            menu_item__category__slug="coffee",
        ).order_by("position", "pk")
    )

    free_line = None
    if order.reward_redeemed and lines:
        free_line = min(
            (line for line in lines if line.unit_price_cents > 0),
            key=lambda line: (line.unit_price_cents, line.position, str(line.pk)),
            default=None,
        )

    remaining = earned
    result = {}
    for line in lines:
        key = line.key or str(line.pk)
        free_units = int(free_line is not None and line.pk == free_line.pk)
        awarded = min(remaining, max(0, line.qty - free_units))
        result[key] = {
            "qty": line.qty,
            "free_units": free_units,
            "awarded": awarded,
            "returned_units": 0,
            "reversed_stamps": 0,
        }
        remaining -= awarded

    if remaining:
        raise invalid("The earned stamps do not match the eligible order lines.")
    return result


@transaction.atomic
def reverse_partial_pos_loyalty(request, order, adjustment):
    order = Order.objects.select_for_update().get(pk=order.pk)
    staff = require_staff_permission(request, "orders.refund", order.location)
    adjustment = get_object_or_404(
        OrderAdjustment.objects.select_for_update(),
        pk=adjustment.pk,
        order=order,
    )

    if order.channel != "pos" or order.pos_loyalty_processed_at is None:
        raise AppError("CONFLICT", "This POS order's loyalty has not been processed.", 409)
    if adjustment.pos_loyalty_processed_at is not None:
        return order
    if order.pos_loyalty_reversed_at is not None:
        raise AppError("CONFLICT", "This order's loyalty has already been fully reversed.", 409)
    if adjustment.mode not in {"refund", "void"}:
        raise invalid("Partial loyalty reversal requires a refund or void.")

    recorded = json.loads(json.dumps(order.pos_loyalty_lines))
    items = adjustment.items or {}
    known = {line.key or str(line.pk) for line in order.lines.all()}
    reversed_stamps = 0

    for key, qty in items.items():
        if key not in known:
            raise invalid("The adjustment contains an unknown order line.")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
            raise invalid("Returned quantities must be non-negative whole numbers.")
        if key not in recorded or qty == 0:
            continue

        row = recorded[key]
        remaining_units = row["qty"] - row["returned_units"]
        if qty > remaining_units:
            raise AppError(
                "CONFLICT", "More coffee units were returned than remain on this order.", 409
            )

        reversed_for_line = min(qty, row["awarded"] - row["reversed_stamps"])
        row["returned_units"] += qty
        row["reversed_stamps"] += reversed_for_line
        reversed_stamps += reversed_for_line

    if reversed_stamps:
        User.objects.select_for_update().get(pk=order.customer_id)
        card = loyalty.card_for(order.customer, lock=True)
        before = card.stamps
        card.stamps = max(0, before - reversed_stamps)
        if card.stamps != before:
            card.updated_by = staff
            card.save(update_fields=["stamps", "updated_by", "updated_at"])
            loyalty.record_stamp_change(card, before, location_name=order.location.name)

    order.pos_loyalty_lines = recorded
    order.updated_by = staff
    order.save(update_fields=["pos_loyalty_lines", "updated_by", "updated_at"])

    adjustment.pos_loyalty_processed_at = timezone.now()
    adjustment.save(update_fields=["pos_loyalty_processed_at", "updated_at"])
    return order


def validate_pos_loyalty_adjustment(order, data):
    if order.channel != "pos" or not (
        order.pos_loyalty_processed_at or order.pos_reward_reserved_at
    ):
        return

    change = data["adjustment"]
    mode = change["mode"]
    lines = {line.key or str(line.pk): line for line in order.lines.all()}
    kept = {row["key"]: row["qty"] for row in data["lines"]}
    items = {key: qty for key, qty in (change.get("items") or {}).items() if qty}

    if len(kept) != len(data["lines"]):
        raise invalid("Each order line may appear only once.", lines="invalid")
    if set(kept) - set(lines) or set(items) - set(lines):
        raise invalid("The adjustment contains an unknown order line.", items="invalid")

    if mode == "cancel":
        remaining = paid_cents(order) - refunded_cents(order)
        if change["amount"] != remaining:
            raise invalid(
                "Cancelling an order must return its remaining payment.", amount="invalid"
            )
        return

    removed = {}
    for key, line in lines.items():
        qty = kept.get(key, 0)
        if qty > line.qty:
            raise invalid("An adjustment cannot increase item quantities.", lines="invalid")
        if qty < line.qty:
            removed[key] = line.qty - qty

    if mode == "refund" and removed:
        raise invalid(
            "Use void to remove items; a refund keeps order quantities unchanged.", lines="invalid"
        )
    if mode == "void" and items != removed:
        raise invalid("Selected void items must match the quantities removed.", items="invalid")

    previously_refunded = {}
    for previous in order.adjustments.filter(mode="refund"):
        for key, qty in (previous.items or {}).items():
            previously_refunded[key] = previously_refunded.get(key, 0) + qty

    for key, qty in items.items():
        available = max(0, lines[key].qty - previously_refunded.get(key, 0))
        if qty > available:
            raise AppError("CONFLICT", "More item units were returned than remain available.", 409)


def settle_pos_order_loyalty(request, order):
    if not order.pos_loyalty_managed or not order.customer_id:
        return order
    if order.pos_loyalty_processed_at is not None:
        return order
    if paid_cents(order) < till_total_cents(order):
        return order
    return award_pos_order_loyalty(request, order)

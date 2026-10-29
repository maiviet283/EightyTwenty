"""
Till shapes (POS src/domain/types.ts and src/services/contracts.ts). Orders, lines and catering
prices are AUD dollars (numbers); payments, refunds and cash are integer cents. Café and menu
ids are the website slugs ("braddon", "mighty").
"""

import re
from collections import defaultdict
from datetime import timedelta

from django.db.models import Case, Count, F, IntegerField, Max, Q, Sum, When
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.bookings.models import Booking
from apps.core.models import AuditLog, SiteSettings
from apps.core.public_services import categories_data, locations_data
from apps.enquiries.models import CateringPackage
from apps.loyalty import services as loyalty
from apps.loyalty.models import STAMPS_PER_REWARD, LoyaltyActivity, LoyaltyCard
from apps.menu.catalog import menu_items
from apps.menu.models import LocationMenuItem, LocationModifierAvailability
from apps.orders import services as order_services
from apps.orders.models import Order, OrderLine, OrderPayment

from . import pins
from .models import CashShift, Promotion, TillSettings, TillTable
from .services import (
    AUDIT_TYPE,
    LIVE,
    TO_TILL,
    catering_scope,
    catering_scope_location,
    customers,
    till_balance,
    till_locations,
    till_total_cents,
)

TILL_TYPE = {
    "dine_in": "dinein",
    "pickup": "pickup",
    "takeaway": "takeaway",
    "delivery": "delivery",
}
TOGGLES = {"kitchen": True, "receipt": False, "tips": False, "openDrawer": True, "autoStamp": True}


def dollars(cents):
    return cents / 100


def local(value):
    return timezone.localtime(value)


def person(user):
    if user is None:
        return ""
    return f"{user.first_name} {user.last_name}".strip() or user.email.split("@")[0]


def initials(name):
    return "".join(part[0] for part in name.split() if part)[:2].upper()


# ---------------------------------------------------------------- catalogue


def photo(asset):
    """The till ships its own copies of the website photos (img/... → img-pos/...jpg)."""
    if not asset:
        return ""
    if asset.legacy_path.startswith("img/"):
        return asset.legacy_path
    return asset.variants.get("md", "")


def menu(location):
    """This café's menu (chain-wide items plus its own, at its prices) and its sold-out ids."""
    entries = menu_items(location, include_unavailable_modifiers=True)
    return [
        {
            "id": entry.item.slug,
            "cat": entry.category_slug,
            "name": entry.item.name,
            "price": dollars(entry.price_cents),
            "diet": [link.tag.code for link in entry.item.dietary_links.all()],
            "img": photo(entry.item.image),
            "desc": entry.item.description,
            "mods": [
                {
                    "id": group.code,
                    "name": group.name,
                    "required": group.required,
                    "max": group.max_select,
                    "options": [[name, dollars(price)] for name, price in group.options],
                }
                for group in entry.groups
            ],
            **({"featured": True} if entry.item.is_featured else {}),
        }
        for entry in entries
    ], [entry.item.slug for entry in entries if not entry.available]


def menu_dto(entry):
    """GET /pos/locations/{slug}/menu (the till's PosMenuItemDto, src/services/http/posMapping.ts)."""
    image = entry.item.image
    return {
        "id": entry.item.slug,
        "categoryId": entry.category_slug,
        "name": entry.item.name,
        "description": entry.item.description,
        "priceCents": entry.price_cents,
        "dietary": [link.tag.code for link in entry.item.dietary_links.all()],
        "imageUrl": (image.variants.get("md") or None) if image else None,
        "featured": entry.item.is_featured,
        "available": entry.available,
        "modifierGroups": [
            {
                "id": group.code,
                "name": group.name,
                "required": group.required,
                "maxSelections": group.max_select,
                "options": [{"name": name, "priceCents": price} for name, price in group.options],
            }
            for group in entry.groups
        ],
    }


def catering():
    return [
        {
            "id": row.name,
            "pricePerPerson": dollars(row.price_cents),
            "unit": row.unit,
            "blurb": row.blurb,
            "items": row.items,
        }
        for row in CateringPackage.objects.filter(is_active=True)
    ]


# ---------------------------------------------------------------- people


def staff_member(user, cafe):
    name = person(user)
    return {
        "id": str(user.pk),
        "name": name,
        "initials": initials(name),
        "role": pins.till_role(user),
        "cafe": cafe,
        "shift": "",
    }


def team(location):
    """Who can sign in at this café's tills (a PIN is set)."""
    people = (
        pins.team()
        .exclude(pos_pin="")
        .filter(
            Q(is_superuser=True)
            | Q(role__in=["owner", "admin"])
            | Q(assignments__is_active=True, assignments__location=location)
        )
        .distinct()
        .order_by("first_name", "email")
    )
    return [staff_member(user, location.slug) for user in people]


# ---------------------------------------------------------------- customers, bookings, catering


def related(obj, name):
    """A reverse one-to-one that may not exist yet (profile, coffee card)."""
    try:
        return getattr(obj, name)
    except AttributeError:  # RelatedObjectDoesNotExist is an AttributeError
        return None


def customer(user, stats=None):
    profile, card = related(user, "profile"), related(user, "loyalty_card")
    stamps, rewards = till_balance(card)
    stats = stats or {}
    last = stats.get("last")
    home = profile.preferred_location if profile else None
    return {
        "id": str(user.pk),
        "name": person(user),
        "phone": user.phone,
        "email": user.email,
        "stamps": stamps,
        "rewards": rewards,
        "homeCafe": home.slug if home else "",
        "dietary": list(((profile.app_preferences or {}) if profile else {}).get("dietary", [])),
        "notes": profile.notes if profile else "",
        "since": local(user.date_joined).strftime("%b %Y"),
        "wallet": bool(card and card.wallet_pass_added),
        "visits": stats.get("visits", 0),
        "spend": dollars(stats.get("spend") or 0),
        "lastDays": (timezone.localdate() - local(last).date()).days if last else 999,
    }


def customer_list(limit=2000):
    standing = Order.objects.filter(customer__isnull=False).exclude(
        status__in=[*order_services.CLOSED, "received"]
    )
    stats = {
        row["customer_id"]: row
        for row in standing.values("customer_id").annotate(
            visits=Count("id"), spend=Sum("total_cents"), last=Max("created_at")
        )
    }
    people = (
        customers()
        .select_related("profile__preferred_location", "loyalty_card")
        .order_by("-date_joined")[:limit]
    )
    return [customer(user, stats.get(user.pk)) for user in people]


BOOKING_TO_TILL = {
    "requested": "pending",
    "confirmed": "confirmed",
    "seated": "completed",
    "declined": "cancelled",
    "cancelled": "cancelled",
    "no_show": "cancelled",
}
SOURCE_TO_TILL = {"web": "web", "app": "app", "staff": "pos"}


def reservation(row):
    data = {
        "id": row.reference,
        "cafe": row.location.slug,
        "date": row.date.isoformat(),
        "time": row.time.strftime("%H:%M"),
        "guests": row.party_size,
        "name": row.name,
        "phone": row.phone,
        "email": row.email,
        "notes": row.notes,
        "status": BOOKING_TO_TILL[row.status],
        "source": SOURCE_TO_TILL.get(row.source, "web"),
    }
    if row.customer_id:
        data["customerId"] = str(row.customer_id)
    if row.seating:
        data["seating"] = row.seating
    if row.source != "staff":
        data["extRef"] = row.reference
    return data


ENQUIRY_TO_TILL = {
    "new": "enquiry",
    "in_progress": "enquiry",
    "quoted": "quoted",
    "won": "confirmed",
    "closed": "delivered",
    "lost": "cancelled",
}


def catering_enquiry(row):
    extra = row.payload or {}
    package = row.package
    quote = extra.get("quote") or (
        dollars(package.price_cents * (row.headcount or 0)) if package else 0
    )
    source = "pos" if extra.get("source") == "pos" else "app" if row.customer_id else "web"
    data = {
        "id": row.reference,
        "packageId": package.name if package else "",
        "guests": row.headcount or 0,
        "date": row.event_date.isoformat() if row.event_date else "",
        "time": extra.get("time", ""),
        "fulfil": extra.get("fulfil") or "pickup",
        "address": extra.get("address", ""),
        "cafe": row.location.slug if row.location else "",
        "name": row.name,
        "contact": extra.get("contact") or row.phone,
        "email": row.email,
        "phone": row.phone,
        "occasion": extra.get("occasion", ""),
        "dietary": extra.get("dietary") or [],
        "notes": row.message,
        "quote": quote,
        "status": ENQUIRY_TO_TILL[row.status],
        "source": source,
    }
    if row.customer_id:
        data["customerId"] = str(row.customer_id)
    if source != "pos":
        data["extRef"] = row.reference
    return data


def availability(user, location, unavailable_here):
    """Sold out at every café of this account → soldOut; only here → branchOff."""
    cafes = list(till_locations(user).exclude(status="tbc").values_list("pk", flat=True))
    off = defaultdict(set)
    rows = LocationMenuItem.objects.filter(location_id__in=cafes, is_available=False)
    for cafe, slug in rows.values_list("location_id", "menu_item__slug"):
        off[cafe].add(slug)
    everywhere = set.intersection(*(off[cafe] for cafe in cafes)) if cafes else set()
    return {
        "soldOut": [slug for slug in unavailable_here if slug in everywhere],
        "branchOff": {location.slug: [slug for slug in unavailable_here if slug not in everywhere]},
        "unavailableMods": list(
            LocationModifierAvailability.objects.filter(location=location, available=False)
            .order_by("label")
            .values_list("label", flat=True)
        ),
        "prices": {},
        "lastPushedAt": local(SiteSettings.load().updated_at).isoformat(),
    }


# ---------------------------------------------------------------- orders


def with_details(qs):
    return qs.select_related("location", "handled_by").prefetch_related(
        "lines", "payments", "adjustments"
    )


def line(row):
    data = {
        "key": row.key or str(row.pk),
        "itemId": row.item_slug,
        "name": row.name,
        "price": dollars(row.unit_price_cents),
        "qty": row.qty,
        "sel": row.selections or {},
        "adjust": row.adjust or [],
        "note": row.note,
    }
    if row.prep:
        data["prep"] = row.prep
    return data


def payment(row):
    data = {
        "method": row.method,
        "amount": row.amount_cents,
        "tip": row.tip_cents,
        "surcharge": row.surcharge_cents,
        "ref": row.reference,
        "giftLast4": row.gift_last4 or None,
        "at": row.taken_at,
        "by": row.staff_name,
    }
    for key, value in [
        ("giftCardId", str(row.gift_card_id or "")),
        ("mode", row.split_mode),
        ("items", row.items),
        ("cashPaid", row.cash_paid_cents),
        ("change", row.change_cents),
    ]:
        if value not in ("", None, {}):
            data[key] = value
    if row.rounding_cents:
        data["rounding"] = row.rounding_cents
    return data


def app_payments(row):
    """App orders were paid in the app: card/Apple Pay for the rest, gift card for its share."""
    rows = []
    charged = row.total_cents - row.surcharge_cents
    if charged > 0:
        rows.append(
            {
                "method": "wallet" if row.payment_choice == "apple_pay" else "card",
                "amount": charged,
                "tip": 0,
                "surcharge": row.surcharge_cents,
                "ref": f"app-{row.pk}",
                "giftLast4": None,
                "at": "",
                "by": "App",
            }
        )
    if row.gift_card_cents:
        rows.append(
            {
                "method": "gift",
                "amount": row.gift_card_cents,
                "tip": 0,
                "surcharge": 0,
                "ref": f"app-gift-{row.pk}",
                "giftLast4": None,
                "at": "",
                "by": "App",
            }
        )
    return rows


def adjustment(row):
    return {
        "key": row.key,
        "mode": row.mode,
        "amount": row.amount_cents,
        "alloc": row.allocation,
        "reason": row.reason,
        "by": row.staff_name,
        "approver": row.approver_name,
        "at": row.made_at,
        "items": row.items,
    }


def order(row):
    placed = local(row.placed_at or row.created_at)
    lines = [line(item) for item in row.lines.all() if item.qty]
    total = till_total_cents(row)
    if row.channel == "app":
        payments = app_payments(row)
        discounts = (
            [{"label": "Free coffee", "amount": dollars(row.reward_cents)}]
            if row.reward_cents
            else []
        )
        extra = ", ".join(
            part for part in [row.contact_name, row.contact_phone, row.delivery_address] if part
        )
        note = f"App order · {extra}" if extra else "App order"
    else:
        payments = [payment(p) for p in row.payments.all()]
        discounts, note = row.discounts, row.note
    paid = sum(p["amount"] for p in payments)
    data = {
        "id": row.number,
        "channel": row.channel,
        "type": TILL_TYPE[row.method],
        "status": TO_TILL[row.status],
        "accepted": row.status != "received",
        "cafe": row.location.slug,
        "time": (row.scheduled_time or placed).strftime("%H:%M"),
        "placedAt": placed.isoformat(),
        "tables": row.tables or ([row.table_number] if row.table_number else []),
        "lines": lines,
        "total": dollars(total),
        "paid": paid >= total,
        "methods": list(dict.fromkeys(p["method"] for p in payments)),
        "payments": payments,
        "note": note,
        "discounts": discounts,
        "rewardUsed": row.reward_redeemed,
        "staff": row.staff_name or person(row.handled_by),
        "history": row.history,
        "refunds": [adjustment(a) for a in row.adjustments.all()],
        # App orders: the server adds and returns their stamps, so the till must not touch the card.
        "stamped": row.channel != "app" and paid >= total,
        "cleared": row.table_cleared,
    }
    if row.table_number:
        data["table"] = row.table_number
    if row.customer_id:
        data["customerId"] = str(row.customer_id)
    if row.channel == "app":
        data["extRef"] = str(row.pk)
    if row.pos_loyalty_managed:
        data["serverLoyalty"] = True
    return data


# ---------------------------------------------------------------- cash & records


def shift(row):
    data = {
        "id": row.reference,
        "cafe": row.location.slug,
        "staffId": row.staff_ref,
        "staffName": row.staff_name,
        "openedAt": row.opened_label,
        "float": row.float_cents,
        "cashSales": row.cash_sales_cents,
        "cashRefunds": row.cash_refunds_cents,
        "moves": row.moves,
        "status": row.status,
    }
    if row.status == "closed":
        data.update(
            closedAt=row.closed_label,
            closedBy=row.closed_by,
            counted=row.counted_cents,
            expected=row.expected_cents,
            variance=row.variance_cents,
            note=row.note,
            approvedBy=row.approved_by,
        )
    return data


def audit_entry(row):
    entry = dict(row.after or {})
    entry.pop("terminal", None)
    return entry


# ---------------------------------------------------------------- reports


SALE_VALUE = Case(
    When(channel="app", then=F("total_cents") + F("gift_card_cents") - F("surcharge_cents")),
    default=F("total_cents"),
    output_field=IntegerField(),
)
STAMPS = re.compile(r"\+(\d+) stamp")


def change(current, previous):
    if not previous:
        return ""
    pct = (current - previous) * 100 / previous
    return f"{'+' if pct >= 0 else '−'}{abs(pct):.0f}%"


def when(value):
    moment = local(value)
    days = (timezone.localdate() - moment.date()).days
    clock = moment.strftime("%I:%M%p").lstrip("0").lower()
    if days == 0:
        return f"Today {clock}"
    if days == 1:
        return f"Yesterday {clock}"
    return f"{moment.day} {moment:%b}"


def guest_figures(location, today):
    """Coffee cards, bookings and catering at this café, for the dashboard and reports."""
    week = today - timedelta(days=6)
    activity = LoyaltyActivity.objects.filter(location_name=location.name)
    recent = activity.filter(created_at__date__gte=week)
    stamps = sum(
        int(match.group(1))
        for text in recent.filter(type="earn").values_list("description", flat=True)
        if (match := STAMPS.search(text))
    )
    regulars = customers().filter(profile__preferred_location=location)
    kept = Booking.objects.filter(location=location, date__range=(week, today)).exclude(
        status__in=["declined", "cancelled"]
    )
    bookings = kept.aggregate(n=Count("id"), covers=Sum("party_size"))
    month = catering_scope_for(location).filter(created_at__date__gte=today - timedelta(days=29))
    won = [row for row in month if row.status == "won"]
    return {
        "loyalty": {
            "stamps": stamps,
            "rewards": recent.filter(description__startswith="Free coffee redeemed").count(),
            "newMembers": regulars.filter(date_joined__date__gte=week).count(),
            "walletPasses": LoyaltyCard.objects.filter(
                wallet_pass_added=True, user__profile__preferred_location=location
            ).count(),
        },
        "reservations": {
            "week": bookings["n"],
            "covers": bookings["covers"] or 0,
            "noShow": kept.filter(status="no_show").count(),
            "avgParty": round((bookings["covers"] or 0) / bookings["n"], 1) if bookings["n"] else 0,
        },
        "catering": {
            "enquiries": len(month),
            "confirmed": len(won),
            "revenue": sum(catering_enquiry(row)["quote"] for row in won),
        },
        "loyaltyActivity": [
            {
                "what": f"{row.description} · {person(row.card.user)}",
                "when": when(row.created_at),
            }
            for row in activity.select_related("card__user").order_by("-created_at")[:8]
        ],
    }


def catering_scope_for(location):
    return catering_scope_location(location).select_related("location", "package").order_by("-id")


def reports(location):
    """Read-only figures for the till dashboard, from this café's records (gross order totals)."""
    today = timezone.localdate()
    start = today - timedelta(days=27)
    sold = Order.objects.filter(location=location).exclude(status__in=order_services.CLOSED)
    sold = sold.exclude(status="received")
    sold = sold.annotate(at=Coalesce("placed_at", "created_at"))
    by_day = {
        row["day"]: row
        for row in sold.filter(at__date__gte=start)
        .annotate(day=TruncDate("at"))
        .values("day")
        .annotate(sales=Sum(SALE_VALUE), orders=Count("id"))
    }

    def sales_on(day):
        return by_day.get(day, {}).get("sales") or 0

    def sales_in(back):
        return sum(sales_on(today - timedelta(days=back + n)) for n in range(7))

    days = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        row = by_day.get(day, {})
        days.append(
            {
                "d": day.strftime("%a"),
                "sales": dollars(row.get("sales") or 0),
                "orders": row.get("orders") or 0,
                **({"partial": True} if offset == 0 else {}),
            }
        )
    weeks = []
    for week in range(3, -1, -1):
        first = today - timedelta(days=week * 7 + 6)
        total = sum((by_day.get(first + timedelta(days=n), {}).get("sales") or 0) for n in range(7))
        weeks.append({"w": first.strftime("%d %b"), "sales": dollars(total)})
    week_start = today - timedelta(days=6)
    week_orders = sold.filter(at__date__gte=week_start)
    best = (
        OrderLine.objects.filter(order__in=week_orders, qty__gt=0)
        .values("item_slug")
        .annotate(units=Sum("qty"), rev=Sum(F("qty") * F("unit_price_cents")))
        .order_by("-units")[:5]
    )
    tenders = (
        OrderPayment.objects.filter(order__in=week_orders)
        .values("method")
        .annotate(amount=Sum("amount_cents"))
    )
    tender_total = sum(row["amount"] for row in tenders) or 1
    channels = week_orders.values("channel").annotate(n=Count("id"))
    channel_total = sum(row["n"] for row in channels) or 1
    labels = dict(OrderPayment._meta.get_field("method").choices)
    return {
        "days": days,
        "weeks": weeks,
        "best": [
            {"itemId": row["item_slug"], "qty": row["units"], "rev": dollars(row["rev"])}
            for row in best
        ],
        "payments": [
            {
                "m": labels.get(row["method"], row["method"]),
                "pct": round(row["amount"] * 100 / tender_total),
                "amt": dollars(row["amount"]),
            }
            for row in tenders
        ],
        "channels": [
            {
                "c": "POS" if row["channel"] == "pos" else "App",
                "pct": round(row["n"] * 100 / channel_total),
            }
            for row in channels
        ],
        **guest_figures(location, today),
        "salesTodayComparison": (
            f"{change(sales_on(today), sales_on(today - timedelta(days=7)))} vs last {today:%A}"
            if sales_on(today - timedelta(days=7))
            else ""
        ),
        "weekComparison": (
            f"{change(sales_in(0), sales_in(7))} vs previous week" if sales_in(7) else ""
        ),
    }


# ---------------------------------------------------------------- start-up snapshot


def promotions(location):
    """Promo codes live today at this café (or every café)."""
    today = timezone.localdate()
    rows = (
        Promotion.objects.filter(is_active=True)
        .filter(Q(location__isnull=True) | Q(location=location))
        .filter(Q(starts_on__isnull=True) | Q(starts_on__lte=today))
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=today))
    )
    return [
        {
            "code": row.code,
            "kind": row.kind,
            "value": row.percent if row.kind == "pct" else dollars(row.amount_cents or 0),
            "label": row.label,
        }
        for row in rows
    ]


def percent(rate):
    return f"{float(rate) * 100:g}"


def settings_data(location=None):
    saved = TillSettings.objects.filter(location=location).first() if location is not None else None
    rules = SiteSettings.load().order_rules or {}
    return {
        "loyalty": {"per": STAMPS_PER_REWARD, "wallet": True, "online": True},
        "loyaltyCopy": {"reward": loyalty.REWARD, "eligible": loyalty.ELIGIBILITY},
        "rules": {
            "gst": "10",
            "surcharge": percent(rules.get("cardSurcharge", 0.0295)),
            "delivery": percent(rules.get("deliveryRate", 0.1)),
        },
        "toggles": {**TOGGLES, **(saved.toggles if saved else {})},
        "cafeFields": {},
        "categoryOrder": saved.category_order if saved else [],
    }


def snapshot(user, location, terminal):
    slugs = set(till_locations(user).values_list("slug", flat=True))
    items, unavailable = menu(location)
    today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    orders = with_details(
        Order.objects.filter(location=location).filter(
            Q(created_at__gte=today) | Q(placed_at__gte=today) | Q(status__in=LIVE)
        )
    ).order_by("-created_at")
    shifts = CashShift.objects.filter(location=location).select_related("location")
    current = (
        shifts.filter(status="open", terminal_id=terminal).order_by("-created_at").first()
        if terminal
        else None
    )
    today_date = timezone.localdate()
    bookings = (
        Booking.objects.filter(
            location=location,
            date__range=(today_date - timedelta(days=2), today_date + timedelta(days=30)),
        )
        .select_related("location")
        .order_by("date", "time")
    )
    enquiries = (
        catering_scope(user)
        .filter(
            Q(created_at__date__gte=today_date - timedelta(days=90))
            | Q(event_date__gte=today_date - timedelta(days=7))
        )
        .select_related("location", "package")
        .order_by("-created_at")[:200]
    )
    audit_rows = AuditLog.objects.filter(
        entity_type=AUDIT_TYPE, after__cafe=location.slug
    ).order_by("-created_at")[:100]
    return {
        "locations": [row for row in locations_data() if row["id"] in slugs],
        "categories": categories_data(),
        "menu": items,
        "cateringPackages": catering(),
        "tables": [
            {"id": row.label, "seats": row.seats, "zone": row.zone}
            for row in TillTable.objects.filter(location=location, is_active=True)
        ],
        "promotions": promotions(location),
        "availability": availability(user, location, unavailable),
        "stock": [],
        "stockLog": [],
        "orders": [order(row) for row in orders],
        "customers": customer_list(),
        "giftCards": [],
        "reservations": [reservation(row) for row in bookings],
        "catering": [catering_enquiry(row) for row in enquiries],
        "staff": team(location),
        "roles": pins.ROLES,
        "settings": settings_data(location),
        "currentShift": shift(current) if current else None,
        "closedShifts": [shift(row) for row in shifts.filter(status="closed")[:20]],
        "audit": [audit_entry(row) for row in audit_rows],
        "reports": reports(location),
    }

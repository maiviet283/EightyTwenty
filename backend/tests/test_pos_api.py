"""Till API (/api/v1/pos): device + PIN sign-in, snapshot, orders, payments, kitchen, cash."""

from datetime import timedelta
from urllib.parse import quote

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import StaffAssignment
from apps.core.models import AuditLog
from apps.core.throttles import PersistentThrottle
from apps.locations.models import Location
from apps.loyalty.models import GiftCard, LoyaltyCard
from apps.menu.models import (
    Category,
    LocationMenuItem,
    MenuItem,
    MenuItemModifierGroup,
    ModifierGroup,
    ModifierOption,
)
from apps.mobile.errors import AppError
from apps.notifications.models import AppNotification
from apps.orders.models import Order, OrderLine
from apps.pos import pins
from apps.pos.models import CashShift, CommandReceipt, Promotion, TillTransaction
from apps.pos.views import OrdersView, PosView, SnapshotView, VerifyPinView

from .factories import UserFactory

BASE = "/api/v1/pos"
TERMINAL = "BRADDON-TILL-1"


@pytest.fixture
def cafe(db):
    return Location.objects.create(
        slug="braddon",
        name="Braddon",
        is_published=True,
        status="open",
        fulfilment_methods=["pickup", "dine_in"],
        email="braddon@example.com",
    )


@pytest.fixture
def elsewhere(db):
    return Location.objects.create(slug="civic", name="Civic", is_published=True, status="open")


@pytest.fixture
def menu(db):
    coffee = Category.objects.create(slug="coffee", name="Coffee & Hot Drinks")
    food = Category.objects.create(slug="allday", name="All Day Meals", sort_order=1)
    flat = MenuItem.objects.create(
        slug="hotcoffee", category=coffee, name="Milk Based Coffee", base_price_cents=500,
        is_published=True,
    )  # fmt: skip
    drink = ModifierGroup.objects.create(
        code="drink", name="Choose your drink", is_required=True, max_select=1
    )
    ModifierOption.objects.create(group=drink, name="Flat White", price_delta_cents=50)
    variation = ModifierGroup.objects.create(code="var", name="Drink variation", max_select=0)
    ModifierOption.objects.create(group=variation, name="OAT ADC", price_delta_cents=80)
    MenuItemModifierGroup.objects.create(menu_item=flat, modifier_group=drink, sort_order=0)
    MenuItemModifierGroup.objects.create(menu_item=flat, modifier_group=variation, sort_order=1)
    burger = MenuItem.objects.create(
        slug="bburger", category=food, name="Breakfast Burger", base_price_cents=2300,
        is_published=True,
    )  # fmt: skip
    return {"coffee": flat, "burger": burger}


@pytest.fixture
def cashier(cafe):
    user = UserFactory(role="staff", first_name="Mia", last_name="Tran", is_staff=True)
    StaffAssignment.objects.create(user=user, location=cafe)
    pins.set_pin(user, "4821")
    user.save()
    return user


@pytest.fixture
def staff_session(till, cafe):
    staff = UserFactory(role="staff", first_name="Finn", last_name="Test", is_staff=True)
    StaffAssignment.objects.create(user=staff, location=cafe)
    pins.set_pin(staff, "7531")
    staff.save(update_fields=["pos_pin"])

    response = post(till, "/staff/verify-pin", {"pin": "7531"})
    assert response.status_code == 200, response.data
    till.credentials(
        HTTP_X_TERMINAL_ID=TERMINAL,
        HTTP_X_POS_STAFF=response.data["staffToken"],
    )
    return staff


@pytest.fixture
def till(cashier):
    client = APIClient()
    client.force_authenticate(cashier)
    client.credentials(HTTP_X_TERMINAL_ID=TERMINAL)
    return client


def line(key="L1", item="hotcoffee", price=6.3, qty=1, **extra):
    sel = {"drink": ["Flat White"], "var": ["OAT ADC"]} if item == "hotcoffee" else {}
    return {"key": key, "itemId": item, "name": item, "price": price, "qty": qty, "sel": sel,
            "adjust": [], "note": "", **extra}  # fmt: skip


def order_body(number="BRADDON-TILL-1-0001", **overrides):
    body = {
        "id": number,
        "channel": "pos",
        "type": "takeaway",
        "status": "new",
        "accepted": True,
        "cafe": "braddon",
        "time": "09:41",
        "placedAt": timezone.now().isoformat(),
        "tables": [],
        "lines": [line(qty=2), line("L2", "bburger", 23)],
        "total": 35.6,
        "paid": False,
        "methods": [],
        "payments": [],
        "note": "",
        "discounts": [],
        "rewardUsed": False,
        "staff": "Mia Tran",
        "history": [{"at": "16 Oct, 9:41 am", "what": "Created on till by Mia Tran"}],
    }
    body.update(overrides)
    return body


def post(client, path, body, key=None, method="post"):
    headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
    return getattr(client, method)(f"{BASE}{path}", body, format="json", **headers)


def create(till, **overrides):
    response = post(till, "/orders", order_body(**overrides), key=f"CMD-{overrides}")
    assert response.status_code == 201, response.json()
    return response.json()


def pay(till, number, amount, ref="P1", **extra):
    body = {"method": "cash", "amount": amount, "tip": 0, "surcharge": 0, "ref": ref,
            "by": "Mia Tran", **extra}  # fmt: skip
    return post(till, f"/orders/{quote(number)}/payments", body)


# ---------------------------------------------------------------- device & staff


def test_customers_and_unassigned_staff_cannot_use_the_till(cafe):
    client = APIClient()
    client.force_authenticate(UserFactory())
    assert client.get(f"{BASE}/locations").status_code == 403
    client.force_authenticate(UserFactory(role="staff"))
    response = client.get(f"{BASE}/locations")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert APIClient().get(f"{BASE}/snapshot").status_code == 401


def test_locations_are_the_cafes_of_the_account(till, cafe, elsewhere):
    assert till.get(f"{BASE}/locations").json() == {
        "results": [{"id": "braddon", "slug": "braddon", "name": "Braddon"}]
    }
    assert till.get(f"{BASE}/snapshot?cafe=civic").status_code == 404
    owner = APIClient()
    owner.force_authenticate(UserFactory(role="owner"))
    assert len(owner.get(f"{BASE}/locations").json()["results"]) == 2


def test_verify_pin_returns_staff_and_permissions(till, cashier, cafe):
    response = post(till, "/staff/verify-pin", {"pin": "4821"})
    assert response.status_code == 200
    assert response.json()["staff"] == {
        "id": str(cashier.pk), "name": "Mia Tran", "initials": "MT", "role": "cashier",
        "cafe": "braddon", "shift": "",
    }  # fmt: skip
    assert "orders.refund" not in response.json()["permissions"]
    assert post(till, "/staff/verify-pin", {"pin": "0000"}).status_code == 204
    assert post(till, "/staff/verify-pin", {"pin": "48"}).status_code == 204


def test_pins_are_unique_and_scoped_to_the_cafes(cafe, elsewhere, cashier):
    other = UserFactory(role="staff")
    StaffAssignment.objects.create(user=other, location=elsewhere)
    with pytest.raises(ValueError, match="already uses"):
        pins.set_pin(other, "4821")
    pins.set_pin(other, "1357")
    other.save()
    assert pins.find_by_pin("1357", [cafe.pk]) is None
    assert pins.find_by_pin("1357", [elsewhere.pk]) == other
    manager = UserFactory(role="manager")
    StaffAssignment.objects.create(user=manager, location=cafe)
    permissions = pins.permissions(manager)
    expected = set(pins.ROLES["manager"]["perms"]) | {"loyalty.stamp", "gift.redeem"}
    assert set(permissions) == expected
    assert len(permissions) == len(expected)
    assert pins.till_role(UserFactory(role="owner")) == "admin"


def test_till_pin_is_set_in_the_bos_workspace(client, cashier):
    owner = UserFactory(role="owner", is_superuser=True, is_staff=True)
    browser = __import__("django.test", fromlist=["Client"]).Client()
    browser.force_login(owner)
    url = reverse("admin:accounts_user_change", args=[cashier.pk])
    page = browser.get(url)
    assert b"Till PIN" in page.content and b"A PIN is set" in page.content
    form = {
        "email": cashier.email, "first_name": "Mia", "last_name": "Tran", "phone": "",
        "role": "staff", "is_active": "on", "till_pin": "2468",
        "assignments-TOTAL_FORMS": "0", "assignments-INITIAL_FORMS": "0",
    }  # fmt: skip
    response = browser.post(url, form)
    assert response.status_code == 302, response.content[:2000]
    cashier.refresh_from_db()
    assert cashier.pos_pin == pins.pin_hash(cashier, "2468")


# ---------------------------------------------------------------- snapshot & catalogue


def test_snapshot_shape_menu_and_staff(till, cafe, menu, cashier):
    LocationMenuItem.objects.create(location=cafe, menu_item=menu["burger"], is_available=False)
    response = till.get(f"{BASE}/snapshot?cafe=braddon")
    assert response.status_code == 200
    snap = response.json()
    assert set(snap) == {
        "locations", "categories", "menu", "cateringPackages", "tables", "promotions",
        "availability", "stock", "stockLog", "orders", "customers", "giftCards",
        "reservations", "catering", "staff", "roles", "settings", "currentShift",
        "closedShifts", "audit", "reports",
    }  # fmt: skip
    coffee = next(item for item in snap["menu"] if item["id"] == "hotcoffee")
    assert coffee["id"] == "hotcoffee" and coffee["price"] == 5
    assert coffee["mods"][0] == {
        "id": "drink", "name": "Choose your drink", "required": True, "max": 1,
        "options": [["Flat White", 0.5]],
    }  # fmt: skip
    assert snap["availability"]["soldOut"] == ["bburger"]  # the account has one café
    assert [s["name"] for s in snap["staff"]] == ["Mia Tran"]
    assert snap["roles"]["cashier"]["perms"][0] == "pos"
    assert snap["settings"]["rules"]["surcharge"] == "2.95"
    assert len(snap["reports"]["days"]) == 7 and snap["currentShift"] is None
    assert till.get(f"{BASE}/snapshot").json()["locations"][0]["id"] == "braddon"


def test_backend_check_catalogue_endpoints(till, cafe, menu):
    assert till.get(f"{BASE}/locations/braddon/categories").json()[0]["id"] == "coffee"
    items = till.get(f"{BASE}/locations/braddon/menu").json()
    item = next(row for row in items if row["id"] == "hotcoffee")
    assert item["priceCents"] == 500 and item["available"] is True
    assert item["modifierGroups"][0]["options"] == [{"name": "Flat White", "priceCents": 50}]


# ---------------------------------------------------------------- orders


def test_create_order_records_lines_and_checks_prices(till, cafe, menu):
    data = create(till)
    assert data["id"] == "BRADDON-TILL-1-0001" and data["status"] == "new"
    assert data["accepted"] and not data["paid"] and data["total"] == 35.6
    assert [ln["key"] for ln in data["lines"]] == ["L1", "L2"]
    order = Order.objects.get(number="BRADDON-TILL-1-0001")
    assert order.channel == "pos" and order.method == "takeaway" and order.customer is None
    assert order.total_cents == 3560 and order.terminal_id == TERMINAL
    assert order.review_note == ""


def test_price_differences_are_recorded_not_rejected(till, cafe, menu):
    body = order_body(lines=[line(price=5.8), line("L2", "unknown-item", 4)], total=9.8)
    response = post(till, "/orders", body)
    assert response.status_code == 201
    note = Order.objects.get().review_note
    assert "rung up at $5.80 (menu $6.30)" in note and "not on the Braddon menu" in note


def test_promo_codes_are_checked_not_refused(till, cafe, elsewhere, menu):
    today = timezone.localdate()
    Promotion.objects.create(code="WELCOME10", label="10% off", kind="pct", percent=10)
    Promotion.objects.create(
        code="SUMMER", label="$5 off", kind="amt", amount_cents=500,
        ends_on=today - timedelta(days=1),
    )  # fmt: skip
    Promotion.objects.create(
        code="CIVIC5", label="$5 off", kind="amt", amount_cents=500, location=elsewhere
    )

    def note(number, discounts, total):
        create(till, id=number, discounts=discounts, total=total)
        return Order.objects.get(number=number).review_note

    assert note("P-1", [{"label": "WELCOME10 · 10% off", "amount": 3.56}], 32.04) == ""
    assert note("P-2", [{"label": "Staff 50%", "amount": 17.8}], 17.8) == ""
    assert note("P-3", [{"label": "10% off", "code": "welcome10", "amount": 10}], 25.6) == (
        "Promo code WELCOME10 took $10.00 off; it allows $3.56."
    )
    assert "Promo code SUMMER ended on" in note("P-4", [{"label": "SUMMER", "amount": 5}], 30.6)
    assert note("P-5", [{"label": "x", "code": "CIVIC5", "amount": 5}], 30.6) == (
        "Promo code CIVIC5 is for Civic only."
    )
    assert note("P-6", [{"label": "x", "code": "NOPE", "amount": 1}], 34.6) == (
        "Promo code NOPE is not in BOS."
    )
    feed = till.get(f"{BASE}/orders", {"cafe": "braddon"}).json()["orders"]
    sent = next(row for row in feed if row["id"] == "P-3")["discounts"]
    assert sent == [{"label": "10% off", "amount": 10.0, "code": "welcome10"}]


def test_totals_must_add_up(till, cafe, menu):
    response = post(till, "/orders", order_body(total=30))
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {"total": "invalid"}
    discounted = order_body(discounts=[{"label": "Staff 50%", "amount": 17.8}], total=17.8)
    assert post(till, "/orders", discounted).status_code == 201
    delivery = order_body("D-1", type="delivery", total=39.16)
    assert post(till, "/orders", delivery).status_code == 201


def test_order_numbers_are_unique_and_retries_replay(till, cafe, menu):
    body = order_body()
    first = post(till, "/orders", body, key="CMD-1")
    again = post(till, "/orders", body, key="CMD-1")
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and Order.objects.count() == 1
    clash = post(till, "/orders", body, key="CMD-2")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "CONFLICT"
    reused = post(till, "/orders", order_body("OTHER-1"), key="CMD-1")
    assert reused.status_code == 409
    assert CommandReceipt.objects.filter(key="CMD-1", status_code=201).count() == 1


@pytest.fixture
def gift_card(db):
    customer = UserFactory(role="customer")
    return GiftCard.objects.create(user=customer, last4="4471", balance_cents=5000)


def test_create_with_a_gift_card_payment(till, cafe, menu, staff_session, gift_card):
    gift = {"method": "gift", "amount": 1000, "tip": 0, "surcharge": 0, "ref": "G1",
            "giftCardId": str(gift_card.pk)}  # fmt: skip
    data = create(till, payments=[gift])
    assert data["payments"][0]["giftLast4"] == "4471" and data["methods"] == ["gift"]
    assert data["payments"][0]["giftCardId"] == str(gift_card.pk)
    gift_card.refresh_from_db()
    assert gift_card.balance_cents == 4000
    spent = gift_card.activity.get()
    assert spent.kind == "spent" and spent.amount_cents == 1000


def test_till_reads_a_customers_gift_card(till, cafe, staff_session, gift_card):
    response = till.get(f"{BASE}/customers/{gift_card.user_id}/gift-card")
    assert response.status_code == 200, response.data
    assert response.json() == {
        "id": str(gift_card.pk),
        "customerId": str(gift_card.user_id),
        "last4": "4471",
        "balance": 5000,
    }
    till.credentials(HTTP_X_TERMINAL_ID=TERMINAL)
    assert till.get(f"{BASE}/customers/{gift_card.user_id}/gift-card").status_code == 403


def test_gift_card_payment_takes_the_balance_once(till, cafe, menu, staff_session, gift_card):
    number = create(till)["id"]
    card = str(gift_card.pk)
    first = pay(till, number, 1500, ref="G1", method="gift", giftCardId=card)
    again = pay(till, number, 1500, ref="G1", method="gift", giftCardId=card)
    assert first.status_code == again.status_code == 200, again.data
    gift_card.refresh_from_db()
    assert gift_card.balance_cents == 3500 and gift_card.activity.count() == 1

    gift_card.balance_cents = 1000
    gift_card.save()
    too_much = pay(till, number, 2000, ref="G2", method="gift", giftCardId=card)
    assert too_much.status_code == 409 and "$10.00 left" in too_much.json()["error"]["message"]
    missing = pay(till, number, 100, ref="G3", method="gift")
    assert missing.status_code == 422
    wrong = pay(till, number, 100, ref="G4", method="gift", giftCardId=card, giftLast4="0000")
    assert wrong.status_code == 422
    gift_card.refresh_from_db()
    assert gift_card.balance_cents == 1000


def test_refund_goes_back_to_the_same_gift_card(till, cafe, menu, refund_session, gift_card):
    number = create(till)["id"]
    card = str(gift_card.pk)
    assert pay(till, number, 2000, ref="G1", method="gift", giftCardId=card).status_code == 200
    assert pay(till, number, 1560, ref="C1", method="card").status_code == 200
    path = f"/orders/{quote(number)}/adjustments"

    def refund(key, alloc, amount):
        return post(till, path, {
            "adjustment": {"key": key, "mode": "refund", "amount": amount, "alloc": alloc},
            "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
            "total": 35.6, "status": "new"})  # fmt: skip

    part = [{"method": "gift", "amount": 500, "ref": "G1", "giftLast4": "4471"}]
    assert refund("R1", part, 500).status_code == 200
    assert refund("R1", part, 500).status_code == 200  # a retry is not refunded twice
    gift_card.refresh_from_db()
    assert gift_card.balance_cents == 3500
    assert gift_card.activity.filter(kind="added").get().amount_cents == 500

    no_alloc = refund("R2", [], 1000)
    assert no_alloc.status_code == 422 and no_alloc.json()["error"]["fields"] == {
        "alloc": "required"
    }

    cancel = post(till, path, {
        "adjustment": {"key": "R3", "mode": "cancel", "amount": 3060},
        "lines": [{"key": "L1", "qty": 0}, {"key": "L2", "qty": 0}],
        "total": 0, "status": "cancelled"})  # fmt: skip
    assert cancel.status_code == 200, cancel.data
    gift_card.refresh_from_db()
    assert gift_card.balance_cents == 5000


def test_payments_are_idempotent_and_capped(till, cafe, menu, staff_session):
    create(till)
    number = "BRADDON-TILL-1-0001"
    first = pay(till, number, 2000, method="card", surcharge=59)
    assert first.status_code == 200 and not first.json()["paid"]
    assert pay(till, number, 2000, method="card", surcharge=59).status_code == 200
    over = pay(till, number, 2000, ref="P2")
    assert over.status_code == 422 and "more than the $15.60 due" in over.json()["error"]["message"]
    done = pay(till, number, 1560, ref="P3", cashPaid=1560, rounding=0, change=440)
    assert done.status_code == 200, done.data
    assert done.json()["paid"] and done.json()["methods"] == ["card", "cash"]
    order = Order.objects.get()
    assert order.payments.count() == 2 and order.surcharge_cents == 59


def test_kitchen_progress_and_status(till, cafe, menu):
    create(till)
    path = f"/orders/{quote('BRADDON-TILL-1-0001')}"
    prep = post(till, f"{path}/preparation",
                {"lines": [{"key": "L1", "prep": "ready"}, {"key": "L2", "prep": "preparing"}],
                 "status": "preparing", "note": "Drinks ready"}, method="put")  # fmt: skip
    assert prep.status_code == 200
    assert [ln.get("prep") for ln in prep.json()["lines"]] == ["ready", "preparing"]
    assert prep.json()["status"] == "preparing"
    assert prep.json()["history"][-1]["what"] == "Drinks ready"
    bad = post(till, f"{path}/preparation", {"lines": [{"key": "X", "prep": "ready"}],
                                             "status": "ready"}, method="put")  # fmt: skip
    assert bad.status_code == 422
    assert post(till, f"{path}/status", {"status": "completed"}).json()["status"] == "completed"
    late = post(till, f"{path}/preparation", {"lines": [], "status": "ready"}, method="put")
    assert late.status_code == 409 and "completed" in late.json()["error"]["message"]
    assert post(till, f"{path}/status", {"status": "ready"}).status_code == 409
    assert post(till, f"{path}/status", {"status": "cancelled"}).status_code == 422


def test_refund_void_and_cancel(till, cafe, menu, refund_session):
    create(till)
    number = "BRADDON-TILL-1-0001"
    path = f"/orders/{quote(number)}/adjustments"
    pay(till, number, 3560)
    void = {
        "adjustment": {"key": "R1", "mode": "void", "amount": 630, "reason": "Entered by mistake",
                       "by": "Mia Tran", "items": {"L1": 1}},
        "lines": [{"key": "L1", "qty": 1}, {"key": "L2", "qty": 1}],
        "total": 29.3,
        "status": "new",
    }  # fmt: skip
    data = post(till, path, void).json()
    assert (
        data["total"] == 29.3
        and data["lines"][0]["qty"] == 1
        and data["refunds"][0]["amount"] == 630
    )
    assert post(till, path, void).status_code == 200  # same key: ignored
    too_much = {**void, "adjustment": {**void["adjustment"], "key": "R2", "mode": "refund",
                                       "amount": 5000}}  # fmt: skip
    assert post(till, path, too_much).status_code == 422
    raise_qty = {**void, "adjustment": {**void["adjustment"], "key": "R3"},
                 "lines": [{"key": "L1", "qty": 5}]}  # fmt: skip
    assert post(till, path, raise_qty).status_code == 422
    cancel = {"adjustment": {"key": "R4", "mode": "cancel", "amount": 2930, "reason": "Customer cancelled"},
              "lines": [], "total": 0, "status": "cancelled"}  # fmt: skip
    data = post(till, path, cancel).json()
    assert data["status"] == "cancelled" and data["lines"] == []
    assert OrderLine.objects.filter(order__number=number).count() == 2  # voided lines are kept
    assert AuditLog.objects.filter(action="pos_cancel").exists()
    assert pay(till, number, 100, ref="P9").status_code == 409


def test_tables(till, cafe, menu):
    create(till, type="dinein", table="T4", tables=["T4"])
    path = f"/orders/{quote('BRADDON-TILL-1-0001')}/tables"
    data = post(till, path, {"table": "T6", "tables": ["T6", "T7"], "cleared": False}, method="put")
    assert data.json()["table"] == "T6" and data.json()["tables"] == ["T6", "T7"]
    assert Order.objects.get().method == "dine_in"


# ---------------------------------------------------------------- app orders on the till


@pytest.fixture
def app_order(cafe, menu):
    customer = UserFactory(first_name="Sophie")
    LoyaltyCard.objects.create(user=customer, stamps=3)
    order = Order.objects.create(
        number="#8020-4471", customer=customer, location=cafe, method="pickup", status="received",
        contact_name="Sophie", contact_phone="0400 123 456", payment_choice="apple_pay",
        payment_label="Apple Pay", payment_reference="demo:1", subtotal_cents=1130,
        total_cents=1130, stamps_earned=2,
    )  # fmt: skip
    OrderLine.objects.create(
        order=order, menu_item=menu["coffee"], item_slug="hotcoffee", name="Milk Based Coffee",
        qty=2, unit_price_cents=565,
    )  # fmt: skip
    return order


def test_app_orders_appear_in_the_feed_and_are_accepted(till, app_order):
    feed = till.get(f"{BASE}/orders?cafe=braddon").json()
    data = feed["orders"][0]
    assert data["id"] == "#8020-4471" and data["channel"] == "app"
    assert data["status"] == "new" and data["accepted"] is False and data["paid"] is True
    assert data["payments"][0]["method"] == "wallet" and data["total"] == 11.3
    assert data["type"] == "pickup" and data["extRef"] == str(app_order.pk)
    path = f"/orders/{quote('#8020-4471')}"
    blocked = post(till, f"{path}/preparation", {"lines": [], "status": "preparing"}, method="put")
    assert (
        blocked.status_code == 409
        and "waiting to be accepted" in blocked.json()["error"]["message"]
    )
    accepted = post(till, f"{path}/accept", {}).json()
    assert accepted["status"] == "preparing" and accepted["accepted"]
    assert AppNotification.objects.filter(user=app_order.customer).exists()
    later = till.get(f"{BASE}/orders?cafe=braddon&since={quote(feed['cursor'])}").json()
    assert [o["id"] for o in later["orders"]] == ["#8020-4471"]


def test_cancelling_an_app_order_returns_stamps(till, app_order, refund_session):
    cancel = {"adjustment": {"key": "R1", "mode": "cancel", "amount": 0, "reason": "Café too busy"},
              "lines": [], "total": 0, "status": "cancelled"}  # fmt: skip
    response = post(till, f"/orders/{quote('#8020-4471')}/adjustments", cancel)
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    app_order.refresh_from_db()
    assert app_order.status == "declined"
    assert LoyaltyCard.objects.get(user=app_order.customer).stamps == 1
    app = APIClient()
    app.force_authenticate(app_order.customer)
    mine = app.get("/api/v1/app/me/orders").json()["items"]
    assert mine[0]["status"] == "declined"


def test_cancel_and_refund_app_orders_as_the_till_sends_them(till, app_order, refund_session):
    # What the till really sends: its view of the order (gift card share counted) and the
    # whole paid amount back (ledger.refundableC), lines unchanged.
    app_order.gift_card_cents, app_order.total_cents = 300, 830
    app_order.save()
    path = f"/orders/{quote('#8020-4471')}"
    seen = till.get(f"{BASE}/orders?cafe=braddon").json()["orders"][0]
    assert seen["total"] == 11.3 and seen["paid"] is True and seen["stamped"] is False
    assert pay(till, "#8020-4471", 100).status_code == 422  # nothing is due
    lines = [{"key": row["key"], "qty": row["qty"]} for row in seen["lines"]]
    too_much = {"adjustment": {"key": "R0", "mode": "refund", "amount": 1131},
                "lines": lines, "total": 11.3, "status": "new"}  # fmt: skip
    assert post(till, f"{path}/adjustments", too_much).status_code == 422
    cancel = {"adjustment": {"key": "R1", "mode": "cancel", "amount": 1130, "reason": "Café too busy"},
              "lines": lines, "total": 11.3, "status": "cancelled"}  # fmt: skip
    response = post(till, f"{path}/adjustments", cancel)
    data = response.json()
    assert response.status_code == 200 and data["status"] == "cancelled", data
    assert data["refunds"][0]["amount"] == 1130
    app_order.refresh_from_db()
    assert app_order.status == "declined" and app_order.total_cents == 830
    assert LoyaltyCard.objects.get(user=app_order.customer).stamps == 1
    assert GiftCard.objects.get(user=app_order.customer).balance_cents == 300


def test_only_pin_checks_are_rate_limited(monkeypatch):
    monkeypatch.setattr(PosView, "throttle_classes", [PersistentThrottle])
    assert OrdersView().get_throttles() == [] and SnapshotView().get_throttles() == []
    assert len(VerifyPinView().get_throttles()) == 1


def test_till_orders_do_not_show_in_the_customer_app(till, cafe, menu, app_order):
    create(till, customerId=str(app_order.customer_id))
    assert Order.objects.get(number="BRADDON-TILL-1-0001").customer == app_order.customer
    app = APIClient()
    app.force_authenticate(app_order.customer)
    assert [o["number"] for o in app.get("/api/v1/app/me/orders").json()["items"]] == ["#8020-4471"]


def test_feed_cursor_only_returns_changes(till, cafe, menu):
    create(till)
    first = till.get(f"{BASE}/orders?cafe=braddon").json()
    Order.objects.update(updated_at=timezone.now() - timedelta(minutes=5))
    quiet = till.get(f"{BASE}/orders?cafe=braddon&since={quote(first['cursor'])}").json()
    assert quiet["orders"] == []
    assert till.get(f"{BASE}/orders?cafe=braddon&since=yesterday").status_code == 422


def test_reads_do_not_grow_queries_with_rows(till, cafe, menu):
    def count(path):
        with CaptureQueriesContext(connection) as ctx:
            assert till.get(path).status_code == 200
        return len(ctx)

    create(till)
    feed, snap = count(f"{BASE}/orders?cafe=braddon"), count(f"{BASE}/snapshot?cafe=braddon")
    for n in range(2, 6):
        number = f"BRADDON-TILL-1-000{n}"
        create(till, id=number)
        pay(till, number, 1000, ref=f"P{n}")
    assert count(f"{BASE}/orders?cafe=braddon") == feed
    assert count(f"{BASE}/snapshot?cafe=braddon") == snap


# ---------------------------------------------------------------- cash & records


def shift_body(**overrides):
    body = {"id": "SH1", "cafe": "braddon", "staffId": "x", "staffName": "Mia Tran",
            "openedAt": "16 Oct, 6:02 am", "float": 20000, "cashSales": 0, "cashRefunds": 0,
            "moves": [], "status": "open"}  # fmt: skip
    body.update(overrides)
    return body


def test_cash_shift_open_move_close(till, cafe, refund_session):
    assert post(till, "/shifts", shift_body()).status_code == 201
    assert post(till, "/shifts", shift_body(id="SH2")).status_code == 409

    move = {"id": "M1", "type": "out", "amount": 4500, "reason": "Banking drop", "by": "Mia"}
    assert len(post(till, "/shifts/SH1/moves", move).json()["moves"]) == 1
    assert len(post(till, "/shifts/SH1/moves", move).json()["moves"]) == 1

    snapshot = till.get(f"{BASE}/snapshot?cafe=braddon").json()
    assert snapshot["currentShift"]["id"] == "SH1"

    close = shift_body(
        status="closed",
        cashSales=0,
        cashRefunds=0,
        moves=[move],
        closedAt="3:08 pm",
        closedBy="Mia",
        counted=15480,
        expected=15500,
        variance=-20,
    )
    wrong = {**close, "expected": 15000, "variance": 480}
    assert post(till, "/shifts/SH1", wrong, method="put").status_code == 422

    response = post(till, "/shifts/SH1", close, method="put")
    assert response.status_code == 200, response.data
    closed = response.json()
    assert closed["status"] == "closed"
    assert closed["variance"] == -20
    shift = CashShift.objects.get()
    actor = refund_session.get_full_name().strip() or refund_session.email
    assert shift.expected_cents == 15500
    assert shift.closed_by == actor
    assert shift.approved_by == actor
    assert shift.updated_by_id == refund_session.pk

    snapshot = till.get(f"{BASE}/snapshot?cafe=braddon").json()
    assert snapshot["currentShift"] is None
    assert snapshot["closedShifts"][0]["id"] == "SH1"


def test_transactions_and_audit_are_kept_once(till, cafe):
    record = {"kind": "payment", "orderId": "BRADDON-TILL-1-0001", "cafeId": "braddon",
              "terminalId": TERMINAL, "amountC": 3560, "gstC": 324,
              "at": timezone.now().isoformat(), "staff": "Mia", "reference": "P1"}  # fmt: skip
    assert post(till, "/transactions", record).json() == {"ok": True}
    assert post(till, "/transactions", record).status_code == 200
    assert TillTransaction.objects.count() == 1
    entry = {"id": "A1", "at": "9:41 am", "ts": 1, "staff": "Mia", "role": "Cashier",
             "action": "Discount applied", "detail": "Staff 50%", "reason": "", "approver": "",
             "amount": -1780, "area": "Till", "cafe": "braddon"}  # fmt: skip
    post(till, "/audit", entry)
    post(till, "/audit", entry)
    assert AuditLog.objects.filter(entity_type="pos.audit").count() == 1
    assert (
        till.get(f"{BASE}/snapshot?cafe=braddon").json()["audit"][0]["action"] == "Discount applied"
    )
    other = {**entry, "id": "A2", "cafe": "civic"}
    assert post(till, "/audit", other).status_code == 404


def test_pos_docs_page_is_published(client):
    assert client.get("/api/docs/pos/").status_code == 200
    schema = client.get("/api/docs/pos/schema/").content.decode()
    assert "/api/v1/pos/orders" in schema and "/api/v1/auth/login" in schema
    assert "/api/v1/app/" not in schema


def test_bos_pages_show_till_orders_shifts_and_transactions(till, cafe, menu, staff_session):
    from django.test import Client

    create(till, lines=[line(price=5.8)], total=5.8)
    opened = post(till, "/shifts", shift_body())
    assert opened.status_code == 201, opened.data
    record = {"kind": "payment", "cafeId": "braddon", "amountC": 580, "gstC": 53,
              "at": timezone.now().isoformat(), "reference": "P1"}  # fmt: skip
    post(till, "/transactions", record)
    manager = UserFactory(role="manager", is_staff=True)
    StaffAssignment.objects.create(user=manager, location=cafe)
    browser = Client()
    browser.force_login(manager)
    order = Order.objects.get()
    page = browser.get(reverse("admin:orders_order_change", args=[order.pk]))
    assert page.status_code == 200 and b"Check prices" in page.content
    assert b"rung up at $5.80" in page.content
    for name in ["orders_order", "pos_cashshift", "pos_tilltransaction"]:
        assert browser.get(reverse(f"admin:{name}_changelist")).status_code == 200
    shift = CashShift.objects.get()
    assert browser.get(reverse("admin:pos_cashshift_change", args=[shift.pk])).status_code == 200


def test_any_origin_and_host_may_call_the_api(client, settings):
    settings.ALLOWED_HOSTS = ["*"]
    response = client.options(
        f"{BASE}/locations",
        HTTP_ORIGIN="http://192.168.1.20:5173",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        HTTP_HOST="pos-preview.example.net",
    )
    assert response.status_code == 200
    assert response["access-control-allow-origin"] == "*"


# ---------------------------------------------------------------- phase 2: customers & bookings


@pytest.fixture
def regular(cafe):
    from apps.accounts.models import CustomerProfile

    user = UserFactory(first_name="Sophie", last_name="Nguyen", phone="0400 123 456")
    CustomerProfile.objects.create(user=user, preferred_location=cafe, notes="Oat milk")
    LoyaltyCard.objects.create(user=user, stamps=9)
    return user


def test_snapshot_lists_customers_with_their_coffee_card(till, cafe, menu, regular):
    people = till.get(f"{BASE}/snapshot?cafe=braddon").json()["customers"]
    assert people[0]["id"] == str(regular.pk) and people[0]["name"] == "Sophie Nguyen"
    assert (people[0]["stamps"], people[0]["rewards"]) == (0, 1)
    assert people[0]["homeCafe"] == "braddon" and people[0]["notes"] == "Oat milk"


def test_customer_edit_and_coffee_card_from_the_till(till, regular, refund_session):
    path = f"/customers/{regular.pk}"
    response = post(
        till,
        path,
        {"name": "Sophie N", "dietary": ["Vegan"], "notes": "Allergic to nuts"},
        method="put",
    )
    assert response.status_code == 200, response.data
    edited = response.json()
    assert edited["name"] == "Sophie N" and edited["dietary"] == ["Vegan"]
    other = UserFactory(email="taken@example.com")
    assert post(till, path, {"email": other.email}, method="put").status_code == 409
    redeemed = post(till, f"{path}/loyalty", {"stamps": 0, "rewards": 0, "reason": "Free coffee"},
                    method="put").json()  # fmt: skip
    assert (redeemed["stamps"], redeemed["rewards"]) == (0, 0)
    assert LoyaltyCard.objects.get(user=regular).stamps == 0
    stamped = post(till, f"{path}/loyalty", {"stamps": 2, "rewards": 0}, method="put").json()
    assert stamped["stamps"] == 2
    assert LoyaltyCard.objects.get(user=regular).activity.filter(type="earn").exists()
    assert AppNotification.objects.filter(user=regular, kind="loyalty").exists()
    full = post(till, f"{path}/loyalty", {"stamps": 3, "rewards": 2}, method="put").json()
    assert (full["stamps"], full["rewards"]) == (3, 2)
    assert post(till, "/customers/not-a-uuid/loyalty", {"stamps": 1, "rewards": 0},
                method="put").status_code == 404  # fmt: skip


def test_sold_out_from_the_till(till, cafe, elsewhere, menu):
    path = "/items/bburger/sold-out"
    assert post(till, path, {"soldOut": True}, method="put").json() == {"ok": True}
    assert not LocationMenuItem.objects.get(location=cafe, menu_item=menu["burger"]).is_available
    assert not LocationMenuItem.objects.filter(location=elsewhere).exists()  # not this account's
    snap = till.get(f"{BASE}/snapshot?cafe=braddon").json()["availability"]
    assert snap["soldOut"] == ["bburger"] and snap["branchOff"] == {"braddon": []}
    on = post(
        till, "/locations/braddon/items/bburger/availability", {"available": True}, method="put"
    )
    assert on.status_code == 200
    assert LocationMenuItem.objects.get(location=cafe, menu_item=menu["burger"]).is_available
    assert post(till, "/items/nope/sold-out", {"soldOut": True}, method="put").status_code == 404
    assert post(till, "/locations/civic/items/bburger/availability", {"available": False},
                method="put").status_code == 404  # fmt: skip


def reservation_body(**overrides):
    body = {"id": "rK2M9QZ1A", "cafe": "braddon", "date": str(timezone.localdate() + timedelta(days=1)),
            "time": "09:30", "guests": 4, "name": "Hannah Lee", "phone": "0412 345 678",
            "notes": "High chair", "status": "pending"}  # fmt: skip
    body.update(overrides)
    return body


def test_reservations_from_the_till(till, cafe, regular):
    from apps.bookings.models import Booking
    from apps.notifications.models import EmailMessage

    path = "/reservations/rK2M9QZ1A"
    made = post(till, path, reservation_body(customerId=str(regular.pk)), method="put").json()
    assert made["status"] == "pending" and made["source"] == "pos"
    booking = Booking.objects.get(reference="rK2M9QZ1A")
    assert booking.source == "staff" and booking.customer == regular and booking.party_size == 4
    seated = post(till, path, reservation_body(status="completed", guests=5), method="put").json()
    assert seated["status"] == "completed" and seated["guests"] == 5
    booking.refresh_from_db()
    assert booking.status == "seated"
    assert not EmailMessage.objects.filter(recipient="").exists()  # no email → no mail
    assert post(till, path, reservation_body(status="pending"), method="put").status_code == 409
    assert post(till, path, reservation_body(id="other"), method="put").status_code == 422
    snap = till.get(f"{BASE}/snapshot?cafe=braddon").json()["reservations"]
    assert [r["id"] for r in snap] == ["rK2M9QZ1A"]


def test_website_bookings_show_and_confirm_on_the_till(till, cafe):
    from apps.bookings.models import Booking

    web = Booking.objects.create(
        location=cafe, date=timezone.localdate() + timedelta(days=2), time="10:00", party_size=2,
        name="Web Guest", email="guest@example.com", phone="0400", source="web",
    )  # fmt: skip
    row = next(r for r in till.get(f"{BASE}/snapshot?cafe=braddon").json()["reservations"])
    assert row["id"] == web.reference and row["source"] == "web" and row["status"] == "pending"
    body = {**row, "status": "confirmed"}
    assert (
        post(till, f"/reservations/{web.reference}", body, method="put").json()["status"]
        == "confirmed"
    )
    web.refresh_from_db()
    assert web.status == "confirmed" and web.handled_by is not None


def test_catering_from_the_till(till, cafe):
    from apps.enquiries.models import CateringPackage, Enquiry

    CateringPackage.objects.create(slug="grazing", name="Grazing Box", price_cents=2200)
    body = {"id": "ceQ2M8ZK1", "packageId": "Grazing Box", "guests": 20, "date": "2026-11-02",
            "time": "11:30", "fulfil": "delivery", "address": "1 Office St", "cafe": "",
            "name": "Acme Pty Ltd", "contact": "Jo · 0400 111 222", "dietary": ["2 vegan"],
            "notes": "Leave at reception", "quote": 484, "status": "enquiry"}  # fmt: skip
    made = post(till, "/catering/ceQ2M8ZK1", body, method="put").json()
    assert made["source"] == "pos" and made["quote"] == 484 and made["fulfil"] == "delivery"
    enquiry = Enquiry.objects.get(reference="ceQ2M8ZK1")
    assert enquiry.kind == "catering" and enquiry.headcount == 20 and enquiry.status == "new"
    won = post(till, "/catering/ceQ2M8ZK1", {**body, "status": "confirmed"}, method="put").json()
    assert won["status"] == "confirmed"
    enquiry.refresh_from_db()
    assert enquiry.status == "won" and enquiry.payload["address"] == "1 Office St"
    snap = till.get(f"{BASE}/snapshot?cafe=braddon").json()["catering"]
    assert snap[0]["id"] == "ceQ2M8ZK1" and snap[0]["packageId"] == "Grazing Box"


# ---------------------------------------------------------------- BOS sales report


def test_bos_reports_show_sales_from_app_and_till_orders(
    till, cafe, menu, app_order, refund_session
):
    from django.test import Client

    create(till)  # $35.60 till order
    number = "BRADDON-TILL-1-0001"
    pay(till, number, 3560, method="card", surcharge=105)
    refund = {"adjustment": {"key": "R1", "mode": "refund", "amount": 630, "reason": "Wrong item"},
              "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}], "total": 35.6,
              "status": "new"}  # fmt: skip
    response = post(till, f"/orders/{quote(number)}/adjustments", refund)
    assert response.status_code == 200, response.data

    response = post(till, f"/orders/{quote('#8020-4471')}/accept", {})
    assert response.status_code == 200, response.data
    create(till, id="CANCELLED-1", status="new")
    cancel = {"adjustment": {"key": "R2", "mode": "cancel", "amount": 0, "reason": "x"},
              "lines": [], "total": 0, "status": "cancelled"}  # fmt: skip
    response = post(till, "/orders/CANCELLED-1/adjustments", cancel)
    assert response.status_code == 200, response.data
    owner = UserFactory(role="owner", is_superuser=True, is_staff=True)
    browser = Client()
    browser.force_login(owner)
    page = browser.get(reverse("admin:bos_reports"))
    assert page.status_code == 200
    sales = page.context["sales"]
    assert sales["total"] == 3560 + 1130 and sales["orders"] == 2
    assert sales["returned"] == 630
    assert {row["label"]: row["value"] for row in sales["tenders"]} == {
        "Card": 3560,
        "Wallet": 1130,
    }
    assert {row["label"] for row in sales["channels"]} == {"Till", "App"}
    assert sales["items"][0]["label"] == "Milk Based Coffee"  # app + till lines, menu name
    assert sales["items"][0]["value"] == 4
    assert b"Sales over time" in page.content and b"$46.90" in page.content
    csv = browser.get(reverse("admin:bos_reports") + "?export=orders").content.decode()
    assert "BRADDON-TILL-1-0001" in csv and "#8020-4471" in csv and "CANCELLED-1" not in csv


# ---------------------------------------------------------------- tables, promo codes, dashboard


def test_snapshot_tables_and_promo_codes(till, cafe, elsewhere):
    from apps.pos.models import Promotion, TillTable

    TillTable.objects.create(location=cafe, label="T2", seats=2, zone="Window", sort_order=2)
    TillTable.objects.create(location=cafe, label="T1", seats=4, zone="Window", sort_order=1)
    TillTable.objects.create(location=cafe, label="T9", is_active=False)
    TillTable.objects.create(location=elsewhere, label="C1")
    today = timezone.localdate()
    Promotion.objects.create(code="welcome10", label="10% off", kind="pct", percent=10)
    Promotion.objects.create(code="LOCAL5", label="$5 off", kind="amt", amount_cents=500,
                             location=cafe)  # fmt: skip
    Promotion.objects.create(code="CIVIC", label="x", kind="pct", percent=5, location=elsewhere)
    Promotion.objects.create(code="OLD", label="x", kind="pct", percent=5,
                             ends_on=today - timedelta(days=1))  # fmt: skip
    Promotion.objects.create(code="OFF", label="x", kind="pct", percent=5, is_active=False)
    snap = till.get(f"{BASE}/snapshot?cafe=braddon").json()
    assert snap["tables"] == [
        {"id": "T1", "seats": 4, "zone": "Window"},
        {"id": "T2", "seats": 2, "zone": "Window"},
    ]
    assert snap["promotions"] == [
        {"code": "LOCAL5", "kind": "amt", "value": 5, "label": "$5 off"},
        {"code": "WELCOME10", "kind": "pct", "value": 10, "label": "10% off"},
    ]


def test_bos_tables_and_promo_codes_pages(cafe, elsewhere):
    from django.test import Client

    from apps.pos.models import Promotion, TillTable

    manager = UserFactory(role="manager", is_staff=True)
    StaffAssignment.objects.create(user=manager, location=cafe)
    TillTable.objects.create(location=elsewhere, label="C1")
    browser = Client()
    browser.force_login(manager)
    added = browser.post(
        reverse("admin:pos_tilltable_add"),
        {"location": cafe.pk, "label": "T1", "seats": 4, "zone": "Main", "sort_order": 0,
         "is_active": "on"},
    )  # fmt: skip
    assert added.status_code == 302, added.content[:1500]
    page = browser.get(reverse("admin:pos_tilltable_changelist"))
    assert page.status_code == 200
    labels = {table.label for table in page.context["cl"].result_list}
    assert "T1" in labels and "C1" not in labels  # only their café's floor plan
    assert browser.get(reverse("admin:pos_promotion_add")).status_code == 403  # chain owner only
    owner = UserFactory(role="owner", is_superuser=True, is_staff=True)
    browser.force_login(owner)
    made = browser.post(
        reverse("admin:pos_promotion_add"),
        {"code": " local5 ", "label": "$5 off", "is_active": "on", "kind": "amt",
         "amount_cents": "5.00", "location": ""},
    )  # fmt: skip
    assert made.status_code == 302, made.content[:1500]
    promo = Promotion.objects.get()
    assert promo.code == "LOCAL5" and promo.amount_cents == 500
    missing = browser.post(
        reverse("admin:pos_promotion_add"),
        {"code": "PCT", "label": "x", "kind": "pct", "location": ""},
    )
    assert missing.status_code == 200 and b"Enter the percentage off" in missing.content


def test_dashboard_figures_come_from_real_records(till, cafe, menu, regular, app_order):
    from apps.bookings.models import Booking
    from apps.enquiries.models import CateringPackage, Enquiry
    from apps.loyalty import services as loyalty

    card = LoyaltyCard.objects.get(user=regular)
    card.stamps = 3
    card.save()
    loyalty.record_stamp_change(card, 1, location_name="Braddon")  # +2 stamps
    card.stamps = 0
    card.save()
    loyalty.record_stamp_change(card, 3, location_name="Braddon")  # a correction, no stamps
    today = timezone.localdate()
    for status, size in [("seated", 4), ("no_show", 2), ("cancelled", 6)]:
        Booking.objects.create(location=cafe, date=today, time="09:00", party_size=size,
                               name="G", email="", phone="", status=status)  # fmt: skip
    package = CateringPackage.objects.create(slug="box", name="Box", price_cents=2000)
    Enquiry.objects.create(kind="catering", name="A", email="", message="", status="won",
                           package=package, headcount=10, location=cafe)  # fmt: skip
    Enquiry.objects.create(kind="catering", name="B", email="", message="", location=cafe)
    create(till)
    reports = till.get(f"{BASE}/snapshot?cafe=braddon").json()["reports"]
    assert reports["loyalty"]["stamps"] == 2
    assert reports["reservations"] == {"week": 2, "covers": 6, "noShow": 1, "avgParty": 3.0}
    assert reports["catering"] == {"enquiries": 2, "confirmed": 1, "revenue": 200}
    assert reports["loyaltyActivity"][0]["what"].endswith("Sophie Nguyen")
    assert reports["loyaltyActivity"][0]["when"].startswith("Today")
    assert reports["days"][-1]["sales"] == 35.6  # till order; the app order is not accepted
    assert reports["salesTodayComparison"] == ""  # nothing sold a week ago to compare with


@pytest.mark.parametrize(
    "changed",
    [
        {},
        {"amount": 3500},
        {"method": "card"},
        {"tip": 100},
    ],
)
def test_payment_reference_checks_retry_details(till, cafe, menu, changed, staff_session):
    created = create(till)
    number = created["id"]

    first = pay(till, number, 3560, ref="RETRY-CHECK-001")
    assert first.status_code in (200, 201), first.data

    body = {
        "method": "cash",
        "amount": 3560,
        "tip": 0,
        "surcharge": 0,
        "ref": "RETRY-CHECK-001",
        "by": "Mia Tran",
        **changed,
    }

    response = post(
        till,
        f"/orders/{quote(number)}/payments",
        body,
    )

    if changed:
        assert response.status_code == 409, response.data
    else:
        assert response.status_code in (200, 201), response.data

    order = Order.objects.get(number=number)
    assert order.payments.count() == 1

    payment = order.payments.get()
    assert payment.method == "cash"
    assert payment.amount_cents == 3560
    assert payment.tip_cents == 0


@pytest.mark.parametrize(
    "changed",
    [
        {"amount": 5000},
        {"type": "in"},
        {"reason": "Changed reason"},
    ],
)
def test_cash_move_reference_rejects_changed_details(till, cafe, changed, refund_session):
    opened = post(till, "/shifts", shift_body())
    assert opened.status_code == 201, opened.data

    move = {
        "id": "MOVE-CONFLICT-001",
        "type": "out",
        "amount": 4500,
        "reason": "Banking drop",
        "by": "Mia",
    }

    first = post(till, "/shifts/SH1/moves", move)
    assert first.status_code == 200, first.data

    retry = post(till, "/shifts/SH1/moves", move)
    assert retry.status_code == 200, retry.data

    conflict = post(till, "/shifts/SH1/moves", {**move, **changed})
    assert conflict.status_code == 409, conflict.data

    shift = CashShift.objects.get(reference="SH1")
    assert len(shift.moves) == 1
    actor = refund_session.get_full_name().strip() or refund_session.email
    assert shift.moves[0] == {**move, "by": actor, "approver": actor, "at": ""}
    assert shift.updated_by_id == refund_session.pk


def test_payment_is_linked_to_requested_shift(till, cafe, menu, staff_session):
    opened = post(till, "/shifts", shift_body())
    assert opened.status_code == 201, opened.data
    created = create(till)

    paid = pay(till, created["id"], 3560, ref="SHIFT-PAY-001", shiftId="SH1")
    assert paid.status_code == 200, paid.data

    order = Order.objects.get(number=created["id"])
    payment = order.payments.get()
    assert payment.shift.reference == "SH1"

    retry = pay(till, created["id"], 3560, ref="SHIFT-PAY-001", shiftId="SH1")
    assert retry.status_code == 200, retry.data
    assert order.payments.count() == 1

    changed = pay(till, created["id"], 3560, ref="SHIFT-PAY-001", shiftId="OTHER-SHIFT")
    assert changed.status_code == 409, changed.data


def test_payment_rejects_unknown_shift(till, cafe, menu, staff_session):
    created = create(till)

    response = pay(till, created["id"], 3560, ref="SHIFT-PAY-002", shiftId="MISSING")
    assert response.status_code == 409, response.data
    assert not Order.objects.get(number=created["id"]).payments.exists()


def test_payment_rejects_shift_from_another_terminal(till, cafe, menu, staff_session):
    opened = post(till, "/shifts", shift_body())
    assert opened.status_code == 201, opened.data
    created = create(till)

    till.credentials(HTTP_X_TERMINAL_ID="OTHER-TILL")
    login = post(till, "/staff/verify-pin", {"pin": "7531"})
    assert login.status_code == 200, login.data

    till.credentials(
        HTTP_X_TERMINAL_ID="OTHER-TILL",
        HTTP_X_POS_STAFF=login.data["staffToken"],
    )
    response = pay(till, created["id"], 3560, ref="SHIFT-PAY-003", shiftId="SH1")
    assert response.status_code == 409, response.data

    order = Order.objects.get(number=created["id"])
    assert not order.payments.exists()


def test_refund_is_linked_to_shift_and_rejects_changed_amount(till, cafe, menu, refund_session):
    opened = post(till, "/shifts", shift_body())
    assert opened.status_code == 201, opened.data
    created = create(till)
    number = created["id"]

    paid = pay(till, number, 3560, ref="REFUND-PAY-001", shiftId="SH1")
    assert paid.status_code == 200, paid.data

    body = {
        "adjustment": {
            "key": "REFUND-SHIFT-001",
            "mode": "refund",
            "amount": 3560,
            "alloc": [{"method": "cash", "amount": 3560, "ref": "REFUND-PAY-001"}],
            "reason": "Customer requested a refund",
            "by": "Mia Tran",
            "shiftId": "SH1",
        },
        "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": 35.6,
        "status": "refunded",
    }
    path = f"/orders/{quote(number)}/adjustments"

    response = post(till, path, body)
    assert response.status_code == 200, response.data

    order = Order.objects.get(number=number)
    assert order.adjustments.get().shift.reference == "SH1"

    retry = post(till, path, body)
    assert retry.status_code == 200, retry.data
    assert order.adjustments.count() == 1

    changed_adjustment = {
        **body["adjustment"],
        "amount": 3500,
        "alloc": [{"method": "cash", "amount": 3500, "ref": "REFUND-PAY-001"}],
    }
    changed = {**body, "adjustment": changed_adjustment}
    conflict = post(till, path, changed)
    assert conflict.status_code == 409, conflict.data
    assert order.adjustments.get().amount_cents == 3560


@pytest.mark.parametrize(
    "allocations",
    [
        [{"method": "cash", "amount": 3500, "ref": "PAY-ALLOC-001"}],
        [{"method": "cash", "amount": -100, "ref": "PAY-ALLOC-001"}],
        [{"method": "invalid", "amount": 3560, "ref": "PAY-ALLOC-001"}],
        [
            {"method": "cash", "amount": 1780, "ref": "PAY-ALLOC-001"},
            {"method": "cash", "amount": 1780, "ref": "PAY-ALLOC-001"},
        ],
    ],
)
def test_refund_rejects_invalid_allocations(till, cafe, menu, allocations, refund_session):
    created = create(till)
    number = created["id"]
    paid = pay(till, number, 3560, ref="PAY-ALLOC-001")
    assert paid.status_code == 200, paid.data

    body = {
        "adjustment": {
            "key": "REFUND-ALLOC-001",
            "mode": "refund",
            "amount": 3560,
            "alloc": allocations,
        },
        "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": 35.6,
        "status": "refunded",
    }

    response = post(till, f"/orders/{quote(number)}/adjustments", body)
    assert response.status_code == 422, response.data
    assert not Order.objects.get(number=number).adjustments.exists()


@pytest.mark.parametrize(
    "allocations",
    [
        [{"method": "cash", "amount": 3560, "ref": "UNKNOWN"}],
        [{"method": "card", "amount": 3560, "ref": "CASH-ORIGINAL"}],
        [
            {"method": "cash", "amount": 1001, "ref": "CASH-ORIGINAL"},
            {"method": "card", "amount": 2559, "ref": "CARD-ORIGINAL"},
        ],
    ],
)
def test_refund_allocations_must_match_original_payments(
    till, cafe, menu, allocations, refund_session
):
    created = create(till)
    number = created["id"]

    cash = pay(till, number, 1000, ref="CASH-ORIGINAL")
    assert cash.status_code == 200, cash.data
    card = pay(till, number, 2560, ref="CARD-ORIGINAL", method="card")
    assert card.status_code == 200, card.data

    body = {
        "adjustment": {
            "key": "REFUND-ORIGINAL-CHECK",
            "mode": "refund",
            "amount": 3560,
            "alloc": allocations,
        },
        "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": 35.6,
        "status": "refunded",
    }

    response = post(till, f"/orders/{quote(number)}/adjustments", body)
    assert response.status_code == 422, response.data

    order = Order.objects.get(number=number)
    assert not order.adjustments.exists()
    assert order.status == "confirmed"


@pytest.mark.parametrize("second_amount, expected_status", [(400, 200), (401, 422)])
def test_refund_uses_remaining_balance_per_payment(
    till, cafe, menu, second_amount, expected_status, refund_session
):
    created = create(till)
    number = created["id"]

    cash = pay(till, number, 1000, ref="CASH-PARTIAL")
    assert cash.status_code == 200, cash.data
    card = pay(till, number, 2560, ref="CARD-PARTIAL", method="card")
    assert card.status_code == 200, card.data

    path = f"/orders/{quote(number)}/adjustments"

    def refund_body(key, amount):
        return {
            "adjustment": {
                "key": key,
                "mode": "refund",
                "amount": amount,
                "alloc": [{"method": "cash", "amount": amount, "ref": "CASH-PARTIAL"}],
                "reason": "Partial refund",
            },
            "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
            "total": 35.6,
            "status": "new",
        }

    first = post(till, path, refund_body("PARTIAL-REFUND-1", 600))
    assert first.status_code == 200, first.data

    second = post(till, path, refund_body("PARTIAL-REFUND-2", second_amount))
    assert second.status_code == expected_status, second.data

    order = Order.objects.get(number=number)
    refunds = list(order.adjustments.order_by("created_at"))
    assert len(refunds) == (2 if expected_status == 200 else 1)
    assert sum(refund.amount_cents for refund in refunds) == (
        1000 if expected_status == 200 else 600
    )


def test_close_shift_checks_sales_against_recorded_payments(till, cafe, menu, refund_session):
    opened = post(till, "/shifts", shift_body())
    assert opened.status_code == 201, opened.data
    created = create(till)

    paid = pay(till, created["id"], 3560, ref="CLOSE-PAY-001", shiftId="SH1")
    assert paid.status_code == 200, paid.data

    close = shift_body(
        status="closed",
        cashSales=3560,
        cashRefunds=0,
        closedAt="3:00 pm",
        closedBy="Mia",
        counted=23560,
        expected=23560,
        variance=0,
    )
    forged = {**close, "cashSales": 0, "expected": 20000, "variance": 3560}

    rejected = post(till, "/shifts/SH1", forged, method="put")
    assert rejected.status_code == 422, rejected.data
    assert CashShift.objects.get(reference="SH1").status == "open"

    accepted = post(till, "/shifts/SH1", close, method="put")
    assert accepted.status_code == 200, accepted.data

    shift = CashShift.objects.get(reference="SH1")
    assert shift.cash_sales_cents == 3560
    assert shift.expected_cents == 23560
    assert shift.variance_cents == 0


@pytest.mark.parametrize("terminal", ["OTHER-TILL", ""])
def test_cash_shift_rejects_other_or_missing_terminal(till, cafe, terminal, refund_session):
    opened = post(till, "/shifts", shift_body())
    assert opened.status_code == 201, opened.data

    headers = dict(till._credentials)
    headers["HTTP_X_TERMINAL_ID"] = terminal
    till.credentials(**headers)
    move = {
        "id": "WRONG-TILL-MOVE",
        "type": "out",
        "amount": 1000,
        "reason": "Cash out",
        "by": "Mia",
    }

    moved = post(till, "/shifts/SH1/moves", move)
    assert moved.status_code == 409, moved.data

    close = shift_body(
        status="closed",
        cashSales=0,
        cashRefunds=0,
        counted=20000,
        expected=20000,
        variance=0,
        closedAt="3:00 pm",
        closedBy="Mia",
    )
    closed = post(till, "/shifts/SH1", close, method="put")
    assert closed.status_code == 409, closed.data

    shift = CashShift.objects.get(reference="SH1")
    assert shift.status == "open"
    assert shift.moves == []


def test_closed_shift_retry_still_checks_terminal(till, cafe, refund_session):
    opened = post(till, "/shifts", shift_body())
    assert opened.status_code == 201, opened.data

    close = shift_body(
        status="closed",
        cashSales=0,
        cashRefunds=0,
        counted=20000,
        expected=20000,
        variance=0,
        closedAt="3:00 pm",
        closedBy="Mia",
    )
    first = post(till, "/shifts/SH1", close, method="put")
    assert first.status_code == 200, first.data

    headers = dict(till._credentials)
    headers["HTTP_X_TERMINAL_ID"] = "OTHER-TILL"
    till.credentials(**headers)
    retry = post(till, "/shifts/SH1", close, method="put")
    assert retry.status_code == 409, retry.data


@pytest.mark.parametrize("last_digit", range(10))
def test_cash_payment_accepts_correct_rounding(till, cafe, menu, last_digit, staff_session):
    created = create(till)
    amount = 1000 + last_digit
    rounded = ((amount + 2) // 5) * 5

    response = pay(
        till,
        created["id"],
        amount,
        ref="CASH-ROUND-001",
        cashPaid=rounded,
        rounding=rounded - amount,
        change=2000 - rounded,
    )
    assert response.status_code == 200, response.data

    payment = Order.objects.get(number=created["id"]).payments.get()
    assert payment.amount_cents == amount
    assert payment.cash_paid_cents == rounded
    assert payment.rounding_cents == rounded - amount


@pytest.mark.parametrize(
    "details",
    [
        {"amount": 1003, "cashPaid": 1003, "rounding": 0},
        {"amount": 1003, "cashPaid": 1000, "rounding": 2},
        {"amount": 1003, "cashPaid": 1005, "rounding": -2},
        {"method": "card", "amount": 1000, "cashPaid": 1000},
    ],
)
def test_payment_rejects_inconsistent_cash_details(till, cafe, menu, details, staff_session):
    created = create(till)
    body = {"method": "cash", "ref": "INVALID-CASH-001", **details}

    response = post(till, f"/orders/{quote(created['id'])}/payments", body)
    assert response.status_code == 422, response.data
    assert not Order.objects.get(number=created["id"]).payments.exists()


def test_manager_can_save_category_order_for_one_cafe(till, cashier, cafe, menu, elsewhere):
    cashier.role = "manager"
    cashier.save(update_fields=["role"])

    response = post(
        till, "/locations/braddon/categories/order", {"ids": ["allday", "coffee"]}, method="put"
    )
    assert response.status_code == 200, response.data

    snapshot = till.get(f"{BASE}/snapshot?cafe=braddon")
    assert snapshot.status_code == 200, snapshot.data
    assert snapshot.json()["settings"]["categoryOrder"] == ["allday", "coffee"]

    from apps.pos.models import TillSettings

    assert not TillSettings.objects.filter(location=elsewhere).exists()


def test_cashier_cannot_change_category_order(till, cafe, menu):
    response = post(till, "/locations/braddon/categories/order", {"ids": ["coffee"]}, method="put")
    assert response.status_code == 403, response.data


@pytest.mark.parametrize("ids", [["coffee", "coffee"], ["unknown-category"]])
def test_category_order_rejects_invalid_ids(till, cashier, cafe, menu, ids):
    cashier.role = "manager"
    cashier.save(update_fields=["role"])

    response = post(till, "/locations/braddon/categories/order", {"ids": ids}, method="put")
    assert response.status_code == 422, response.data


def test_owner_can_change_branch_price_without_changing_base_price(till, cafe, menu):
    owner = UserFactory(role="owner", is_staff=True)
    till.force_authenticate(owner)
    item = menu["coffee"]
    original = item.base_price_cents

    response = post(
        till, f"/locations/braddon/items/{item.slug}/price", {"price": "6.25"}, method="put"
    )
    assert response.status_code == 200, response.data

    override = LocationMenuItem.objects.get(location=cafe, menu_item=item)
    assert override.price_override_cents == 625

    item.refresh_from_db()
    assert item.base_price_cents == original

    snapshot = till.get(f"{BASE}/snapshot?cafe=braddon").json()
    product = next(row for row in snapshot["menu"] if row["id"] == item.slug)
    assert product["price"] == 6.25


@pytest.mark.parametrize("role", ["staff", "manager"])
def test_branch_price_requires_owner_or_admin(till, cashier, cafe, menu, role):
    cashier.role = role
    cashier.save(update_fields=["role"])

    response = post(
        till, "/locations/braddon/items/hotcoffee/price", {"price": "6.25"}, method="put"
    )
    assert response.status_code == 403, response.data
    assert not LocationMenuItem.objects.filter(location=cafe, menu_item=menu["coffee"]).exists()


@pytest.mark.parametrize("price", ["-1", "6.251", "NaN"])
def test_branch_price_rejects_invalid_amounts(till, cafe, menu, price):
    till.force_authenticate(UserFactory(role="owner", is_staff=True))

    response = post(
        till, "/locations/braddon/items/hotcoffee/price", {"price": price}, method="put"
    )
    assert response.status_code == 422, response.data
    assert not LocationMenuItem.objects.filter(location=cafe, menu_item=menu["coffee"]).exists()


def test_modifier_availability_is_scoped_and_can_be_restored(till, cashier, cafe, menu, elsewhere):
    from apps.menu.catalog import menu_items

    cashier.role = "manager"
    cashier.save(update_fields=["role"])
    path = "/locations/braddon/modifiers/availability"

    disabled = post(till, path, {"label": "OAT ADC", "available": False}, method="put")
    assert disabled.status_code == 200, disabled.data

    def labels(location):
        return {
            name
            for entry in menu_items(location)
            for group in entry.groups
            for name, price in group.options
        }

    assert "OAT ADC" not in labels(cafe)
    assert "OAT ADC" in labels(elsewhere)

    snapshot = till.get(f"{BASE}/snapshot?cafe=braddon").json()
    assert "OAT ADC" in snapshot["availability"]["unavailableMods"]
    product = next(row for row in snapshot["menu"] if row["id"] == "hotcoffee")
    assert any(option[0] == "OAT ADC" for group in product["mods"] for option in group["options"])

    restored = post(till, path, {"label": "OAT ADC", "available": True}, method="put")
    assert restored.status_code == 200, restored.data
    assert "OAT ADC" in labels(cafe)


def test_cashier_cannot_change_modifier_availability(till, cafe, menu):
    response = post(
        till,
        "/locations/braddon/modifiers/availability",
        {"label": "OAT ADC", "available": False},
        method="put",
    )
    assert response.status_code == 403, response.data


def test_modifier_must_exist_on_cafe_menu(till, cashier, cafe, menu):
    cashier.role = "manager"
    cashier.save(update_fields=["role"])

    response = post(
        till,
        "/locations/braddon/modifiers/availability",
        {"label": "Unknown modifier", "available": False},
        method="put",
    )
    assert response.status_code == 404, response.data


def test_owner_can_save_and_merge_till_toggles(till, cafe, elsewhere):
    from apps.pos.models import TillSettings

    till.force_authenticate(UserFactory(role="owner", is_staff=True))
    path = "/locations/braddon/settings/toggles"

    first = post(till, path, {"toggles": {"receipt": True}}, method="put")
    assert first.status_code == 200, first.data

    second = post(till, path, {"toggles": {"openDrawer": False}}, method="put")
    assert second.status_code == 200, second.data

    saved = TillSettings.objects.get(location=cafe)
    assert saved.toggles == {"receipt": True, "openDrawer": False}
    assert not TillSettings.objects.filter(location=elsewhere).exists()

    snapshot = till.get(f"{BASE}/snapshot?cafe=braddon").json()
    assert snapshot["settings"]["toggles"]["receipt"] is True
    assert snapshot["settings"]["toggles"]["openDrawer"] is False


@pytest.mark.parametrize("role", ["staff", "manager"])
def test_till_settings_require_owner_or_admin(till, cashier, cafe, role):
    cashier.role = role
    cashier.save(update_fields=["role"])

    response = post(
        till, "/locations/braddon/settings/toggles", {"toggles": {"receipt": True}}, method="put"
    )
    assert response.status_code == 403, response.data


@pytest.mark.parametrize(
    "body",
    [
        {"toggles": {"unknown": True}},
        {"toggles": {}},
        {"toggles": {"receipt": "invalid"}},
        {"toggles": {"receipt": True}, "rules": {"gst": "0"}},
    ],
)
def test_till_toggles_reject_invalid_or_unsupported_fields(till, cafe, body):
    till.force_authenticate(UserFactory(role="owner", is_staff=True))

    response = post(till, "/locations/braddon/settings/toggles", body, method="put")
    assert response.status_code == 422, response.data


def test_pin_login_issues_a_verifiable_staff_session(till, cashier, cafe):
    from types import SimpleNamespace

    from apps.pos.staff_sessions import read_staff_session

    response = post(till, "/staff/verify-pin", {"pin": "4821"})
    assert response.status_code == 200, response.data
    assert response.data["staffTokenExpiresIn"] > 0

    request = SimpleNamespace(user=cashier, headers={"X-Terminal-Id": TERMINAL})
    staff = read_staff_session(request, response.data["staffToken"], cafe)
    assert staff.pk == cashier.pk


@pytest.mark.parametrize("mismatch", ["account", "terminal", "pin"])
def test_staff_session_rejects_changed_identity(till, cashier, cafe, mismatch):
    from types import SimpleNamespace

    from apps.mobile.errors import AppError
    from apps.pos.staff_sessions import read_staff_session

    response = post(till, "/staff/verify-pin", {"pin": "4821"})
    assert response.status_code == 200, response.data
    request = SimpleNamespace(user=cashier, headers={"X-Terminal-Id": TERMINAL})

    if mismatch == "account":
        request.user = UserFactory(role="owner", is_staff=True)
    elif mismatch == "terminal":
        request.headers["X-Terminal-Id"] = "OTHER-TILL"
    else:
        pins.set_pin(cashier, "1357")
        cashier.save(update_fields=["pos_pin"])

    with pytest.raises(AppError):
        read_staff_session(request, response.data["staffToken"], cafe)


def test_cashier_session_does_not_grant_refund_permission(till, cashier, cafe):
    from types import SimpleNamespace

    from apps.mobile.errors import AppError
    from apps.pos.staff_sessions import require_staff_permission

    response = post(till, "/staff/verify-pin", {"pin": "4821"})
    assert response.status_code == 200, response.data
    request = SimpleNamespace(
        user=cashier,
        headers={"X-Terminal-Id": TERMINAL, "X-Pos-Staff": response.data["staffToken"]},
    )

    assert require_staff_permission(request, "pos", cafe).pk == cashier.pk
    with pytest.raises(AppError):
        require_staff_permission(request, "orders.refund", cafe)


@pytest.fixture
def refund_session(till, cafe):
    manager = UserFactory(role="manager", first_name="Refund", last_name="Manager", is_staff=True)
    StaffAssignment.objects.create(user=manager, location=cafe)
    pins.set_pin(manager, "8642")
    manager.save(update_fields=["pos_pin"])

    response = post(till, "/staff/verify-pin", {"pin": "8642"})
    assert response.status_code == 200, response.data
    till.credentials(
        HTTP_X_TERMINAL_ID=TERMINAL,
        HTTP_X_POS_STAFF=response.data["staffToken"],
    )
    return manager


@pytest.mark.parametrize(
    "staff_role, expected_status",
    [
        ("missing", 403),
        ("staff", 403),
        ("manager", 200),
    ],
)
def test_order_adjustment_uses_pin_staff_permissions(
    till, cafe, menu, cashier, staff_role, expected_status
):
    # Even an owner device account must respect the PIN staff's permissions.
    owner = UserFactory(role="owner", is_staff=True)
    till.force_authenticate(owner)
    created = create(till)

    if staff_role != "missing":
        if staff_role == "manager":
            staff = UserFactory(role="manager", is_staff=True)
            StaffAssignment.objects.create(user=staff, location=cafe)
            pins.set_pin(staff, "8642")
            staff.save(update_fields=["pos_pin"])
            pin = "8642"
        else:
            pin = "4821"

        login = post(till, "/staff/verify-pin", {"pin": pin})
        assert login.status_code == 200, login.data
        till.credentials(
            HTTP_X_TERMINAL_ID=TERMINAL,
            HTTP_X_POS_STAFF=login.data["staffToken"],
        )

    body = {
        "adjustment": {
            "key": "PERMISSION-CHECK",
            "mode": "cancel",
            "amount": 0,
            "reason": "Permission test",
        },
        "lines": [],
        "total": 0,
        "status": "cancelled",
    }
    response = post(till, f"/orders/{quote(created['id'])}/adjustments", body)
    assert response.status_code == expected_status, response.data

    order = Order.objects.get(number=created["id"])
    if expected_status == 403:
        assert order.status == "confirmed"
        assert not order.adjustments.exists()
    else:
        assert order.status == "cancelled"


def test_adjustment_uses_verified_staff_name(till, cafe, menu, refund_session):
    created = create(till)
    number = created["id"]
    body = {
        "adjustment": {
            "key": "VERIFIED-ACTOR-001",
            "mode": "cancel",
            "amount": 0,
            "reason": "Customer cancelled",
            "by": "Fake cashier",
            "approver": "Fake manager",
        },
        "lines": [],
        "total": 0,
        "status": "cancelled",
    }

    response = post(till, f"/orders/{quote(number)}/adjustments", body)
    assert response.status_code == 200, response.data

    order = Order.objects.get(number=number)
    adjustment = order.adjustments.get()
    expected = refund_session.get_full_name().strip() or refund_session.email
    assert adjustment.staff_name == expected
    assert adjustment.approver_name == expected
    assert adjustment.reason == "Customer cancelled"


@pytest.mark.parametrize("revoke", ["change_pin", "disable_staff"])
def test_adjustment_retry_rechecks_staff_session(till, cafe, menu, refund_session, revoke):
    from apps.pos import pins

    created = create(till)
    number = created["id"]
    path = f"/api/v1/pos/orders/{quote(number)}/adjustments"
    body = {
        "adjustment": {
            "key": "REVOKED-SESSION-001",
            "mode": "cancel",
            "amount": 0,
            "reason": "Customer cancelled",
        },
        "lines": [],
        "total": 0,
        "status": "cancelled",
    }
    headers = {"HTTP_IDEMPOTENCY_KEY": "revoked-session-request-001"}

    first = till.post(path, body, format="json", **headers)
    assert first.status_code == 200, first.data

    retry = till.post(path, body, format="json", **headers)
    assert retry.status_code == 200, retry.data
    assert retry.data == first.data

    if revoke == "change_pin":
        pins.set_pin(refund_session, "9753")
        refund_session.save(update_fields=["pos_pin"])
    else:
        refund_session.is_active = False
        refund_session.save(update_fields=["is_active"])

    rejected = till.post(path, body, format="json", **headers)
    assert rejected.status_code == 403, rejected.data
    assert rejected.data["error"]["code"] == "STAFF_SESSION_REQUIRED"

    order = Order.objects.get(number=number)
    assert order.adjustments.count() == 1


@pytest.mark.parametrize("staff_role", ["missing", "staff", "manager"])
@pytest.mark.parametrize("operation", ["move", "close"])
def test_cash_operations_require_manager_permission(
    till, cafe, staff_role, operation, staff_session
):
    from apps.pos import pins

    opened = post(till, "/shifts", shift_body())
    assert opened.status_code == 201, opened.data

    till.credentials(HTTP_X_TERMINAL_ID=TERMINAL)

    staff = None
    if staff_role != "missing":
        staff = UserFactory(role=staff_role, is_staff=True)
        StaffAssignment.objects.create(user=staff, location=cafe)
        pins.set_pin(staff, "8526")
        staff.save(update_fields=["pos_pin"])

        login = post(till, "/staff/verify-pin", {"pin": "8526"})
        assert login.status_code == 200, login.data
        till.credentials(
            HTTP_X_TERMINAL_ID=TERMINAL,
            HTTP_X_POS_STAFF=login.data["staffToken"],
        )

    if operation == "move":
        body = {
            "id": "PERMISSION-MOVE-001",
            "type": "out",
            "amount": 1000,
            "reason": "Banking drop",
            "by": "Untrusted name",
        }
        response = post(till, "/shifts/SH1/moves", body)
    else:
        body = shift_body(
            status="closed",
            cashSales=0,
            cashRefunds=0,
            counted=20000,
            expected=20000,
            variance=0,
            closedAt="3:00 pm",
            closedBy="Untrusted name",
        )
        response = post(till, "/shifts/SH1", body, method="put")

    shift = CashShift.objects.get(reference="SH1")
    if staff_role != "manager":
        assert response.status_code == 403, response.data
        assert shift.status == "open"
        assert shift.moves == []
        assert shift.closed_at is None
    else:
        assert response.status_code == 200, response.data
        actor = staff.get_full_name().strip() or staff.email
        assert shift.updated_by_id == staff.pk
        if operation == "move":
            assert len(shift.moves) == 1
            assert shift.moves[0]["by"] == actor
            assert shift.moves[0]["approver"] == actor
        else:
            assert shift.status == "closed"
            assert shift.closed_by == actor
            assert shift.approved_by == actor


def test_open_shift_requires_staff_session(till, cafe):
    response = post(till, "/shifts", shift_body())
    assert response.status_code == 403, response.data
    assert not CashShift.objects.exists()


def test_open_shift_uses_verified_staff(till, cafe, staff_session):
    body = shift_body(staffId="fake-id", staffName="Fake name")
    response = post(till, "/shifts", body)
    assert response.status_code == 201, response.data

    shift = CashShift.objects.get(reference="SH1")
    assert shift.staff_ref == str(staff_session.pk)
    assert shift.staff_name == "Finn Test"
    assert shift.created_by_id == staff_session.pk
    assert shift.updated_by_id == staff_session.pk


def test_payment_requires_staff_session(till, cafe, menu):
    created = create(till)
    response = pay(till, created["id"], 3560, ref="NO-STAFF-PAYMENT")
    assert response.status_code == 403, response.data

    order = Order.objects.get(number=created["id"])
    assert not order.payments.exists()


def test_payment_uses_verified_staff_and_retries(till, cafe, menu, staff_session):
    created = create(till)
    number = created["id"]
    body = {
        "ref": "VERIFIED-PAYMENT-001",
        "method": "cash",
        "amount": 3560,
        "cashPaid": 3560,
        "change": 0,
        "by": "Fake cashier",
    }
    path = f"/orders/{quote(number)}/payments"

    first = post(till, path, body)
    assert first.status_code == 200, first.data

    retry = post(till, path, body)
    assert retry.status_code == 200, retry.data

    order = Order.objects.get(number=number)
    payment = order.payments.get()
    assert payment.staff_name == "Finn Test"
    assert payment.amount_cents == 3560


@pytest.mark.parametrize("signed_in", [False, True])
def test_embedded_payment_requires_verified_staff(till, cafe, menu, request, signed_in):
    staff = request.getfixturevalue("staff_session") if signed_in else None
    payment = {
        "method": "cash",
        "amount": 1000,
        "tip": 0,
        "surcharge": 0,
        "ref": "EMBEDDED-PAYMENT-001",
        "by": "Fake cashier",
    }
    body = order_body(payments=[payment])
    before = Order.objects.count()

    response = post(till, "/orders", body, key="embedded-payment-command-001")

    if not signed_in:
        assert response.status_code == 403, response.data
        assert Order.objects.count() == before
        return

    assert response.status_code == 201, response.data
    order = Order.objects.get(number=response.data["id"])
    saved = order.payments.get()
    assert saved.amount_cents == 1000
    assert saved.staff_name == (staff.get_full_name().strip() or staff.email)


@pytest.mark.parametrize("session_fixture", [None, "staff_session", "refund_session"])
def test_customer_update_requires_manager_permission(till, cafe, request, session_fixture):
    customer = UserFactory(role="customer", phone="0400000000")
    if session_fixture:
        request.getfixturevalue(session_fixture)

    response = post(
        till,
        f"/customers/{customer.pk}",
        {"phone": "0412345678"},
        method="put",
    )

    customer.refresh_from_db()
    if session_fixture == "refund_session":
        assert response.status_code == 200, response.data
        assert customer.phone == "0412345678"
    else:
        assert response.status_code == 403, response.data
        assert customer.phone == "0400000000"


def test_loyalty_action_requires_staff_session(till, cafe):
    customer = UserFactory(role="customer")
    response = post(
        till,
        f"/customers/{customer.pk}/loyalty/actions",
        {"cafe": cafe.slug, "action": "stamp"},
        key="loyalty-no-session",
    )
    assert response.status_code == 403, response.data


def test_loyalty_actions_are_incremental_and_idempotent(till, cafe, staff_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 7
    card.save(update_fields=["stamps"])
    path = f"/customers/{customer.pk}/loyalty/actions"
    stamp = {"cafe": cafe.slug, "action": "stamp"}

    first = post(till, path, stamp, key="stamp-001")
    assert first.status_code == 200, first.data

    retry = post(till, path, stamp, key="stamp-001")
    assert retry.status_code == 200, retry.data
    card.refresh_from_db()
    assert card.stamps == 8

    second = post(till, path, stamp, key="stamp-002")
    assert second.status_code == 200, second.data
    card.refresh_from_db()
    assert card.stamps == 9
    assert card.updated_by_id == staff_session.pk

    full = post(till, path, stamp, key="stamp-003")
    assert full.status_code == 200, full.data

    redeem = {"cafe": cafe.slug, "action": "redeem"}
    redeemed = post(till, path, redeem, key="redeem-001")
    assert redeemed.status_code == 200, redeemed.data
    card.refresh_from_db()
    assert card.stamps == 1

    retry = post(till, path, redeem, key="redeem-001")
    assert retry.status_code == 200, retry.data

    empty = post(till, path, redeem, key="redeem-002")
    assert empty.status_code == 409, empty.data
    card.refresh_from_db()
    assert card.stamps == 1


def test_manager_can_add_loyalty_stamp(till, cafe, refund_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    before = card.stamps

    response = post(
        till,
        f"/customers/{customer.pk}/loyalty/actions",
        {"cafe": cafe.slug, "action": "stamp"},
        key="manager-loyalty-stamp-001",
    )
    assert response.status_code == 200, response.data
    card.refresh_from_db()
    assert card.stamps == before + 1
    assert card.updated_by_id == refund_session.pk


def test_order_loyalty_requires_full_payment_and_runs_once(
    till, cafe, menu, cashier, staff_session
):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.services import award_pos_order_loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 2
    card.save(update_fields=["stamps"])

    created = create(till, customerId=str(customer.pk))
    order = Order.objects.get(number=created["id"])
    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )

    partial = pay(till, order.number, 1000, ref="LOYALTY-PARTIAL")
    assert partial.status_code == 200, partial.data

    with pytest.raises(AppError) as error:
        award_pos_order_loyalty(request, order)
    assert getattr(error.value, "status_code", None) == 409

    order.refresh_from_db()
    card.refresh_from_db()
    assert order.pos_loyalty_processed_at is None
    assert card.stamps == 2

    remaining = order.total_cents - 1000
    paid = pay(till, order.number, remaining, ref="LOYALTY-FINAL")
    assert paid.status_code == 200, paid.data

    coffee_count = sum(
        line.qty
        for line in order.lines.select_related("menu_item__category")
        if line.menu_item_id and line.menu_item.category.slug == "coffee"
    )
    assert coffee_count > 0

    processed = award_pos_order_loyalty(request, order)
    expected, earned = loyalty.stamps_after_order(2, coffee_count, False)
    card.refresh_from_db()
    assert card.stamps == expected
    assert processed.stamps_earned == earned
    assert processed.pos_loyalty_processed_at is not None

    activity_count = card.activity.count()
    processed_at = processed.pos_loyalty_processed_at
    repeated = award_pos_order_loyalty(request, order)
    card.refresh_from_db()
    assert card.stamps == expected
    assert card.activity.count() == activity_count
    assert repeated.pos_loyalty_processed_at == processed_at


@pytest.mark.parametrize(
    "initial, auto_stamp, expected, earned",
    [
        (8, True, 10, 2),
        (9, True, 11, 2),
        (2, False, 2, 0),
    ],
)
def test_order_loyalty_accumulates_rewards_and_respects_settings(
    till,
    cafe,
    menu,
    cashier,
    staff_session,
    initial,
    auto_stamp,
    expected,
    earned,
):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.models import TillSettings
    from apps.pos.services import award_pos_order_loyalty

    TillSettings.objects.update_or_create(
        location=cafe,
        defaults={"toggles": {"autoStamp": auto_stamp}},
    )
    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = initial
    card.save(update_fields=["stamps"])

    created = create(till, customerId=str(customer.pk))
    order = Order.objects.get(number=created["id"])
    paid = pay(till, order.number, order.total_cents, ref="LOYALTY-CAP-PAYMENT")
    assert paid.status_code == 200, paid.data

    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )
    activity_before = card.activity.count()
    processed = award_pos_order_loyalty(request, order)

    card.refresh_from_db()
    assert card.stamps == expected
    assert processed.stamps_earned == earned
    processed.refresh_from_db()
    recorded = processed.pos_loyalty_lines
    assert recorded
    assert sum(row["awarded"] for row in recorded.values()) == earned
    for row in recorded.values():
        assert 0 <= row["awarded"] <= row["qty"] - row["free_units"]
        assert row["returned_units"] == 0
        assert row["reversed_stamps"] == 0
    assert processed.pos_loyalty_processed_at is not None
    assert card.activity.count() == activity_before + (1 if earned else 0)

    processed_at = processed.pos_loyalty_processed_at
    repeated = award_pos_order_loyalty(request, order)
    card.refresh_from_db()
    assert card.stamps == expected
    assert repeated.pos_loyalty_processed_at == processed_at
    assert card.activity.count() == activity_before + (1 if earned else 0)


def test_pos_reward_reservation_is_applied_once(till, cafe, menu, cashier, staff_session):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.services import reserve_pos_order_reward

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(row for row in body["lines"] if row["itemId"] == menu["coffee"].slug)
    reward_value = coffee["price"]

    created = create(
        till,
        customerId=str(customer.pk),
        rewardUsed=True,
        discounts=[{"label": "Free coffee", "amount": reward_value}],
        total=body["total"] - reward_value,
    )
    order = Order.objects.get(number=created["id"])
    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )

    reserved = reserve_pos_order_reward(request, order)
    card.refresh_from_db()
    assert card.stamps == 0
    assert reserved.reward_cents == round(reward_value * 100)
    assert reserved.pos_reward_reserved_at is not None
    assert reserved.pos_reward_returned_at is None

    activity_count = card.activity.count()
    repeated = reserve_pos_order_reward(request, order)
    card.refresh_from_db()
    assert card.stamps == 0
    assert repeated.pos_reward_reserved_at == reserved.pos_reward_reserved_at
    assert card.activity.count() == activity_count


def test_pos_reward_cannot_be_used_by_two_orders(till, cafe, menu, cashier, staff_session):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.services import reserve_pos_order_reward

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(row for row in body["lines"] if row["itemId"] == menu["coffee"].slug)
    reward_value = coffee["price"]
    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )

    def make_order(number):
        created = create(
            till,
            id=number,
            customerId=str(customer.pk),
            rewardUsed=True,
            discounts=[{"label": "Free coffee", "amount": reward_value}],
            total=body["total"] - reward_value,
        )
        return Order.objects.get(number=created["id"])

    first = make_order("REWARD-RESERVE-001")
    second = make_order("REWARD-RESERVE-002")
    reserve_pos_order_reward(request, first)

    with pytest.raises(AppError) as error:
        reserve_pos_order_reward(request, second)
    assert error.value.status_code == 409

    second.refresh_from_db()
    card.refresh_from_db()
    assert second.pos_reward_reserved_at is None
    assert card.stamps == 0


def test_cancelled_unpaid_order_returns_reward_once(till, cafe, menu, cashier, refund_session):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.services import reserve_pos_order_reward, return_unpaid_pos_order_reward

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(row for row in body["lines"] if row["itemId"] == menu["coffee"].slug)
    reward_value = coffee["price"]
    created = create(
        till,
        customerId=str(customer.pk),
        rewardUsed=True,
        discounts=[{"label": "Free coffee", "amount": reward_value}],
        total=body["total"] - reward_value,
    )
    order = Order.objects.get(number=created["id"])
    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )

    reserve_pos_order_reward(request, order)

    with pytest.raises(AppError) as error:
        return_unpaid_pos_order_reward(request, order)
    assert error.value.status_code == 409
    card.refresh_from_db()
    assert card.stamps == 0

    order.status = "cancelled"
    order.save(update_fields=["status"])
    returned = return_unpaid_pos_order_reward(request, order)
    card.refresh_from_db()
    assert card.stamps == 9
    assert card.updated_by_id == refund_session.pk

    activity_count = card.activity.count()
    repeated = return_unpaid_pos_order_reward(request, order)
    assert repeated.pos_reward_returned_at == returned.pos_reward_returned_at
    assert card.activity.count() == activity_count


def test_paid_reward_order_preserves_other_stamps(till, cafe, menu, cashier, refund_session):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.services import (
        apply_loyalty_action,
        award_pos_order_loyalty,
        reserve_pos_order_reward,
        return_unpaid_pos_order_reward,
    )

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(row for row in body["lines"] if row["itemId"] == menu["coffee"].slug)
    reward_value = coffee["price"]
    created = create(
        till,
        customerId=str(customer.pk),
        rewardUsed=True,
        discounts=[{"label": "Free coffee", "amount": reward_value}],
        total=body["total"] - reward_value,
    )
    order = Order.objects.get(number=created["id"])
    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )

    reserve_pos_order_reward(request, order)
    apply_loyalty_action(request, customer, {"cafe": cafe.slug, "action": "stamp"})
    card.refresh_from_db()
    assert card.stamps == 1

    paid = pay(till, order.number, order.total_cents, ref="REWARD-ORDER-PAYMENT")
    assert paid.status_code == 200, paid.data

    coffee_count = sum(
        line.qty
        for line in order.lines.select_related("menu_item__category")
        if line.menu_item_id and line.menu_item.category.slug == "coffee"
    )
    expected, earned = loyalty.stamps_after_order(1, max(0, coffee_count - 1), False)
    processed = award_pos_order_loyalty(request, order)

    card.refresh_from_db()
    assert card.stamps == expected
    assert processed.stamps_earned == earned
    processed.refresh_from_db()
    recorded = processed.pos_loyalty_lines
    assert sum(row["free_units"] for row in recorded.values()) == 1
    assert sum(row["awarded"] for row in recorded.values()) == earned
    assert processed.pos_loyalty_processed_at is not None

    activity_count = card.activity.count()
    award_pos_order_loyalty(request, order)
    card.refresh_from_db()
    assert card.stamps == expected
    assert card.activity.count() == activity_count

    order.status = "cancelled"
    order.save(update_fields=["status"])
    with pytest.raises(AppError) as error:
        return_unpaid_pos_order_reward(request, order)
    assert error.value.status_code == 409

    order.refresh_from_db()
    card.refresh_from_db()
    assert order.pos_reward_returned_at is None
    assert card.stamps == expected


def test_full_refund_reverses_pos_stamps_once(till, cafe, menu, cashier, refund_session):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.services import award_pos_order_loyalty, reverse_fully_refunded_pos_loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 2
    card.save(update_fields=["stamps"])
    created = create(till, customerId=str(customer.pk))
    order = Order.objects.get(number=created["id"])

    paid = pay(till, order.number, order.total_cents, ref="FULL-REFUND-LOYALTY-PAY")
    assert paid.status_code == 200, paid.data
    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )
    award_pos_order_loyalty(request, order)

    with pytest.raises(AppError) as error:
        reverse_fully_refunded_pos_loyalty(request, order)
    assert error.value.status_code == 409

    body = {
        "adjustment": {
            "key": "FULL-REFUND-LOYALTY-001",
            "mode": "refund",
            "amount": order.total_cents,
            "alloc": [
                {
                    "method": "cash",
                    "amount": order.total_cents,
                    "ref": "FULL-REFUND-LOYALTY-PAY",
                }
            ],
            "reason": "Customer requested a full refund",
        },
        "lines": [{"key": line.key, "qty": line.qty} for line in order.lines.all()],
        "total": order.total_cents / 100,
        "status": "refunded",
    }
    response = post(till, f"/orders/{quote(order.number)}/adjustments", body)
    assert response.status_code == 200, response.data

    reversed_order = reverse_fully_refunded_pos_loyalty(request, order)
    card.refresh_from_db()
    assert card.stamps == 2
    assert reversed_order.pos_loyalty_reversed_at is not None

    activity_count = card.activity.count()
    repeated = reverse_fully_refunded_pos_loyalty(request, order)
    card.refresh_from_db()
    assert card.stamps == 2
    assert repeated.pos_loyalty_reversed_at == reversed_order.pos_loyalty_reversed_at
    assert card.activity.count() == activity_count


def test_full_refund_returns_used_reward_once(till, cafe, menu, cashier, refund_session):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.services import (
        award_pos_order_loyalty,
        reserve_pos_order_reward,
        reverse_fully_refunded_pos_loyalty,
    )

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(row for row in body["lines"] if row["itemId"] == menu["coffee"].slug)
    reward_value = coffee["price"]
    created = create(
        till,
        customerId=str(customer.pk),
        rewardUsed=True,
        discounts=[{"label": "Free coffee", "amount": reward_value}],
        total=body["total"] - reward_value,
    )
    order = Order.objects.get(number=created["id"])
    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )

    reserve_pos_order_reward(request, order)
    paid = pay(till, order.number, order.total_cents, ref="REWARD-REFUND-PAY")
    assert paid.status_code == 200, paid.data
    award_pos_order_loyalty(request, order)

    refund = {
        "adjustment": {
            "key": "REWARD-FULL-REFUND-001",
            "mode": "refund",
            "amount": order.total_cents,
            "alloc": [
                {
                    "method": "cash",
                    "amount": order.total_cents,
                    "ref": "REWARD-REFUND-PAY",
                }
            ],
            "reason": "Customer requested a full refund",
        },
        "lines": [{"key": line.key, "qty": line.qty} for line in order.lines.all()],
        "total": order.total_cents / 100,
        "status": "refunded",
    }
    response = post(till, f"/orders/{quote(order.number)}/adjustments", refund)
    assert response.status_code == 200, response.data

    returned = reverse_fully_refunded_pos_loyalty(request, order)
    card.refresh_from_db()
    assert card.stamps == 9
    assert returned.pos_reward_returned_at is not None
    assert returned.pos_loyalty_reversed_at is not None
    assert card.updated_by_id == refund_session.pk

    activity_count = card.activity.count()
    repeated = reverse_fully_refunded_pos_loyalty(request, order)
    card.refresh_from_db()
    assert card.stamps == 9
    assert repeated.pos_reward_returned_at == returned.pos_reward_returned_at
    assert repeated.pos_loyalty_reversed_at == returned.pos_loyalty_reversed_at
    assert card.activity.count() == activity_count


def test_partial_loyalty_reversal_is_limited_and_idempotent(
    till, cafe, menu, cashier, refund_session
):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.services import award_pos_order_loyalty, reverse_partial_pos_loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 2
    card.save(update_fields=["stamps"])

    created = create(till, customerId=str(customer.pk))
    order = Order.objects.get(number=created["id"])
    paid = pay(till, order.number, order.total_cents, ref="PARTIAL-LOYALTY-PAY")
    assert paid.status_code == 200, paid.data

    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )
    processed = award_pos_order_loyalty(request, order)
    line = order.lines.get(menu_item=menu["coffee"])
    assert line.qty == 2
    assert processed.pos_loyalty_lines[line.key]["awarded"] == 2

    first = order.adjustments.create(
        key="PARTIAL-LOYALTY-001",
        mode="refund",
        amount_cents=100,
        items={line.key: 1},
    )
    reverse_partial_pos_loyalty(request, order, first)
    card.refresh_from_db()
    assert card.stamps == 3

    activity_count = card.activity.count()
    reverse_partial_pos_loyalty(request, order, first)
    card.refresh_from_db()
    assert card.stamps == 3
    assert card.activity.count() == activity_count

    second = order.adjustments.create(
        key="PARTIAL-LOYALTY-002",
        mode="refund",
        amount_cents=100,
        items={line.key: 1},
    )
    reverse_partial_pos_loyalty(request, order, second)
    card.refresh_from_db()
    assert card.stamps == 2

    excessive = order.adjustments.create(
        key="PARTIAL-LOYALTY-003",
        mode="refund",
        amount_cents=100,
        items={line.key: 1},
    )
    with pytest.raises(AppError) as error:
        reverse_partial_pos_loyalty(request, order, excessive)
    assert error.value.status_code == 409

    excessive.refresh_from_db()
    card.refresh_from_db()
    assert excessive.pos_loyalty_processed_at is None
    assert card.stamps == 2


def test_adjustment_rejects_excess_loyalty_items_without_changes(
    till, cafe, menu, cashier, refund_session
):
    from types import SimpleNamespace

    from apps.loyalty import services as loyalty
    from apps.pos.services import award_pos_order_loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    created = create(till, customerId=str(customer.pk))
    order = Order.objects.get(number=created["id"])
    paid = pay(till, order.number, order.total_cents, ref="INVALID-ITEMS-PAY")
    assert paid.status_code == 200, paid.data

    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )
    award_pos_order_loyalty(request, order)
    card.refresh_from_db()
    before_stamps = card.stamps
    before_status = order.status
    line = order.lines.get(menu_item=menu["coffee"])

    body = {
        "adjustment": {
            "key": "INVALID-LOYALTY-ITEMS",
            "mode": "refund",
            "amount": 100,
            "items": {line.key: line.qty + 1},
            "alloc": [{"method": "cash", "amount": 100, "ref": "INVALID-ITEMS-PAY"}],
            "reason": "Partial refund",
        },
        "lines": [{"key": row.key, "qty": row.qty} for row in order.lines.all()],
        "total": order.total_cents / 100,
        "status": "new",
    }
    response = post(till, f"/orders/{quote(order.number)}/adjustments", body)
    assert response.status_code == 409, response.data

    order.refresh_from_db()
    card.refresh_from_db()
    line.refresh_from_db()
    assert not order.adjustments.exists()
    assert order.status == before_status
    assert card.stamps == before_stamps
    assert line.qty == 2


@pytest.mark.parametrize("managed", [False, True])
def test_order_preserves_loyalty_processing_mode(till, cafe, menu, staff_session, managed):
    created = create(till, serverLoyalty=managed)
    order = Order.objects.get(number=created["id"])

    assert order.pos_loyalty_managed is managed
    assert created.get("serverLoyalty", False) is managed

    response = till.get(f"{BASE}/snapshot", {"cafe": cafe.slug})
    assert response.status_code == 200, response.data
    saved = next(row for row in response.data["orders"] if row["id"] == order.number)
    assert saved.get("serverLoyalty", False) is managed


@pytest.mark.parametrize("managed", [False, True])
def test_payment_awards_server_loyalty_only_after_full_payment(
    till,
    cafe,
    menu,
    staff_session,
    managed,
):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 2
    card.save(update_fields=["stamps"])

    created = create(
        till,
        customerId=str(customer.pk),
        serverLoyalty=managed,
    )
    order = Order.objects.get(number=created["id"])

    partial = pay(till, order.number, 1000, ref="AUTO-LOYALTY-PARTIAL")
    assert partial.status_code == 200, partial.data
    card.refresh_from_db()
    order.refresh_from_db()
    assert card.stamps == 2
    assert order.pos_loyalty_processed_at is None

    remaining = order.total_cents - 1000
    final = pay(till, order.number, remaining, ref="AUTO-LOYALTY-FINAL")
    assert final.status_code == 200, final.data

    coffee_count = sum(
        row.qty
        for row in order.lines.select_related("menu_item__category")
        if row.menu_item_id and row.menu_item.category.slug == "coffee"
    )
    expected, earned = loyalty.stamps_after_order(2, coffee_count, False)
    card.refresh_from_db()
    order.refresh_from_db()

    assert card.stamps == (expected if managed else 2)
    assert order.stamps_earned == (earned if managed else 0)
    assert (order.pos_loyalty_processed_at is not None) is managed

    activity_count = card.activity.count()
    retry = pay(till, order.number, remaining, ref="AUTO-LOYALTY-FINAL")
    assert retry.status_code == 200, retry.data
    card.refresh_from_db()
    assert card.stamps == (expected if managed else 2)
    assert card.activity.count() == activity_count


def test_managed_orders_reserve_reward_once(till, cafe, menu, staff_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    base = order_body()
    coffee = next(row for row in base["lines"] if row["itemId"] == menu["coffee"].slug)
    reward_value = coffee["price"]

    first_body = order_body(
        id="MANAGED-REWARD-001",
        customerId=str(customer.pk),
        serverLoyalty=True,
        rewardUsed=True,
        discounts=[{"label": "Free coffee", "amount": reward_value}],
        total=base["total"] - reward_value,
    )

    first = post(till, "/orders", first_body, key="managed-reward-create-001")
    assert first.status_code == 201, first.data

    card.refresh_from_db()
    order = Order.objects.get(number="MANAGED-REWARD-001")
    assert card.stamps == 0
    assert order.pos_reward_reserved_at is not None

    activity_count = card.activity.count()
    retry = post(till, "/orders", first_body, key="managed-reward-create-001")
    assert retry.status_code == 201, retry.data
    card.refresh_from_db()
    assert card.stamps == 0
    assert card.activity.count() == activity_count

    second_body = {**first_body, "id": "MANAGED-REWARD-002"}
    second = post(till, "/orders", second_body, key="managed-reward-create-002")
    assert second.status_code == 409, second.data
    assert not Order.objects.filter(number="MANAGED-REWARD-002").exists()

    card.refresh_from_db()
    assert card.stamps == 0
    assert card.activity.count() == activity_count


def test_unpaid_reward_return_preserves_new_stamps(till, cafe, menu, cashier, refund_session):
    from types import SimpleNamespace

    from django.utils import timezone

    from apps.loyalty import services as loyalty
    from apps.pos.services import return_unpaid_pos_order_reward

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 3
    card.save(update_fields=["stamps"])

    created = create(till, customerId=str(customer.pk))
    order = Order.objects.get(number=created["id"])
    order.status = "cancelled"
    order.pos_reward_reserved_at = timezone.now()
    order.save(update_fields=["status", "pos_reward_reserved_at"])

    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )

    return_unpaid_pos_order_reward(request, order)
    return_unpaid_pos_order_reward(request, order)  # Replay must not restore the reward twice.

    card.refresh_from_db()
    order.refresh_from_db()
    assert card.stamps == 12
    assert order.pos_reward_returned_at is not None


def test_full_refund_preserves_new_stamps(till, cafe, menu, cashier, refund_session):
    from types import SimpleNamespace

    from django.utils import timezone

    from apps.loyalty import services as loyalty
    from apps.pos.services import reverse_fully_refunded_pos_loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 3
    card.save(update_fields=["stamps"])

    created = create(till, customerId=str(customer.pk))
    order = Order.objects.get(number=created["id"])

    response = pay(till, order.number, order.total_cents, ref="REWARD-REFUND-PAY")
    assert response.status_code == 200, response.data

    order.refresh_from_db()
    order.reward_redeemed = True
    order.pos_reward_reserved_at = timezone.now()
    order.pos_loyalty_processed_at = timezone.now()
    order.stamps_earned = 0
    order.status = "refunded"
    order.save(
        update_fields=[
            "reward_redeemed",
            "pos_reward_reserved_at",
            "pos_loyalty_processed_at",
            "stamps_earned",
            "status",
        ]
    )
    order.adjustments.create(key="REWARD-REFUND-001", mode="refund", amount_cents=order.total_cents)

    request = SimpleNamespace(
        user=cashier,
        headers={
            "X-Terminal-Id": TERMINAL,
            "X-Pos-Staff": till._credentials["HTTP_X_POS_STAFF"],
        },
    )

    reverse_fully_refunded_pos_loyalty(request, order)
    reverse_fully_refunded_pos_loyalty(request, order)  # Replay must not restore the reward twice.

    card.refresh_from_db()
    order.refresh_from_db()
    assert card.stamps == 12
    assert order.pos_reward_returned_at is not None
    assert order.pos_loyalty_reversed_at is not None


def test_cancelling_unpaid_managed_order_returns_reward(till, cafe, menu, refund_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(line for line in body["lines"] if line["itemId"] == menu["coffee"].slug)
    body.update(
        {
            "id": "REWARD-CANCEL-001",
            "customerId": str(customer.pk),
            "serverLoyalty": True,
            "rewardUsed": True,
            "discounts": [{"label": "Free coffee", "amount": coffee["price"]}],
            "total": body["total"] - coffee["price"],
        }
    )

    created = post(till, "/orders", body)
    assert created.status_code == 201, created.data

    card.refresh_from_db()
    assert card.stamps == 0

    cancel = {
        "adjustment": {
            "key": "REWARD-CANCEL-ADJUSTMENT",
            "mode": "cancel",
            "amount": 0,
            "reason": "Customer changed their mind",
        },
        "lines": [],
        "total": 0,
        "status": "cancelled",
    }
    response = post(till, "/orders/REWARD-CANCEL-001/adjustments", cancel)
    assert response.status_code == 200, response.data

    card.refresh_from_db()
    order = Order.objects.get(number="REWARD-CANCEL-001")
    assert card.stamps == 9
    assert order.pos_reward_returned_at is not None


def test_cancel_returns_reward_alongside_new_stamps(till, cafe, menu, refund_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(line for line in body["lines"] if line["itemId"] == menu["coffee"].slug)
    body.update(
        {
            "id": "REWARD-CANCEL-CONFLICT",
            "customerId": str(customer.pk),
            "serverLoyalty": True,
            "rewardUsed": True,
            "discounts": [{"label": "Free coffee", "amount": coffee["price"]}],
            "total": body["total"] - coffee["price"],
        }
    )

    created = post(till, "/orders", body)
    assert created.status_code == 201, created.data

    card.refresh_from_db()
    assert card.stamps == 0
    card.stamps = 3
    card.save(update_fields=["stamps"])

    cancel = {
        "adjustment": {
            "key": "REWARD-CANCEL-CONFLICT-ADJUSTMENT",
            "mode": "cancel",
            "amount": 0,
            "reason": "Customer changed their mind",
        },
        "lines": [],
        "total": 0,
        "status": "cancelled",
    }

    response = post(till, "/orders/REWARD-CANCEL-CONFLICT/adjustments", cancel)
    assert response.status_code == 200, response.data

    card.refresh_from_db()
    order = Order.objects.get(number="REWARD-CANCEL-CONFLICT")
    assert card.stamps == 12
    assert order.status == "cancelled"
    assert order.pos_reward_returned_at is not None
    assert order.adjustments.count() == 1
    assert order.lines.filter(qty__gt=0).count() == 0


def test_cancel_partly_paid_order_returns_reward(till, cafe, menu, refund_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(line for line in body["lines"] if line["itemId"] == menu["coffee"].slug)
    body.update(
        {
            "id": "REWARD-PARTIAL-CANCEL",
            "customerId": str(customer.pk),
            "serverLoyalty": True,
            "rewardUsed": True,
            "discounts": [{"label": "Free coffee", "amount": coffee["price"]}],
            "total": body["total"] - coffee["price"],
        }
    )

    created = post(till, "/orders", body)
    assert created.status_code == 201, created.data

    paid = pay(till, body["id"], 1000, ref="PARTIAL-CANCEL-PAY")
    assert paid.status_code == 200, paid.data

    cancel = {
        "adjustment": {
            "key": "PARTIAL-CANCEL-ADJUSTMENT",
            "mode": "cancel",
            "amount": 1000,
            "alloc": [{"method": "cash", "amount": 1000, "ref": "PARTIAL-CANCEL-PAY"}],
            "reason": "Customer changed their mind",
        },
        "lines": [],
        "total": 0,
        "status": "cancelled",
    }

    response = post(till, f"/orders/{body['id']}/adjustments", cancel)
    assert response.status_code == 200, response.data

    order = Order.objects.get(number=body["id"])
    card.refresh_from_db()
    assert order.status == "cancelled"
    assert card.stamps == 9
    assert order.pos_reward_returned_at is not None


@pytest.mark.parametrize("mode, status", [("refund", "refunded"), ("cancel", "cancelled")])
def test_full_refund_reverses_managed_order_stamps(till, cafe, menu, refund_session, mode, status):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 2
    card.save(update_fields=["stamps"])

    body = order_body(id="MANAGED-FULL-REFUND", customerId=str(customer.pk), serverLoyalty=True)
    created = post(till, "/orders", body)
    assert created.status_code == 201, created.data

    paid = pay(till, body["id"], 3560, ref="MANAGED-FULL-PAY")
    assert paid.status_code == 200, paid.data

    card.refresh_from_db()
    order = Order.objects.get(number=body["id"])
    assert card.stamps == 4
    assert order.pos_loyalty_processed_at is not None

    refund = {
        "adjustment": {
            "key": "MANAGED-FULL-REFUND-001",
            "mode": mode,
            "amount": 3560,
            "alloc": [{"method": "cash", "amount": 3560, "ref": "MANAGED-FULL-PAY"}],
            "reason": "Customer requested a refund",
        },
        "lines": [] if mode == "cancel" else [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": 0 if mode == "cancel" else 35.6,
        "status": status,
    }

    response = post(till, f"/orders/{body['id']}/adjustments", refund)
    assert response.status_code == 200, response.data

    card.refresh_from_db()
    order.refresh_from_db()
    assert order.status == status
    assert card.stamps == 2
    assert order.pos_loyalty_reversed_at is not None
    activity_count = card.activity.count()

    retry = post(till, f"/orders/{body['id']}/adjustments", refund)
    assert retry.status_code == 200, retry.data

    card.refresh_from_db()
    order.refresh_from_db()
    assert card.stamps == 2
    assert card.activity.count() == activity_count
    assert order.adjustments.count() == 1


def test_partial_refund_reverses_one_managed_stamp(till, cafe, menu, refund_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 2
    card.save(update_fields=["stamps"])

    body = order_body(id="MANAGED-PARTIAL-REFUND", customerId=str(customer.pk), serverLoyalty=True)
    created = post(till, "/orders", body)
    assert created.status_code == 201, created.data

    paid = pay(till, body["id"], 3560, ref="MANAGED-PARTIAL-PAY")
    assert paid.status_code == 200, paid.data

    card.refresh_from_db()
    assert card.stamps == 4

    refund = {
        "adjustment": {
            "key": "MANAGED-PARTIAL-REFUND-001",
            "mode": "refund",
            "amount": 630,
            "alloc": [{"method": "cash", "amount": 630, "ref": "MANAGED-PARTIAL-PAY"}],
            "items": {"L1": 1},
            "reason": "One coffee returned",
        },
        "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": 35.6,
        "status": "new",
    }

    response = post(till, f"/orders/{body['id']}/adjustments", refund)
    assert response.status_code == 200, response.data

    card.refresh_from_db()
    order = Order.objects.get(number=body["id"])
    assert card.stamps == 3
    assert order.adjustments.get().pos_loyalty_processed_at is not None

    retry = post(till, f"/orders/{body['id']}/adjustments", refund)
    assert retry.status_code == 200, retry.data

    card.refresh_from_db()
    assert card.stamps == 3
    assert order.adjustments.count() == 1

    final_refund = {
        "adjustment": {
            "key": "MANAGED-PARTIAL-REFUND-002",
            "mode": "refund",
            "amount": 2930,
            "alloc": [{"method": "cash", "amount": 2930, "ref": "MANAGED-PARTIAL-PAY"}],
            "reason": "Remaining order refunded",
        },
        "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": 35.6,
        "status": "refunded",
    }

    response = post(till, f"/orders/{body['id']}/adjustments", final_refund)
    assert response.status_code == 200, response.data

    card.refresh_from_db()
    order.refresh_from_db()
    assert card.stamps == 2
    assert order.pos_loyalty_reversed_at is not None
    assert order.adjustments.count() == 2


def test_full_refund_restores_used_reward(till, cafe, menu, refund_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(line for line in body["lines"] if line["itemId"] == menu["coffee"].slug)
    body.update(
        {
            "id": "MANAGED-REWARD-REFUND",
            "customerId": str(customer.pk),
            "serverLoyalty": True,
            "rewardUsed": True,
            "discounts": [{"label": "Free coffee", "amount": coffee["price"]}],
            "total": body["total"] - coffee["price"],
        }
    )

    created = post(till, "/orders", body)
    assert created.status_code == 201, created.data

    order = Order.objects.get(number=body["id"])
    paid = pay(till, body["id"], order.total_cents, ref="REWARD-FULL-PAY")
    assert paid.status_code == 200, paid.data

    card.refresh_from_db()
    assert card.stamps == 1

    refund = {
        "adjustment": {
            "key": "REWARD-FULL-REFUND",
            "mode": "refund",
            "amount": order.total_cents,
            "alloc": [{"method": "cash", "amount": order.total_cents, "ref": "REWARD-FULL-PAY"}],
            "reason": "Customer requested a refund",
        },
        "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": body["total"],
        "status": "refunded",
    }

    response = post(till, f"/orders/{body['id']}/adjustments", refund)
    assert response.status_code == 200, response.data

    card.refresh_from_db()
    order.refresh_from_db()
    assert card.stamps == 9
    assert order.pos_reward_returned_at is not None
    assert order.pos_loyalty_reversed_at is not None


def test_full_reward_refund_preserves_new_stamps(till, cafe, menu, refund_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(line for line in body["lines"] if line["itemId"] == menu["coffee"].slug)
    body.update(
        {
            "id": "REWARD-REFUND-CONFLICT",
            "customerId": str(customer.pk),
            "serverLoyalty": True,
            "rewardUsed": True,
            "discounts": [{"label": "Free coffee", "amount": coffee["price"]}],
            "total": body["total"] - coffee["price"],
        }
    )

    created = post(till, "/orders", body)
    assert created.status_code == 201, created.data

    order = Order.objects.get(number=body["id"])
    paid = pay(till, body["id"], order.total_cents, ref="REWARD-CONFLICT-PAY")
    assert paid.status_code == 200, paid.data

    card.refresh_from_db()
    assert card.stamps == 1

    # Simulate a stamp earned after this order was paid.
    card.stamps = 2
    card.save(update_fields=["stamps"])

    refund = {
        "adjustment": {
            "key": "REWARD-CONFLICT-REFUND",
            "mode": "refund",
            "amount": order.total_cents,
            "alloc": [
                {"method": "cash", "amount": order.total_cents, "ref": "REWARD-CONFLICT-PAY"}
            ],
            "reason": "Customer requested a refund",
        },
        "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": body["total"],
        "status": "refunded",
    }

    response = post(till, f"/orders/{body['id']}/adjustments", refund)
    assert response.status_code == 200, response.data

    card.refresh_from_db()
    order.refresh_from_db()
    assert card.stamps == 10
    assert order.status == "refunded"
    assert order.pos_reward_returned_at is not None
    assert order.pos_loyalty_reversed_at is not None
    assert order.adjustments.count() == 1


def test_customer_loyalty_read_requires_staff_session(till, cafe, regular):
    from apps.loyalty import services as loyalty

    card = loyalty.card_for(regular)
    card.stamps = 7
    card.save(update_fields=["stamps"])

    path = f"{BASE}/customers/{regular.pk}/loyalty"
    denied = till.get(path)
    assert denied.status_code == 403, denied.data

    manager = UserFactory(role="manager", first_name="Read", last_name="Manager", is_staff=True)
    StaffAssignment.objects.create(user=manager, location=cafe)
    pins.set_pin(manager, "8642")
    manager.save(update_fields=["pos_pin"])

    signed_in = post(till, "/staff/verify-pin", {"pin": "8642"})
    assert signed_in.status_code == 200, signed_in.data
    till.credentials(
        HTTP_X_TERMINAL_ID=TERMINAL,
        HTTP_X_POS_STAFF=signed_in.data["staffToken"],
    )

    response = till.get(path)
    assert response.status_code == 200, response.data
    assert response.json() == {"id": str(regular.pk), "stamps": 7, "rewards": 0}

    card.refresh_from_db()
    assert card.stamps == 7


def test_partial_reward_refund_is_rejected_without_changes(till, cafe, menu, refund_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 9
    card.save(update_fields=["stamps"])

    body = order_body()
    coffee = next(line for line in body["lines"] if line["itemId"] == menu["coffee"].slug)
    body.update(
        {
            "id": "PARTIAL-REWARD-ORDER",
            "customerId": str(customer.pk),
            "serverLoyalty": True,
            "rewardUsed": True,
            "discounts": [{"label": "Free coffee", "amount": coffee["price"]}],
            "total": body["total"] - coffee["price"],
        }
    )

    created = post(till, "/orders", body)
    assert created.status_code == 201, created.data

    order = Order.objects.get(number=body["id"])
    paid = pay(till, body["id"], order.total_cents, ref="PARTIAL-REWARD-PAY")
    assert paid.status_code == 200, paid.data

    card.refresh_from_db()
    assert card.stamps == 1
    order.refresh_from_db()
    previous_status = order.status
    previous_total = order.total_cents

    refund = {
        "adjustment": {
            "key": "PARTIAL-REWARD-REFUND",
            "mode": "refund",
            "amount": 100,
            "alloc": [{"method": "cash", "amount": 100, "ref": "PARTIAL-REWARD-PAY"}],
            "items": {"L1": 1},
            "reason": "One coffee returned",
        },
        "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": body["total"],
        "status": "new",
    }

    response = post(till, f"/orders/{body['id']}/adjustments", refund)
    assert response.status_code == 409, response.data

    card.refresh_from_db()
    order.refresh_from_db()
    assert card.stamps == 1
    assert order.status == previous_status
    assert order.total_cents == previous_total
    assert not order.adjustments.exists()


@pytest.mark.parametrize(
    "initial, coffees, redeemed, expected, earned",
    [
        (8, 3, False, 11, 3),
        (17, 4, False, 21, 4),
        (21, 3, True, 14, 2),
    ],
)
def test_accumulated_loyalty_arithmetic(initial, coffees, redeemed, expected, earned):
    from types import SimpleNamespace

    from apps.loyalty.services import stamps_after_order
    from apps.pos.services import card_stamps, till_balance

    assert stamps_after_order(initial, coffees, redeemed) == (expected, earned)
    stamps, rewards = till_balance(SimpleNamespace(stamps=expected))
    assert stamps == expected % 9
    assert rewards == expected // 9
    assert card_stamps(stamps, rewards) == expected


def test_multiple_rewards_survive_redemption_and_replay(till, cafe, staff_session):
    from apps.loyalty import services as loyalty

    customer = UserFactory(role="customer")
    card = loyalty.card_for(customer)
    card.stamps = 20
    card.save(update_fields=["stamps"])
    path = f"/customers/{customer.pk}/loyalty/actions"
    data = {"cafe": cafe.slug, "action": "redeem"}
    for _ in range(2):
        response = post(till, path, data, key="multi-reward-redeem")
        assert response.status_code == 200, response.data
        assert (response.data["stamps"], response.data["rewards"]) == (2, 1)
    card.refresh_from_db()
    assert card.stamps == 11
    assert card.activity.count() == 1
    response = post(till, path, {"cafe": cafe.slug, "action": "stamp"}, key="multi-stamp")
    assert response.status_code == 200, response.data
    assert (response.data["stamps"], response.data["rewards"]) == (3, 1)


def test_bos_and_mobile_preserve_multiple_rewards(owner):
    from apps.loyalty.services import add_stamp, card_for, redeem_reward
    from apps.mobile.presenters import loyalty_card
    from apps.mobile.serializers import LoyaltyCardOut

    customer = UserFactory(role="customer")
    card = card_for(customer)
    card.stamps = 20
    card.save(update_fields=["stamps"])
    from django.test import RequestFactory

    request = RequestFactory().post("/")
    request.user = owner
    card = add_stamp(card, request)
    assert card.stamps == 21
    payload = LoyaltyCardOut(loyalty_card(card)).data
    assert payload["stamps"] == 9  # Legacy mobile clients still display a ready reward.
    assert payload["progressStamps"] == 3
    assert payload["rewards"] == 2
    card = redeem_reward(card, request)
    assert card.stamps == 12
    card = redeem_reward(card, request)
    assert card.stamps == 3


def test_managed_order_preserves_multiple_rewards_through_full_refund(
    till, cafe, menu, refund_session
):
    from apps.loyalty.services import card_for

    customer = UserFactory(role="customer")
    card = card_for(customer)
    card.stamps = 20
    card.save(update_fields=["stamps"])
    body = order_body(id="MULTI-REWARD-ORDER", customerId=str(customer.pk), serverLoyalty=True)
    coffee = next(row for row in body["lines"] if row["itemId"] == menu["coffee"].slug)
    body.update(
        rewardUsed=True,
        discounts=[{"label": "Free coffee", "amount": coffee["price"]}],
        total=body["total"] - coffee["price"],
    )
    created = post(till, "/orders", body)
    assert created.status_code == 201, created.data
    card.refresh_from_db()
    assert card.stamps == 11
    order = Order.objects.get(number=body["id"])
    paid = pay(till, order.number, order.total_cents, ref="MULTI-REWARD-PAY")
    assert paid.status_code == 200, paid.data
    card.refresh_from_db()
    assert card.stamps == 12  # One paid coffee earns a stamp; the free coffee does not.
    refund = {
        "adjustment": {
            "key": "MULTI-REWARD-REFUND",
            "mode": "refund",
            "amount": order.total_cents,
            "alloc": [{"method": "cash", "amount": order.total_cents, "ref": "MULTI-REWARD-PAY"}],
            "reason": "Full return",
        },
        "lines": [{"key": "L1", "qty": 2}, {"key": "L2", "qty": 1}],
        "total": body["total"],
        "status": "refunded",
    }
    for _ in range(2):
        response = post(till, f"/orders/{order.number}/adjustments", refund)
        assert response.status_code == 200, response.data
        card.refresh_from_db()
        assert card.stamps == 20
    assert order.adjustments.count() == 1

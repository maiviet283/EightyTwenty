"""
Till API (POS + kitchen display), mounted at /api/v1/pos. Shapes follow the till's
src/services/contracts.ts; the guide is apps/pos/guide.md, published at /api/docs/pos/.
Views stay thin: rules live in services.py, shapes in presenters.py.
"""

import hashlib
import json
from functools import wraps

from django.db import IntegrityError, transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import STAFF_ROLES
from apps.core.public_services import categories_data
from apps.loyalty import services as loyalty
from apps.menu.catalog import menu_items
from apps.mobile.errors import AppError, app_exception_handler
from apps.mobile.serializers import ErrorOut
from apps.orders.models import Order

from . import examples as X
from . import pins, services
from . import presenters as p
from . import serializers as s
from .models import CommandReceipt
from .staff_sessions import (
    issue_staff_session,
    require_embedded_payment_staff,
    require_open_shift_staff,
    require_order_staff,
    require_shift_staff,
    require_staff_access,
    session_max_age,
)

DEVICE, START, ORDERS, MONEY, CASH, GUESTS = (
    "POS · Device & staff",
    "POS · Start-up & catalogue",
    "POS · Orders & kitchen",
    "POS · Payments & refunds",
    "POS · Cash & records",
    "POS · Customers & bookings",
)
IDEMPOTENCY = OpenApiParameter(
    "Idempotency-Key",
    str,
    OpenApiParameter.HEADER,
    description="One per command, reused on every retry (the till's outbox entry id).",
)
TERMINAL = OpenApiParameter(
    "X-Terminal-Id", str, OpenApiParameter.HEADER, description="This till, e.g. PHILLIP-TILL-1."
)
ORDER_ID = OpenApiParameter(
    "order_id", str, OpenApiParameter.PATH, description="Order number, URL-encoded (#→%23)."
)


def doc(tag, summary, request=None, responses=None, errors=(), write=False, **kwargs):
    responses = dict(responses or {})
    for code in errors:
        responses.setdefault(code, ErrorOut)
    parameters = [*kwargs.pop("parameters", []), TERMINAL, *([IDEMPOTENCY] if write else [])]
    return extend_schema(
        tags=[tag],
        summary=summary,
        request=request,
        responses=responses,
        parameters=parameters,
        **kwargs,
    )


def fingerprint(request):
    body = json.dumps(request.data, sort_keys=True, default=str)
    return hashlib.sha256(f"{request.method} {request.path}\n{body}".encode()).hexdigest()


def idempotent(handler):
    """
    Runs a write in one transaction and keeps its answer per Idempotency-Key: a retry with the
    same key and body gets the same answer without doing the work twice; the same key with a
    different body → 409 CONFLICT. Errors are not kept, so a corrected retry can succeed.
    """

    @wraps(handler)
    def wrapper(self, request, *args, **kwargs):
        key = request.headers.get("Idempotency-Key", "").strip()[:100]
        with transaction.atomic():
            if not key:
                return handler(self, request, *args, **kwargs)
            digest = fingerprint(request)
            receipts = CommandReceipt.objects.select_for_update()
            receipt = receipts.filter(user=request.user, key=key).first()
            if receipt is None:
                try:
                    with transaction.atomic():
                        receipt = CommandReceipt.objects.create(
                            user=request.user, key=key, request_hash=digest
                        )
                except IntegrityError:  # the same command arrived twice at once
                    receipt = receipts.get(user=request.user, key=key)
            if receipt.status_code:
                if receipt.request_hash != digest:
                    raise AppError(
                        "CONFLICT",
                        "This Idempotency-Key was already used for a different request.",
                        409,
                    )
                return Response(receipt.response, status=receipt.status_code)
            response = handler(self, request, *args, **kwargs)
            receipt.status_code = response.status_code
            receipt.response = response.data
            receipt.save(update_fields=["status_code", "response", "updated_at"])
            return response

    return wrapper


class IsTillUser(BasePermission):
    message = "This account can’t use the till. Sign in with a café team account."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user.is_authenticated
            and user.is_active
            and user.role in STAFF_ROLES
            and services.till_locations(user).exists()
        )


class PosView(APIView):
    permission_classes = [IsTillUser]

    def get_exception_handler(self):
        return app_exception_handler

    def get_throttles(self):
        # Tills poll all day and replay their outbox after being offline (a 429 there would be
        # rejected, losing the command), so only PIN checks are limited.
        return super().get_throttles() if getattr(self, "throttle_scope", None) else []

    def valid(self, serializer):
        data = serializer(data=self.request.data)
        data.is_valid(raise_exception=True)
        return data.validated_data

    def order_response(self, order, status=200):
        order = p.with_details(Order.objects.filter(pk=order.pk)).get()
        return Response(s.PosOrderOut(p.order(order)).data, status=status)


# ---------------------------------------------------------------- device & staff


class LocationsView(PosView):
    @doc(DEVICE, "Cafés this till account works at", responses={200: s.PosLocationListOut})
    def get(self, request):
        """
        The cafés the signed-in account may run a till for (chain-wide accounts: every
        published café). `id` and `slug` are both the café slug the till uses as `cafe`.
        """
        rows = services.till_locations(request.user)
        return Response({"results": [{"id": c.slug, "slug": c.slug, "name": c.name} for c in rows]})


class VerifyPinView(PosView):
    throttle_scope = "pin"

    @doc(
        DEVICE,
        "Check a staff PIN",
        s.PosPinIn,
        {200: s.PosPinOut, 204: None},
        (401, 403, 429),
        examples=X.VERIFY_PIN,
    )
    def post(self, request):
        """
        `StaffAuthService.verifyPin`. Returns the team member and the permissions the till
        should apply (the role matrix in the snapshot's `roles`). **204 No Content = PIN not
        recognised** (the adapter returns `null`). Wrong PINs count towards a limit of 20 checks
        per minute per till account (`429`).

        PINs are set in the BOS workspace (People → Users → Till PIN), 4 digits, unique across
        the chain. Only people who work at one of this account's cafés (or chain-wide accounts)
        are found.
        """
        data = self.valid(s.PosPinIn)
        user = pins.find_by_pin(data["pin"], services.till_locations(request.user).values("pk"))
        if user is None:
            return Response(status=204)
        location = (
            services.till_locations(request.user)
            .filter(staffassignment__user=user, staffassignment__is_active=True)
            .first()
        )
        return Response(
            s.PosPinOut(
                {
                    "staff": p.staff_member(user, location.slug if location else ""),
                    "permissions": pins.permissions(user),
                    "staffToken": issue_staff_session(request, user),
                    "staffTokenExpiresIn": session_max_age(),
                }
            ).data
        )


# ---------------------------------------------------------------- start-up & catalogue


class SnapshotView(PosView):
    @doc(
        START,
        "Everything one till needs to start trading",
        responses={200: s.PosSnapshotOut},
        errors=(401, 403, 404),
        parameters=[
            OpenApiParameter("cafe", str, description="Café slug; default: the first café.")
        ],
        examples=X.SNAPSHOT,
    )
    def get(self, request):
        """
        `PosBackend.loadSnapshot`, for one café (`?cafe=braddon`; send the till's saved café,
        and load again when the till switches café).

        - `locations`: every café this account works at (website shape, `id` = slug).
        - `menu`: **this café's** menu at its prices, website shape (`price` and option prices in
          AUD). `img` is the website path (`img/menu-original/…webp`, which the till maps to its
          own `img-pos/…jpg`), or an absolute URL for photos added later in BOS.
        - `orders`: this café's orders from today plus every order still live (app orders
          included, `channel: "app"`).
        - `staff`: people with a till PIN who work here (and chain-wide accounts).
        - `currentShift`: the open cash shift of this till (`X-Terminal-Id`), else `null`.
        - `reports`: last 7 days / 4 weeks from real orders (gross totals).
        - `customers`: the app's customers (newest 2,000) with their coffee card as stamps/rewards.
        - `reservations`: this café's bookings from 2 days ago to 30 days ahead (all sources).
        - `catering`: catering enquiries for this account's cafés (or with no café) from the last
          90 days or still to come.
        - `availability.soldOut`: sold out at every café of this account; `branchOff[cafe]`: here.

        - `tables`: this café's floor plan (BOS → Tables), `promotions`: promo codes live today
          at this café (BOS → Promo codes).
        - `reports`: also coffee card, booking and catering figures for the dashboard, and the
          "vs last week" lines.

        Not connected yet and therefore empty: `stock`, `stockLog`, `giftCards`.
        """
        cafe = request.query_params.get("cafe")
        location = (
            services.location_for(request.user, cafe)
            if cafe
            else services.default_location(request.user)
        )
        return Response(p.snapshot(request.user, location, services.terminal_of(request)))


class CategoriesView(PosView):
    @doc(START, "A café's menu categories", responses={200: s.PosCategoryOut(many=True)})
    def get(self, request, slug):
        """Active categories in menu order: `{id, name, note}`."""
        services.location_for(request.user, slug)
        return Response(categories_data())


class MenuView(PosView):
    @doc(START, "A café's menu (cents)", responses={200: s.PosMenuItemDtoOut(many=True)})
    def get(self, request, slug):
        """
        The till's `PosMenuItemDto` list (src/services/http/posMapping.ts): prices in cents,
        `available: false` = sold out at this café. Same items as the snapshot's `menu`.
        """
        location = services.location_for(request.user, slug)
        return Response([p.menu_dto(entry) for entry in menu_items(location)])


# ---------------------------------------------------------------- orders & kitchen


class OrdersView(PosView):
    @doc(
        ORDERS,
        "Orders changed since a cursor (website/app orders and other tills)",
        responses={200: s.PosOrderFeedOut},
        errors=(401, 403, 404, 422),
        parameters=[
            OpenApiParameter("cafe", str, required=True),
            OpenApiParameter("since", str, description="`cursor` from the previous response"),
        ],
    )
    def get(self, request):
        """
        `OrdersService.listChanged`. Poll every 15 s at the counter, 5 s on a kitchen display.
        Without `since`: today's orders plus every live one. With `since`: every order at the café
        that changed since (new app orders, payments, kitchen progress and status from other
        devices), oldest first, at most 500 — poll again at once when 500 come back. Changes
        overlap the cursor by a few seconds; merge by `id`.
        """
        location = services.location_for(request.user, request.query_params.get("cafe", ""))
        since = services.parse_since(request.query_params.get("since"))
        rows, cursor = services.changed_since(request.user, location, since)
        rows = p.with_details(Order.objects.filter(pk__in=[row.pk for row in rows]))
        return Response(
            {
                "orders": [p.order(row) for row in rows.order_by("updated_at")],
                "cursor": cursor.isoformat(),
            }
        )

    @doc(
        ORDERS,
        "Record an order rung up at the till",
        s.PosOrderIn,
        {201: s.PosOrderOut},
        (401, 403, 404, 409, 422),
        write=True,
        examples=X.CREATE_ORDER,
    )
    @require_embedded_payment_staff
    @idempotent
    def post(self, request):
        """
        `OrdersService.create` — the till's `Order` as it is (status `new`, usually unpaid; a gift
        card applied in the cart arrives as the first entry of `payments`).

        **Checks** (first failure returned): café on this account (`404`) · `channel` is `pos`
        (`422`) · `id` not used before (`409 CONFLICT` — every till must number its orders
        uniquely, e.g. `<VITE_TERMINAL_ID>-<n>`; a retry with the same Idempotency-Key replays
        instead) · `total` = Σ(price × qty) − discounts + delivery (10 %) to the cent (`422`).

        **Prices are not rejected**: the sale already happened at the counter. Lines whose price
        differs from the café's current menu (or items/options not on it, or sold-out items) are
        recorded and listed on the order for a manager in BOS (Orders → "Check prices").
        """
        order = services.create_order(request, self.valid(s.PosOrderIn))
        return self.order_response(order, 201)


class OrderStatusView(PosView):
    @doc(ORDERS, "Move an order along", s.PosStatusIn, {200: s.PosOrderOut}, (404, 409, 422), True)
    @idempotent
    def post(self, request, order_id):
        """
        `OrdersService.setStatus` with `new`, `preparing`, `ready` or `completed` (cancel and
        refund go through `/adjustments`). Repeating the current status is a no-op. A finished
        order (completed, cancelled, refunded) → `409 CONFLICT`. App customers are notified.
        """
        data = self.valid(s.PosStatusIn)
        order = services.order_for(request.user, order_id, lock=True)
        return self.order_response(
            services.set_status(request, order, data["status"], data["note"])
        )


class AcceptView(PosView):
    @doc(ORDERS, "Accept a website/app order", None, {200: s.PosOrderOut}, (404, 409), True)
    @idempotent
    def post(self, request, order_id):
        """
        `OrdersService.accept`: an app order waiting for the café becomes `preparing`
        (`accepted: true`) and the customer is told. Already accepted → no-op.
        """
        order = services.order_for(request.user, order_id, lock=True)
        return self.order_response(services.accept(request, order))


class PreparationView(PosView):
    @doc(
        ORDERS,
        "Kitchen display progress",
        s.PosPrepIn,
        {200: s.PosOrderOut},
        (404, 409, 422),
        True,
        examples=X.PREPARATION,
    )
    @idempotent
    def put(self, request, order_id):
        """
        `OrdersService.setPrep`: each line's `prep` (`new` → `preparing` → `ready`, by line `key`)
        and the order status they imply (`ready` only when every line is ready). An order that is
        not live — completed, cancelled, refunded, or an app order not accepted yet — →
        `409 CONFLICT` "Order … is completed; no change made."
        """
        data = self.valid(s.PosPrepIn)
        order = services.order_for(request.user, order_id, lock=True)
        return self.order_response(services.set_prep(request, order, data))


class AdjustmentsView(PosView):
    @doc(
        MONEY,
        "Refund, void or cancel",
        s.PosAdjustIn,
        {200: s.PosOrderOut},
        (404, 409, 422),
        True,
        examples=X.ADJUSTMENT,
    )
    @require_order_staff("orders.refund")
    @idempotent
    def post(self, request, order_id):
        """
        `OrdersService.adjust`. Idempotent on `adjustment.key`.

        Checks: `adjustment.amount` ≤ paid − already returned · `lines` (key + qty after the
        change) only lower quantities (voided lines are kept with qty 0 and drop out of `lines`)
        · `total` not above the current total · the order is not already cancelled or refunded.
        Cancelling an app order returns its stamps and gift card spend and tells the customer.
        """
        data = self.valid(s.PosAdjustIn)
        order = services.order_for(request.user, order_id, lock=True)
        return self.order_response(services.adjust(request, order, data))


class TablesView(PosView):
    @doc(
        ORDERS,
        "Move, join, separate or clear tables",
        s.PosTablesIn,
        {200: s.PosOrderOut},
        (404,),
        True,
    )
    @idempotent
    def put(self, request, order_id):
        """`OrdersService.setTables`: `table` (main), `tables` (all seats), `cleared`."""
        data = self.valid(s.PosTablesIn)
        order = services.order_for(request.user, order_id, lock=True)
        return self.order_response(services.set_tables(request, order, data))


class PaymentsView(PosView):
    @doc(
        MONEY,
        "Record a tender already taken",
        s.PosPaymentIn,
        {200: s.PosOrderOut},
        (404, 409, 422),
        True,
        examples=X.PAYMENT,
    )
    @require_order_staff("pos")
    @idempotent
    def post(self, request, order_id):
        """
        `PaymentsService.record`: cash counted, a card approved on the terminal, a gift card or a
        reward. Idempotent on `ref` (a repeated `ref` is ignored). `amount` (cents, without tip and
        surcharge) may not exceed what is still due → `422`. A cancelled or refunded order →
        `409`. No money moves here: the card terminal is not connected to the server.
        """
        data = self.valid(s.PosPaymentIn)
        order = services.order_for(request.user, order_id, lock=True)
        return self.order_response(services.record_payment(request, order, data))


# ---------------------------------------------------------------- cash & records


class ShiftsView(PosView):
    @doc(CASH, "Open a cash shift", s.PosShiftIn, {201: s.PosShiftOut}, (404, 409, 422), True)
    @require_open_shift_staff
    @idempotent
    def post(self, request):
        """
        `TransactionRecordsService.openShift` with the till's `CashShift` (float in cents). One
        open shift per till (`X-Terminal-Id`); a second → `409`.
        """
        shift = services.open_shift(request, self.valid(s.PosShiftIn))
        return Response(s.PosShiftOut(p.shift(shift)).data, status=201)


class ShiftView(PosView):
    @doc(
        CASH,
        "Close a cash shift",
        s.PosCloseShiftIn,
        {200: s.PosShiftOut},
        (404, 409, 422),
        True,
        examples=X.CLOSE_SHIFT,
    )
    @require_shift_staff("cash.out")
    @idempotent
    def put(self, request, shift_id):
        """
        `TransactionRecordsService.closeShift` with the till's `ClosedShift`. The server checks
        `expected` = float + cashSales − cashRefunds + cash in − cash out and `variance` =
        counted − expected (`422` when they do not add up). Closing again with the same count is
        a no-op.
        """
        data = self.valid(s.PosCloseShiftIn)
        shift = services.shift_for(request.user, shift_id, lock=True)
        return Response(s.PosShiftOut(p.shift(services.close_shift(request, shift, data))).data)


class ShiftMovesView(PosView):
    @doc(CASH, "Cash in / cash out", s.PosCashMoveIn, {200: s.PosShiftOut}, (404, 409, 422), True)
    @require_shift_staff("cash.out")
    @idempotent
    def post(self, request, shift_id):
        """`TransactionRecordsService.recordCashMove`. Idempotent on the move `id`."""
        data = self.valid(s.PosCashMoveIn)
        shift = services.shift_for(request.user, shift_id, lock=True)
        return Response(s.PosShiftOut(p.shift(services.cash_move(request, shift, data))).data)


class TransactionsView(PosView):
    @doc(CASH, "Record money moving", s.PosTransactionIn, {200: s.PosOkOut}, (404, 422), True)
    @idempotent
    def post(self, request):
        """
        `TransactionRecordsService.submit`: the till's immutable `TransactionRecord` (sale,
        payment, refund, void, cancel, cash in/out). Kept for the ledger and invoicing later.
        Idempotent on café + `kind` + `reference`.
        """
        services.record_transaction(request, self.valid(s.PosTransactionIn))
        return Response({"ok": True})


class AuditView(PosView):
    @doc(CASH, "Add to the activity log", s.PosAuditIn, {200: s.PosOkOut}, (404, 422), True)
    @idempotent
    def post(self, request):
        """
        `TransactionRecordsService.appendAudit`: the till's `AuditEntry` (who, what, approver).
        Shown in the BOS activity log as "pos: …" and returned in the snapshot's `audit`.
        Idempotent on `id`.
        """
        services.append_audit(request, self.valid(s.PosAuditIn))
        return Response({"ok": True})


# ---------------------------------------------------------------- customers & bookings


class CustomerView(PosView):
    @doc(GUESTS, "Edit a customer", s.PosCustomerPatchIn, {200: s.PosCustomerOut}, (404, 409), True)
    @require_staff_access("customers")
    @idempotent
    def put(self, request, customer_id):
        """
        `CustomersService.update` with the fields the till edits: `name`, `phone`, `email`,
        `homeCafe`, `dietary`, `notes` (any subset). Customers are the app's customer accounts,
        shared across cafés; an email another customer uses → `409 CONFLICT`.
        """
        data = self.valid(s.PosCustomerPatchIn)
        customer = services.customer_for(customer_id)
        services.update_customer(request, customer, data)
        return Response(s.PosCustomerOut(p.customer(customer)).data)


class CustomerLoyaltyView(PosView):
    @doc(GUESTS, "A customer's coffee card", None, {200: s.PosLoyaltyBalanceOut}, (403, 404))
    @require_staff_access("loyalty.stamp")
    def get(self, request, customer_id):
        """
        The card as it is now, read before a stamp or a free coffee at the counter (the snapshot
        can be minutes old): `rewards` free coffees ready + `stamps` (0–8) towards the next one.
        Needs `X-POS-Staff`.
        """
        data = p.customer(services.customer_for(customer_id))
        return Response({"id": data["id"], "stamps": data["stamps"], "rewards": data["rewards"]})

    @doc(
        GUESTS,
        "Set a customer's coffee card",
        s.PosLoyaltyIn,
        {200: s.PosCustomerOut},
        (404,),
        True,
        examples=X.LOYALTY,
    )
    @idempotent
    def put(self, request, customer_id):
        """
        `LoyaltyService.setBalance`: the card after a stamp at the counter, a free coffee redeemed
        or a refund. This is the **same card as the app**: every 9 stamps is one free coffee and
        free coffees add up — `rewards` = free coffees ready, `stamps` (0–8) = towards the next
        one. The customer sees the change in the app's card history and feed.
        """
        data = self.valid(s.PosLoyaltyIn)
        customer = services.customer_for(customer_id)
        services.set_card_balance(request, customer, data["stamps"], data["rewards"])
        return Response(s.PosCustomerOut(p.customer(customer)).data)


class CustomerGiftCardView(PosView):
    @doc(MONEY, "A customer's gift card", None, {200: s.PosGiftCardOut}, (403, 404))
    @require_staff_access("gift.redeem")
    def get(self, request, customer_id):
        """
        The customer's gift card (the same balance as the app and BOS), read before taking a
        gift card payment. `balance` is in cents. Pay with `method: gift` + `giftCardId` = this
        `id`: the server takes the amount off the card in the same transaction (a repeated `ref`
        is not taken twice; more than the balance → `409`). A refund or cancellation allocated to
        that payment puts the money back on the same card, once. Needs `X-POS-Staff`.
        """
        customer = services.customer_for(customer_id)
        card = loyalty.gift_card_for(customer)
        return Response(
            {
                "id": str(card.pk),
                "customerId": str(customer.pk),
                "last4": card.last4,
                "balance": card.balance_cents,
            }
        )


class SoldOutView(PosView):
    @doc(GUESTS, "Sold out at every café", s.PosSoldOutIn, {200: s.PosOkOut}, (404,), True)
    @idempotent
    def put(self, request, item_id):
        """
        `CatalogService.setSoldOut`: marks an item sold out (or back on sale) at **every café
        this till account works at** — for a café team account, that is its own café. Website,
        app and BOS Availability board show it at once.
        """
        data = self.valid(s.PosSoldOutIn)
        services.set_sold_out(request, item_id, data["soldOut"])
        return Response({"ok": True})


class CafeAvailabilityView(PosView):
    @doc(
        GUESTS,
        "On / off the menu at one café",
        s.PosAvailabilityIn,
        {200: s.PosOkOut},
        (404,),
        True,
    )
    @idempotent
    def put(self, request, slug, item_id):
        """`CatalogService.setBranchAvailability` for one café (`available: false` = sold out there)."""
        data = self.valid(s.PosAvailabilityIn)
        services.set_cafe_availability(request, slug, item_id, data["available"])
        return Response({"ok": True})


class ReservationView(PosView):
    @doc(
        GUESTS,
        "Create or update a reservation",
        s.PosReservationIn,
        {200: s.PosReservationOut},
        (404, 409, 422),
        True,
        examples=X.RESERVATION,
    )
    @idempotent
    def put(self, request, reservation_id):
        """
        `ReservationsService.upsert`: the same bookings the website and app create (BOS →
        Bookings). An unknown `id` creates a booking made by the café (no booking rules applied,
        no email); a known one is updated.

        Status: `pending` (requested) → `confirmed` → `completed` (seated), or `cancelled`. The
        guest is emailed (when there is an email) and app customers get a feed line on each
        change, as in BOS. A finished booking (seated, cancelled, declined, no-show) can't move
        again → `409`. `table` is not stored yet.
        """
        data = self.valid(s.PosReservationIn)
        booking = services.upsert_reservation(request, reservation_id, data)
        return Response(s.PosReservationOut(p.reservation(booking)).data)


class CateringView(PosView):
    @doc(
        GUESTS,
        "Create or update a catering enquiry",
        s.PosCateringIn,
        {200: s.PosCateringOut},
        (404, 409, 422),
        True,
    )
    @idempotent
    def put(self, request, enquiry_id):
        """
        `CateringService.upsert`: the same catering enquiries the website and app send (BOS →
        Enquiries). Unknown `id` → created; known → updated. Status `enquiry` ↔ new / in
        progress, `quoted`, `confirmed` (won), `delivered` (closed), `cancelled` (lost).
        `packageId` is the package name; time, fulfilment, address, contact, dietary, occasion and
        quote are kept with the enquiry.
        """
        data = self.valid(s.PosCateringIn)
        enquiry = services.upsert_catering(request, enquiry_id, data)
        return Response(s.PosCateringOut(p.catering_enquiry(enquiry)).data)


class CategoryOrderView(PosView):
    @doc(
        GUESTS,
        "Set category order for one cafe",
        s.PosCategoryOrderIn,
        {200: s.PosOkOut},
        (403, 404, 422),
        True,
    )
    @idempotent
    def put(self, request, slug):
        data = self.valid(s.PosCategoryOrderIn)
        services.set_category_order(request, slug, data["ids"])
        return Response({"ok": True})


class ItemPriceView(PosView):
    @doc(
        GUESTS,
        "Set an item price for one cafe",
        s.PosItemPriceIn,
        {200: s.PosOkOut},
        (403, 404, 422),
        True,
    )
    @idempotent
    def put(self, request, slug, item_id):
        data = self.valid(s.PosItemPriceIn)
        services.set_item_price(request, slug, item_id, data["price"])
        return Response({"ok": True})


class ModifierAvailabilityView(PosView):
    @doc(
        GUESTS,
        "Set modifier availability at one cafe",
        s.PosModifierAvailabilityIn,
        {200: s.PosOkOut},
        (403, 404, 422),
        True,
    )
    @idempotent
    def put(self, request, slug):
        data = self.valid(s.PosModifierAvailabilityIn)
        services.set_modifier_availability(request, slug, data["label"], data["available"])
        return Response({"ok": True})


class TillTogglesView(PosView):
    @doc(
        START,
        "Update cafe operational toggles",
        s.PosTogglesIn,
        {200: s.PosOkOut},
        (403, 404, 422),
        True,
    )
    @idempotent
    def put(self, request, slug):
        data = self.valid(s.PosTogglesIn)
        services.update_till_toggles(request, slug, data["toggles"])
        return Response({"ok": True})


##### loyalty #####
class CustomerLoyaltyActionView(PosView):
    @doc(
        GUESTS,
        "Add a coffee stamp or redeem a reward",
        s.PosLoyaltyActionIn,
        {200: s.PosCustomerOut},
        (403, 404, 409, 422),
        True,
    )
    @require_staff_access("loyalty.stamp")
    @idempotent
    def post(self, request, customer_id):
        data = self.valid(s.PosLoyaltyActionIn)
        customer = services.customer_for(customer_id)
        services.apply_loyalty_action(request, customer, data)
        customer = services.customer_for(customer_id)
        return Response(s.PosCustomerOut(p.customer(customer)).data)

"""
Request validation and response shapes for /api/v1/pos (camelCase, as the till's
src/domain/types.ts). Serializers only check shapes; rules live in services.py.
"""

from decimal import Decimal

from rest_framework import serializers

TILL_STATUSES = ["new", "preparing", "ready", "completed", "refunded", "cancelled", "failed"]
ORDER_TYPES = ["dinein", "pickup", "takeaway", "delivery"]
PREP = ["new", "preparing", "ready"]
METHODS = ["cash", "card", "wallet", "gift", "reward"]
ROLES = ["cashier", "manager", "admin"]
SHIFT_STATUSES = ["open", "closed"]
MOVE_TYPES = ["in", "out"]
ADJUSTMENT_MODES = ["cancel", "void", "refund"]
SPLIT_MODES = ["full", "equal", "items", "amount"]
RESERVATION_STATUSES = ["pending", "confirmed", "completed", "cancelled"]
CATERING_STATUSES = ["enquiry", "quoted", "confirmed", "delivered", "cancelled"]
CHANNELS = ["pos", "web", "app"]
ORDER_CHANNELS = ["pos", "app"]


def text(max_length, **kwargs):
    return serializers.CharField(
        max_length=max_length, required=False, allow_blank=True, default="", **kwargs
    )


def cents(min_value=0, **kwargs):
    return serializers.IntegerField(min_value=min_value, max_value=10_000_000, **kwargs)


def dollars(**kwargs):
    return serializers.FloatField(min_value=0, max_value=100_000, **kwargs)


# ---------------------------------------------------------------- requests


class PosPinIn(serializers.Serializer):
    pin = serializers.CharField(max_length=10, help_text="4 digits.")


class PosHistoryIn(serializers.Serializer):
    at = serializers.CharField(max_length=60)
    what = serializers.CharField(max_length=500)


class PosLineIn(serializers.Serializer):
    key = serializers.CharField(max_length=40)
    itemId = serializers.CharField(max_length=50, help_text="Menu item id (website slug).")
    name = serializers.CharField(max_length=160)
    price = dollars(help_text="Unit price incl. chosen options, AUD.")
    qty = serializers.IntegerField(min_value=1, max_value=999)
    sel = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField(max_length=120)),
        required=False,
        default=dict,
        help_text="Modifier group id → chosen option labels.",
    )
    adjust = serializers.ListField(
        child=serializers.CharField(max_length=120), required=False, default=list
    )
    note = text(500)
    prep = serializers.ChoiceField(choices=PREP, required=False)


class PosDiscountIn(serializers.Serializer):
    label = serializers.CharField(max_length=120)
    amount = dollars()
    code = serializers.CharField(
        max_length=30,
        required=False,
        allow_blank=True,
        help_text="Optional: the promo code applied (BOS → Promo codes). Without it the server "
        "looks for a code in the label. A wrong, expired or too large code is noted on the "
        "order for a manager (Check prices), never refused.",
    )


class PosPaymentIn(serializers.Serializer):
    method = serializers.ChoiceField(choices=METHODS)
    amount = cents(help_text="Cents applied to the order (no tip, no surcharge).")
    tip = cents(required=False, default=0)
    surcharge = cents(required=False, default=0)
    ref = serializers.CharField(max_length=80, help_text="Idempotency reference of this tender.")
    giftLast4 = serializers.CharField(
        max_length=4, required=False, allow_null=True, allow_blank=True
    )
    giftCardId = serializers.CharField(
        max_length=40,
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text="`method: gift`: the card's `id` from `GET /customers/{id}/gift-card`. The "
        "server takes `amount` off its balance; a refund puts it back.",
    )
    at = text(40)
    by = text(120)
    mode = serializers.ChoiceField(choices=SPLIT_MODES, required=False)
    items = serializers.DictField(child=serializers.IntegerField(min_value=0), required=False)
    cashPaid = cents(required=False, allow_null=True)
    rounding = serializers.IntegerField(min_value=-100, max_value=100, required=False, default=0)
    change = cents(required=False, allow_null=True)
    shiftId = serializers.CharField(max_length=60, required=False)

    def validate(self, data):
        rounding = data.get("rounding", 0)
        cash_paid = data.get("cashPaid")
        change = data.get("change")

        if data["method"] != "cash":
            if rounding != 0 or cash_paid is not None or change is not None:
                raise serializers.ValidationError(
                    {"method": "Cash details are only allowed for cash payments."}
                )
            return data

        rounded = ((data["amount"] + 2) // 5) * 5
        if rounding != rounded - data["amount"]:
            raise serializers.ValidationError(
                {"rounding": "Cash rounding must match the nearest five cents."}
            )
        if cash_paid is not None and cash_paid != rounded:
            raise serializers.ValidationError(
                {"cashPaid": "Cash paid must match the rounded payment amount."}
            )

        return data


class PosOrderIn(serializers.Serializer):
    id = serializers.CharField(max_length=40, help_text="Unique across every till and the app.")
    channel = serializers.ChoiceField(choices=CHANNELS)
    type = serializers.ChoiceField(choices=ORDER_TYPES)
    status = serializers.ChoiceField(choices=TILL_STATUSES, required=False, default="new")
    accepted = serializers.BooleanField(required=False)
    cafe = serializers.CharField(max_length=50)
    time = text(5)
    placedAt = serializers.DateTimeField(required=False, allow_null=True)
    table = serializers.CharField(max_length=20, required=False, allow_null=True, allow_blank=True)
    tables = serializers.ListField(
        child=serializers.CharField(max_length=20), required=False, default=list
    )
    customerId = serializers.CharField(
        max_length=60, required=False, allow_null=True, allow_blank=True
    )
    lines = PosLineIn(many=True, allow_empty=False)
    total = dollars(help_text="AUD: lines − discounts + delivery (no card surcharge).")
    paid = serializers.BooleanField(required=False)
    methods = serializers.ListField(child=serializers.CharField(), required=False)
    payments = PosPaymentIn(many=True, required=False, default=list)
    note = text(1000)
    discounts = PosDiscountIn(many=True, required=False, default=list)
    rewardUsed = serializers.BooleanField(required=False, default=False)
    staff = text(120)
    history = PosHistoryIn(many=True, required=False, default=list)
    serverLoyalty = serializers.BooleanField(required=False, default=False)


class PosStatusIn(serializers.Serializer):
    status = serializers.ChoiceField(choices=TILL_STATUSES)
    note = text(300)


class PosPrepLineIn(serializers.Serializer):
    key = serializers.CharField(max_length=40)
    prep = serializers.ChoiceField(choices=PREP)


class PosPrepIn(serializers.Serializer):
    lines = PosPrepLineIn(many=True)
    status = serializers.ChoiceField(choices=TILL_STATUSES)
    note = text(300)


class PosRefundAllocationIn(serializers.Serializer):
    method = serializers.ChoiceField(choices=METHODS)
    amount = cents(min_value=1)
    ref = serializers.CharField(max_length=80)
    giftLast4 = serializers.CharField(
        max_length=4, required=False, allow_null=True, allow_blank=True, default=None
    )


class PosAdjustmentIn(serializers.Serializer):
    key = serializers.CharField(max_length=60)
    mode = serializers.ChoiceField(choices=ADJUSTMENT_MODES)
    amount = cents(help_text="Cents returned to the customer.")
    alloc = PosRefundAllocationIn(many=True, required=False, default=list)
    reason = text(200)
    by = text(120)
    approver = text(120)
    at = text(40)
    items = serializers.DictField(child=serializers.IntegerField(min_value=0), required=False)
    shiftId = serializers.CharField(max_length=60, required=False)

    def validate(self, data):
        allocations = data.get("alloc", [])

        if allocations:
            if sum(row["amount"] for row in allocations) != data["amount"]:
                raise serializers.ValidationError(
                    {"alloc": "Allocated amounts must equal the refund amount."}
                )

            references = [row["ref"] for row in allocations]
            if len(references) != len(set(references)):
                raise serializers.ValidationError(
                    {"alloc": "Each payment reference may appear only once."}
                )

        if data.get("shiftId") and data["amount"] > 0 and not allocations:
            raise serializers.ValidationError(
                {"alloc": "Refunds linked to a shift must include payment allocations."}
            )

        return data


class PosLineQtyIn(serializers.Serializer):
    key = serializers.CharField(max_length=40)
    qty = serializers.IntegerField(min_value=0, max_value=999)


class PosAdjustIn(serializers.Serializer):
    adjustment = PosAdjustmentIn()
    lines = PosLineQtyIn(many=True, help_text="The order's lines after the change (key + qty).")
    total = dollars(help_text="The order total after the change, AUD.")
    status = serializers.ChoiceField(choices=TILL_STATUSES)


class PosTablesIn(serializers.Serializer):
    table = serializers.CharField(max_length=20, required=False, allow_null=True, allow_blank=True)
    tables = serializers.ListField(child=serializers.CharField(max_length=20), default=list)
    cleared = serializers.BooleanField(required=False, allow_null=True)


class PosCashMoveIn(serializers.Serializer):
    id = serializers.CharField(max_length=40)
    type = serializers.ChoiceField(choices=MOVE_TYPES)
    amount = cents(min_value=1)
    reason = text(200)
    by = text(120)
    approver = text(120)
    at = text(40)


class PosShiftIn(serializers.Serializer):
    id = serializers.CharField(max_length=60)
    cafe = serializers.CharField(max_length=50)
    staffId = text(60)
    staffName = text(120)
    openedAt = text(40)
    float = cents(help_text="Opening float, cents.")
    cashSales = cents(required=False, default=0)
    cashRefunds = cents(required=False, default=0)
    moves = PosCashMoveIn(many=True, required=False, default=list)
    status = serializers.ChoiceField(choices=SHIFT_STATUSES, required=False, default="open")


class PosCloseShiftIn(PosShiftIn):
    status = serializers.ChoiceField(choices=["closed"])
    closedAt = text(40)
    closedBy = text(120)
    counted = cents()
    expected = serializers.IntegerField()
    variance = serializers.IntegerField()
    note = text(1000)
    approvedBy = text(120)


class PosTransactionIn(serializers.Serializer):
    kind = serializers.ChoiceField(
        choices=["sale", "payment", "refund", "void", "cancel", "gift-issue", "cash-in", "cash-out"]
    )
    orderId = text(40)
    cafeId = serializers.CharField(max_length=50)
    terminalId = text(60)
    amountC = serializers.IntegerField(help_text="Cents, GST inclusive; negative = money returned.")
    gstC = serializers.IntegerField()
    at = serializers.DateTimeField()
    staff = text(120)
    reference = serializers.CharField(max_length=80)


class PosAuditIn(serializers.Serializer):
    id = serializers.CharField(max_length=100)
    at = text(60)
    ts = serializers.IntegerField(required=False)
    staff = text(120)
    role = text(40)
    action = serializers.CharField(max_length=120)
    detail = text(500)
    reason = text(200)
    approver = text(120)
    amount = serializers.IntegerField(required=False, allow_null=True)
    area = text(60)
    cafe = serializers.CharField(max_length=50)


class PosCustomerPatchIn(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    homeCafe = serializers.CharField(max_length=50, required=False, allow_blank=True)
    dietary = serializers.ListField(child=serializers.CharField(max_length=60), required=False)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class PosLoyaltyIn(serializers.Serializer):
    stamps = serializers.IntegerField(min_value=0, max_value=99)
    rewards = serializers.IntegerField(min_value=0, max_value=99)
    reason = text(200)


class PosLoyaltyBalanceOut(serializers.Serializer):
    id = serializers.CharField()
    stamps = serializers.IntegerField(help_text="0–8 towards the next free coffee.")
    rewards = serializers.IntegerField(help_text="Free coffees ready to redeem (they add up).")


class PosGiftCardOut(serializers.Serializer):
    id = serializers.CharField(help_text="Send as `giftCardId` with a `gift` payment.")
    customerId = serializers.CharField()
    last4 = serializers.CharField()
    balance = serializers.IntegerField(help_text="Cents left on the card.")


class PosSoldOutIn(serializers.Serializer):
    soldOut = serializers.BooleanField()


class PosAvailabilityIn(serializers.Serializer):
    available = serializers.BooleanField()


class PosReservationIn(serializers.Serializer):
    id = serializers.CharField(max_length=20, help_text="Booking reference (the till's id).")
    cafe = serializers.CharField(max_length=50)
    date = serializers.DateField()
    time = serializers.TimeField(format="%H:%M")
    guests = serializers.IntegerField(min_value=1, max_value=200)
    name = serializers.CharField(max_length=160)
    phone = text(32)
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    notes = text(2000)
    status = serializers.ChoiceField(choices=RESERVATION_STATUSES)
    customerId = serializers.CharField(
        max_length=60, required=False, allow_null=True, allow_blank=True
    )
    seating = serializers.CharField(max_length=7, required=False, allow_blank=True)
    table = serializers.CharField(max_length=20, required=False, allow_blank=True)
    source = serializers.CharField(max_length=10, required=False, allow_blank=True)
    extRef = serializers.CharField(max_length=40, required=False, allow_blank=True)


class PosCateringIn(serializers.Serializer):
    id = serializers.CharField(max_length=20, help_text="Enquiry reference (the till's id).")
    packageId = text(120, help_text="Package name, as in the snapshot's cateringPackages.")
    guests = serializers.IntegerField(min_value=0, max_value=10_000, required=False, default=0)
    date = text(10, help_text="YYYY-MM-DD")
    time = text(5)
    fulfil = serializers.ChoiceField(choices=["delivery", "pickup"], required=False)
    address = text(300)
    cafe = text(50)
    name = serializers.CharField(max_length=160)
    contact = text(200)
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    phone = text(32)
    occasion = text(120)
    dietary = serializers.ListField(
        child=serializers.CharField(max_length=60), required=False, default=list
    )
    notes = text(4000)
    quote = dollars(required=False, default=0)
    status = serializers.ChoiceField(choices=CATERING_STATUSES)
    customerId = serializers.CharField(
        max_length=60, required=False, allow_null=True, allow_blank=True
    )
    source = serializers.CharField(max_length=10, required=False, allow_blank=True)
    extRef = serializers.CharField(max_length=40, required=False, allow_blank=True)


# ---------------------------------------------------------------- responses


class PosStaffOut(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    initials = serializers.CharField()
    role = serializers.ChoiceField(choices=ROLES)
    cafe = serializers.CharField()
    shift = serializers.CharField()


class PosPinOut(serializers.Serializer):
    staff = PosStaffOut()
    permissions = serializers.ListField(child=serializers.CharField())
    staffToken = serializers.CharField()
    staffTokenExpiresIn = serializers.IntegerField()


class PosLocationRefOut(serializers.Serializer):
    id = serializers.CharField(help_text="Café slug (the till's cafe id).")
    slug = serializers.CharField()
    name = serializers.CharField()


class PosLocationListOut(serializers.Serializer):
    results = PosLocationRefOut(many=True)


class PosCategoryOut(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    note = serializers.CharField(allow_blank=True)


class PosOptionDtoOut(serializers.Serializer):
    name = serializers.CharField()
    priceCents = serializers.IntegerField()


class PosGroupDtoOut(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    required = serializers.BooleanField()
    maxSelections = serializers.IntegerField(help_text="0 = any number.")
    options = PosOptionDtoOut(many=True)


class PosMenuItemDtoOut(serializers.Serializer):
    id = serializers.CharField()
    categoryId = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    priceCents = serializers.IntegerField()
    dietary = serializers.ListField(child=serializers.CharField())
    imageUrl = serializers.CharField(allow_null=True)
    featured = serializers.BooleanField()
    available = serializers.BooleanField()
    modifierGroups = PosGroupDtoOut(many=True)


class PosLineOut(serializers.Serializer):
    key = serializers.CharField()
    itemId = serializers.CharField()
    name = serializers.CharField()
    price = serializers.FloatField()
    qty = serializers.IntegerField()
    sel = serializers.DictField(child=serializers.ListField(child=serializers.CharField()))
    adjust = serializers.ListField(child=serializers.CharField())
    note = serializers.CharField(allow_blank=True)
    prep = serializers.ChoiceField(choices=PREP, required=False)


class PosPaymentOut(serializers.Serializer):
    method = serializers.ChoiceField(choices=METHODS)
    amount = serializers.IntegerField()
    tip = serializers.IntegerField()
    surcharge = serializers.IntegerField()
    ref = serializers.CharField()
    giftLast4 = serializers.CharField(allow_null=True)
    giftCardId = serializers.CharField(required=False)
    at = serializers.CharField(allow_blank=True)
    by = serializers.CharField(allow_blank=True)
    mode = serializers.CharField(required=False)
    items = serializers.DictField(child=serializers.IntegerField(), required=False)
    cashPaid = serializers.IntegerField(required=False)
    rounding = serializers.IntegerField(required=False)
    change = serializers.IntegerField(required=False)


class PosAdjustmentOut(serializers.Serializer):
    key = serializers.CharField()
    mode = serializers.ChoiceField(choices=ADJUSTMENT_MODES)
    amount = serializers.IntegerField()
    alloc = serializers.ListField(child=serializers.DictField())
    reason = serializers.CharField(allow_blank=True)
    by = serializers.CharField(allow_blank=True)
    approver = serializers.CharField(allow_blank=True)
    at = serializers.CharField(allow_blank=True)
    items = serializers.DictField(child=serializers.IntegerField())


class PosDiscountOut(serializers.Serializer):
    label = serializers.CharField()
    amount = serializers.FloatField()
    code = serializers.CharField(required=False, help_text="Promo code, when the till sent one.")


class PosHistoryOut(serializers.Serializer):
    at = serializers.CharField()
    what = serializers.CharField()


class PosOrderOut(serializers.Serializer):
    id = serializers.CharField(help_text="Order number, e.g. #8020-4471 (app) or PHL1-0042 (till).")
    channel = serializers.ChoiceField(choices=ORDER_CHANNELS)
    type = serializers.ChoiceField(choices=ORDER_TYPES)
    status = serializers.ChoiceField(choices=TILL_STATUSES)
    accepted = serializers.BooleanField(help_text="false = app order waiting for the café.")
    cafe = serializers.CharField()
    time = serializers.CharField(help_text="HH:MM café time (scheduled time for app orders).")
    placedAt = serializers.DateTimeField()
    table = serializers.CharField(required=False)
    tables = serializers.ListField(child=serializers.CharField())
    customerId = serializers.CharField(required=False)
    lines = PosLineOut(many=True)
    total = serializers.FloatField()
    paid = serializers.BooleanField()
    methods = serializers.ListField(child=serializers.ChoiceField(choices=METHODS))
    payments = PosPaymentOut(many=True)
    note = serializers.CharField(allow_blank=True)
    discounts = PosDiscountOut(many=True)
    rewardUsed = serializers.BooleanField()
    staff = serializers.CharField(allow_blank=True)
    history = PosHistoryOut(many=True)
    refunds = PosAdjustmentOut(many=True)
    extRef = serializers.CharField(required=False)
    stamped = serializers.BooleanField()
    cleared = serializers.BooleanField()
    serverLoyalty = serializers.BooleanField(required=False)


class PosOrderFeedOut(serializers.Serializer):
    orders = PosOrderOut(many=True)
    cursor = serializers.CharField(help_text="Send as `since` on the next poll.")


class PosMoveOut(serializers.Serializer):
    id = serializers.CharField()
    type = serializers.ChoiceField(choices=MOVE_TYPES)
    amount = serializers.IntegerField()
    reason = serializers.CharField(allow_blank=True)
    by = serializers.CharField(allow_blank=True)
    approver = serializers.CharField(allow_blank=True)
    at = serializers.CharField(allow_blank=True)


class PosShiftOut(serializers.Serializer):
    id = serializers.CharField()
    cafe = serializers.CharField()
    staffId = serializers.CharField(allow_blank=True)
    staffName = serializers.CharField(allow_blank=True)
    openedAt = serializers.CharField(allow_blank=True)
    float = serializers.IntegerField()
    cashSales = serializers.IntegerField()
    cashRefunds = serializers.IntegerField()
    moves = PosMoveOut(many=True)
    status = serializers.ChoiceField(choices=SHIFT_STATUSES)
    closedAt = serializers.CharField(required=False)
    closedBy = serializers.CharField(required=False)
    counted = serializers.IntegerField(required=False)
    expected = serializers.IntegerField(required=False)
    variance = serializers.IntegerField(required=False)
    note = serializers.CharField(required=False)
    approvedBy = serializers.CharField(required=False)


class PosCustomerOut(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    phone = serializers.CharField(allow_blank=True)
    email = serializers.CharField()
    stamps = serializers.IntegerField(help_text="0–8 towards the next free coffee.")
    rewards = serializers.IntegerField(help_text="Free coffees ready to redeem (they add up).")
    homeCafe = serializers.CharField(allow_blank=True)
    dietary = serializers.ListField(child=serializers.CharField())
    notes = serializers.CharField(allow_blank=True)
    since = serializers.CharField(help_text="Month joined, e.g. Mar 2024.")
    wallet = serializers.BooleanField()
    visits = serializers.IntegerField()
    spend = serializers.FloatField(help_text="Lifetime order totals, AUD.")
    lastDays = serializers.IntegerField(help_text="Days since the last order (999 = never).")


class PosReservationOut(serializers.Serializer):
    id = serializers.CharField()
    cafe = serializers.CharField()
    date = serializers.CharField()
    time = serializers.CharField()
    guests = serializers.IntegerField()
    name = serializers.CharField()
    phone = serializers.CharField(allow_blank=True)
    email = serializers.CharField(allow_blank=True)
    notes = serializers.CharField(allow_blank=True)
    status = serializers.ChoiceField(choices=RESERVATION_STATUSES)
    source = serializers.ChoiceField(choices=CHANNELS)
    customerId = serializers.CharField(required=False)
    seating = serializers.CharField(required=False)
    extRef = serializers.CharField(required=False)


class PosCateringOut(serializers.Serializer):
    id = serializers.CharField()
    packageId = serializers.CharField(allow_blank=True)
    guests = serializers.IntegerField()
    date = serializers.CharField(allow_blank=True)
    time = serializers.CharField(allow_blank=True)
    fulfil = serializers.ChoiceField(choices=["delivery", "pickup"])
    address = serializers.CharField(allow_blank=True)
    cafe = serializers.CharField(allow_blank=True)
    name = serializers.CharField()
    contact = serializers.CharField(allow_blank=True)
    email = serializers.CharField(allow_blank=True)
    phone = serializers.CharField(allow_blank=True)
    occasion = serializers.CharField(allow_blank=True)
    dietary = serializers.ListField(child=serializers.CharField())
    notes = serializers.CharField(allow_blank=True)
    quote = serializers.FloatField()
    status = serializers.ChoiceField(choices=CATERING_STATUSES)
    source = serializers.ChoiceField(choices=CHANNELS)
    customerId = serializers.CharField(required=False)
    extRef = serializers.CharField(required=False)


class PosOkOut(serializers.Serializer):
    ok = serializers.BooleanField()


class PosSnapshotOut(serializers.Serializer):
    locations = serializers.ListField(
        child=serializers.DictField(), help_text="Website café records (id = slug)."
    )
    categories = PosCategoryOut(many=True)
    menu = serializers.ListField(
        child=serializers.DictField(),
        help_text="This café's menu in the website shape: id, cat, name, price (AUD), diet, img, "
        "desc, mods [{id, name, required, max, options [[label, AUD]]}], featured.",
    )
    cateringPackages = serializers.ListField(child=serializers.DictField())
    tables = serializers.ListField(
        child=serializers.DictField(),
        help_text="This café's floor plan from BOS → Tables: [{id, seats, zone}].",
    )
    promotions = serializers.ListField(
        child=serializers.DictField(),
        help_text="Promo codes live today here (BOS → Promo codes): [{code, kind: pct|amt, "
        "value (percent or AUD), label}].",
    )
    availability = serializers.DictField()
    stock = serializers.ListField(child=serializers.DictField(), help_text="Not yet: [].")
    stockLog = serializers.ListField(child=serializers.DictField(), help_text="Not yet: [].")
    orders = PosOrderOut(many=True)
    customers = PosCustomerOut(many=True)
    giftCards = serializers.ListField(child=serializers.DictField(), help_text="Not yet: [].")
    reservations = PosReservationOut(many=True)
    catering = PosCateringOut(many=True)
    staff = PosStaffOut(many=True)
    roles = serializers.DictField()
    settings = serializers.DictField()
    currentShift = PosShiftOut(allow_null=True)
    closedShifts = PosShiftOut(many=True)
    audit = serializers.ListField(child=serializers.DictField())
    reports = serializers.DictField()


class PosCategoryOrderIn(serializers.Serializer):
    ids = serializers.ListField(child=serializers.SlugField(), allow_empty=True, max_length=200)

    def validate_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Category IDs must not be repeated.")
        return value


class PosItemPriceIn(serializers.Serializer):
    price = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal("0.00"))


class PosModifierAvailabilityIn(serializers.Serializer):
    label = serializers.CharField(max_length=120)
    available = serializers.BooleanField()


class PosTogglesIn(serializers.Serializer):
    toggles = serializers.DictField(child=serializers.BooleanField(), allow_empty=False)

    def validate_toggles(self, value):
        allowed = {"kitchen", "receipt", "tips", "openDrawer", "autoStamp"}
        if set(value) - allowed:
            raise serializers.ValidationError("One or more toggle names are unsupported.")
        return value

    def validate(self, data):
        if set(self.initial_data) - {"toggles"}:
            raise serializers.ValidationError("This endpoint only accepts operational toggles.")
        return data


class PosLoyaltyActionIn(serializers.Serializer):
    cafe = serializers.SlugField(max_length=50)
    action = serializers.ChoiceField(choices=["stamp", "redeem"])

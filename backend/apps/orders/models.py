import secrets

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel, ManagedModel


def order_number():
    return f"#8020-{secrets.randbelow(10**4):04d}"


class Order(ManagedModel):
    """
    An order from the mobile app (priced by the server, never by the client) or from a till
    (rung up by staff; checked against the menu, differences noted in `review_note`).
    """

    class Status(models.TextChoices):
        RECEIVED = "received"
        CONFIRMED = "confirmed"
        PREPARING = "preparing"
        READY = "ready"
        COMPLETED = "completed"
        DECLINED = "declined"
        CANCELLED = "cancelled"
        REFUNDED = "refunded"
        FAILED = "failed"

    class Channel(models.TextChoices):
        APP = "app", "App"
        POS = "pos", "Till"

    def __str__(self):
        return self.number

    number = models.CharField(max_length=40, unique=True, default=order_number, editable=False)
    channel = models.CharField(max_length=3, choices=Channel.choices, default=Channel.APP)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    location = models.ForeignKey("locations.Location", on_delete=models.PROTECT)
    method = models.CharField(
        max_length=8,
        choices=[
            ("pickup", "Pickup"),
            ("delivery", "Delivery"),
            ("dine_in", "Dine in"),
            ("takeaway", "Takeaway"),
        ],
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED)
    scheduled_time = models.TimeField(
        null=True, blank=True, help_text="Empty = as soon as possible (today, café time)."
    )
    delivery_address = models.CharField(max_length=300, blank=True)
    table_number = models.CharField(max_length=20, blank=True)
    contact_name = models.CharField(max_length=160, blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    payment_choice = models.CharField(
        max_length=10,
        blank=True,
        choices=[("apple_pay", "Apple Pay"), ("saved_card", "Saved card"), ("new_card", "Card")],
    )
    payment_label = models.CharField(max_length=60, blank=True)
    payment_reference = models.CharField(max_length=120, blank=True)
    subtotal_cents = models.PositiveIntegerField()
    delivery_cents = models.PositiveIntegerField(default=0)
    reward_cents = models.PositiveIntegerField(default=0)
    surcharge_cents = models.PositiveIntegerField(default=0)
    gift_card_cents = models.PositiveIntegerField(default=0)
    total_cents = models.PositiveIntegerField()
    stamps_earned = models.PositiveSmallIntegerField(default=0)
    pos_loyalty_processed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When POS loyalty was processed after full payment.",
    )
    pos_loyalty_reversed_at = models.DateTimeField(null=True, blank=True)
    pos_loyalty_lines = models.JSONField(default=dict, blank=True)
    pos_loyalty_managed = models.BooleanField(default=False)
    pos_reward_reserved_at = models.DateTimeField(null=True, blank=True)
    pos_reward_returned_at = models.DateTimeField(null=True, blank=True)
    reward_redeemed = models.BooleanField(default=False)
    estimated_wait = models.CharField(max_length=40, blank=True)
    idempotency_key = models.CharField(max_length=100, blank=True, editable=False)
    request_hash = models.CharField(max_length=64, blank=True, editable=False)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="handled_orders",
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    # Till orders (channel "pos"); app orders leave these empty.
    placed_at = models.DateTimeField(null=True, blank=True)
    tables = models.JSONField(default=list, blank=True)
    table_cleared = models.BooleanField(default=False)
    discount_cents = models.PositiveIntegerField(default=0)
    discounts = models.JSONField(default=list, blank=True)
    note = models.TextField(blank=True)
    staff_name = models.CharField(max_length=120, blank=True)
    terminal_id = models.CharField(max_length=60, blank=True)
    history = models.JSONField(default=list, blank=True)
    review_note = models.TextField(
        blank=True,
        help_text="Where the till's prices differ from the menu, for a manager to check.",
    )

    class Meta(ManagedModel.Meta):
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["location", "status", "created_at"]),
            models.Index(fields=["customer", "created_at"]),
            models.Index(fields=["location", "updated_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="unique_order_idempotency_key",
            )
        ]


class OrderLine(BaseModel):
    def __str__(self):
        return f"{self.qty} × {self.name}"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    menu_item = models.ForeignKey(
        "menu.MenuItem", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    item_slug = models.SlugField()
    name = models.CharField(max_length=160)
    qty = models.PositiveSmallIntegerField()
    unit_price_cents = models.PositiveIntegerField(help_text="Item plus selected options.")
    selections = models.JSONField(default=dict, blank=True)
    modifiers_label = models.TextField(blank=True)
    note = models.CharField(max_length=500, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    # Till lines: the till's line key, kitchen adjustments ("No dairy") and kitchen progress.
    # A void lowers qty (0 = voided); lines are never deleted.
    key = models.CharField(max_length=40, blank=True)
    adjust = models.JSONField(default=list, blank=True)
    prep = models.CharField(
        max_length=10,
        blank=True,
        choices=[("new", "Not started"), ("preparing", "Preparing"), ("ready", "Ready")],
    )

    class Meta(BaseModel.Meta):
        ordering = ["position"]


class OrderPayment(BaseModel):
    """One tender taken at a till (cash, card, wallet, gift card, reward). Idempotent on reference."""

    def __str__(self):
        return f"{self.get_method_display()} ${self.amount_cents / 100:.2f}"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    shift = models.ForeignKey(
        "pos.CashShift",
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
        blank=True,
        help_text="Cash shift associated with this payment.",
    )
    method = models.CharField(
        max_length=6,
        choices=[
            ("cash", "Cash"),
            ("card", "Card"),
            ("wallet", "Wallet"),
            ("gift", "Gift card"),
            ("reward", "Reward"),
        ],
    )
    amount_cents = models.PositiveIntegerField(help_text="Applied to the order (no tip/surcharge).")
    tip_cents = models.PositiveIntegerField(default=0)
    surcharge_cents = models.PositiveIntegerField(default=0)
    reference = models.CharField(max_length=80)
    gift_last4 = models.CharField(max_length=4, blank=True)
    gift_card = models.ForeignKey(
        "loyalty.GiftCard",
        on_delete=models.PROTECT,
        related_name="till_payments",
        null=True,
        blank=True,
        help_text="The gift card this payment was taken from (refunds go back to it).",
    )
    split_mode = models.CharField(max_length=10, blank=True)
    items = models.JSONField(default=dict, blank=True)
    cash_paid_cents = models.PositiveIntegerField(null=True, blank=True)
    rounding_cents = models.IntegerField(default=0)
    change_cents = models.PositiveIntegerField(null=True, blank=True)
    staff_name = models.CharField(max_length=120, blank=True)
    taken_at = models.CharField(max_length=40, blank=True, help_text="As shown on the till.")
    terminal_id = models.CharField(max_length=60, blank=True)

    class Meta(BaseModel.Meta):
        verbose_name = "payment"
        constraints = [
            models.UniqueConstraint(fields=["order", "reference"], name="unique_payment_reference")
        ]


class OrderAdjustment(BaseModel):
    """A refund, void or cancellation made at a till. Idempotent on key."""

    def __str__(self):
        return f"{self.get_mode_display()} ${self.amount_cents / 100:.2f}"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="adjustments")
    shift = models.ForeignKey(
        "pos.CashShift",
        on_delete=models.PROTECT,
        related_name="adjustments",
        null=True,
        blank=True,
        help_text="Cash shift associated with this adjustment.",
    )
    key = models.CharField(max_length=60)
    mode = models.CharField(
        max_length=6, choices=[("cancel", "Cancel"), ("void", "Void"), ("refund", "Refund")]
    )
    amount_cents = models.PositiveIntegerField(help_text="Returned to the customer.")
    allocation = models.JSONField(default=list, blank=True)
    items = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    staff_name = models.CharField(max_length=120, blank=True)
    approver_name = models.CharField(max_length=120, blank=True)
    made_at = models.CharField(max_length=40, blank=True, help_text="As shown on the till.")
    terminal_id = models.CharField(max_length=60, blank=True)
    pos_loyalty_processed_at = models.DateTimeField(null=True, blank=True)

    class Meta(BaseModel.Meta):
        verbose_name = "refund or void"
        verbose_name_plural = "refunds and voids"
        constraints = [
            models.UniqueConstraint(fields=["order", "key"], name="unique_adjustment_key")
        ]

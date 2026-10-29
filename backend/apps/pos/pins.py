"""
Till PINs and what each person may do at a till.

A PIN is 4 digits, unique across the chain's team (so a till never has to ask "who are you?"
first), stored as an HMAC keyed by SECRET_KEY and the user id. Rotating SECRET_KEY clears
every PIN in effect; set them again in the BOS workspace.
"""

import hashlib
import hmac
import re

from django.conf import settings
from django.db.models import Q

from apps.accounts.models import User
from apps.core.permissions import GLOBAL_ROLES

PIN_PATTERN = re.compile(r"^\d{4}$")
TEAM_ROLES = ("manager", "staff")

# The till's default role matrix (POS src/domain/permissions.ts DEFAULT_ROLES).
ROLES = {
    "cashier": {
        "label": "Cashier",
        "perms": [
            "pos",
            "orders",
            "reservations.view",
            "customers.view",
            "loyalty.stamp",
            "gift.redeem",
            "kitchen",
        ],
    },
    "manager": {
        "label": "Manager",
        "perms": [
            "pos",
            "orders",
            "orders.refund",
            "cash.out",
            "reservations",
            "catering",
            "customers",
            "loyalty",
            "gift",
            "inventory",
            "reports",
            "staff.view",
            "settings.view",
            "kitchen",
            "devices",
        ],
    },
    "admin": {"label": "Admin", "perms": ["*"]},
}


def till_role(user):
    """BOS role → till role: chain owner/head office → admin, café owner → manager, staff → cashier."""
    if user.is_superuser or user.role in GLOBAL_ROLES:
        return "admin"
    return "manager" if user.role == "manager" else "cashier"


def permissions(user):
    perms = list(ROLES[till_role(user)]["perms"])
    if "loyalty" in perms and "loyalty.stamp" not in perms:
        perms.append("loyalty.stamp")
    if "gift" in perms and "gift.redeem" not in perms:
        perms.append("gift.redeem")
    return perms


def pin_hash(user, pin):
    key = settings.SECRET_KEY.encode()
    return hmac.new(key, f"pos-pin:{user.pk}:{pin}".encode(), hashlib.sha256).hexdigest()


def team():
    """Everyone who may hold a till PIN: active chain-wide accounts and café teams."""
    return User.objects.filter(is_active=True).filter(
        Q(is_superuser=True)
        | Q(role__in=GLOBAL_ROLES)
        | Q(role__in=TEAM_ROLES, assignments__is_active=True)
    )


def pin_taken(pin, exclude=None):
    holders = team().exclude(pos_pin="").distinct()
    if exclude is not None:
        holders = holders.exclude(pk=exclude.pk)
    return any(hmac.compare_digest(user.pos_pin, pin_hash(user, pin)) for user in holders)


def set_pin(user, pin):
    """Sets (or with "" removes) a PIN. Raises ValueError with a message for the form."""
    if pin and not PIN_PATTERN.match(pin):
        raise ValueError("A till PIN is exactly 4 digits.")
    if pin and pin_taken(pin, exclude=user):
        raise ValueError("Someone else already uses this PIN. Choose another.")
    user.pos_pin = pin_hash(user, pin) if pin else ""


def find_by_pin(pin, location_ids):
    """
    The team member with this PIN who may work at one of these cafés (None = every café).
    Chain-wide accounts may sign in at any café.
    """
    if not PIN_PATTERN.match(pin or ""):
        return None
    holders = team().exclude(pos_pin="")
    if location_ids is not None:
        holders = holders.filter(
            Q(is_superuser=True)
            | Q(role__in=GLOBAL_ROLES)
            | Q(assignments__is_active=True, assignments__location_id__in=location_ids)
        )
    for user in holders.distinct():
        if hmac.compare_digest(user.pos_pin, pin_hash(user, pin)):
            return user
    return None

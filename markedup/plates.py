"""Plate normalisation and the vehicle type list (the single source of truth)."""

import re

# (id, label). The browser reads this list from GET /api/types.
VEHICLE_TYPES = (
    ("pickup", "Pickup"),
    ("car", "Car"),
    ("suv", "SUV"),
    ("van", "Van"),
    ("motorcycle", "Motorcycle"),
    ("tricycle", "Tricycle"),
    ("jeepney", "Jeepney"),
    ("bus", "Bus"),
    ("truck", "Truck"),
    ("ebike", "E-bike"),
    ("other", "Other"),
)
TYPE_LABELS = dict(VEHICLE_TYPES)

_PLATE = re.compile(r"^[A-Z0-9]{2,10}$")
_LETTERS_DIGITS = re.compile(r"^([A-Z]+)(\d+)$")
_DIGITS_LETTERS = re.compile(r"^(\d+)([A-Z]+)$")


def normalize_plate(raw):
    """'abc-1234', 'ABC 1234' and 'abc1234' all become 'ABC1234'. None when unusable."""
    if not isinstance(raw, str):
        return None
    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return compact if _PLATE.match(compact) else None


def display_plate(plate):
    """'ABC1234' -> 'ABC 1234', '1234ABC' -> '1234 ABC', anything else unchanged."""
    match = _LETTERS_DIGITS.match(plate) or _DIGITS_LETTERS.match(plate)
    return "%s %s" % (match.group(1), match.group(2)) if match else plate

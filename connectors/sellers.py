# SELLER_NUMBERS=+50764000001,+50764000002
# SELLER_NAMES=50764000001:Luis,50764000002:Gladys
#
# Both vars go in Railway environment variables.
# SELLER_NUMBERS controls the whitelist — these numbers never hit the customer flow.
# SELLER_NAMES maps numbers to friendly names for owner commands and notifications.

import os


def _normalize_number(number: str) -> str:
    """Normalize numbers for robust comparisons across +/whatsapp:/spaces formats."""
    if not number:
        return ""
    n = number.strip()
    n = n.replace("whatsapp:", "").replace("+", "")
    n = n.replace(" ", "").replace("-", "")
    return n


def get_seller_whitelist() -> set:
    raw = os.getenv("SELLER_NUMBERS", "")
    for sep in ("\n", ";"):
        raw = raw.replace(sep, ",")
    numbers = {n.strip() for n in raw.split(",") if n.strip()}
    return {_normalize_number(n) for n in numbers if _normalize_number(n)}


def is_seller(number: str) -> bool:
    return _normalize_number(number) in get_seller_whitelist()


def get_seller_name(number: str) -> str:
    """Return the friendly name for a seller number, or the number itself if not found."""
    normalized = _normalize_number(number)
    raw = os.getenv("SELLER_NAMES", "")
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        num, name = entry.split(":", 1)
        if _normalize_number(num) == normalized:
            return name.strip()
    return number


def get_seller_number_by_name(name: str) -> str | None:
    """Look up a seller's WhatsApp number by friendly name (case-insensitive).
    Returns '+digits' format or None if not found.
    """
    raw = os.getenv("SELLER_NAMES", "")
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        number, seller_name = entry.split(":", 1)
        if seller_name.strip().lower() == name.strip().lower():
            return "+" + _normalize_number(number)
    return None

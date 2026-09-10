import re
import unicodedata

_DIGITS = re.compile(r"\D")


def normalize_phone(raw: str | None) -> str | None:
    """US phone -> 1xxxxxxxxxx."""
    digits = _DIGITS.sub("", raw or "")
    if len(digits) == 10:
        return "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return digits
    return None

def normalize_phone_with_plus(raw: str) -> str | None:
    digits = _DIGITS.sub("", raw or "")
    if len(digits) == 10:
            return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None
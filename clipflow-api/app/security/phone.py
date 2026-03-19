import re


def normalize_phone_number(phone_number: str, country_code: str | None = None) -> str:
    raw = (phone_number or "").strip()
    country = (country_code or "").strip().upper()

    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        normalized = f"+{digits}"
    elif country == "BR":
        normalized = f"+55{digits}"
    elif country and digits:
        normalized = f"+{digits}"
    else:
        normalized = f"+{digits}"

    if len(normalized) < 8 or len(normalized) > 16 or not normalized.startswith("+"):
        raise ValueError("Invalid phone number")

    return normalized

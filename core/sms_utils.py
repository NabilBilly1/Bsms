import re

# Official Ghana 10-digit prefixes (without the leading '0'):
# Mobile:
# - MTN: 24, 25, 53, 54, 55, 59
# - Telecel (Vodafone): 20, 50
# - AT (AirtelTigo): 26, 27, 56, 57
# - Glo: 23
# - Expresso: 28
# Landline / Fixed (NCA): 30 - 39
GHANA_OFFICIAL_LOCAL_REGEX = re.compile(r"^0(2[0345678]|5[0345679]|3[0-9])\d{7}$")
GHANA_OFFICIAL_E164_REGEX = re.compile(r"^\+233(2[0345678]|5[0345679]|3[0-9])\d{7}$")
FOREIGN_E164_REGEX = re.compile(r"^\+(?!233)[1-9]\d{7,14}$")


def normalize_phone(phone: str, default_country_code: str = "233") -> str:
    """
    Normalizes a phone number to standard E.164 format.
    - Ghana local numbers MUST be strictly 10 digits starting with '0' and an official Ghana prefix
      (e.g. '0241234567' -> '+233241234567').
    - Ghana numbers with 233/+233 must match the 9 subscriber digits of an official 10-digit Ghana number.
    - Foreign numbers must start with '+' or '00' (e.g. '+2348031234567').
    Returns empty string '' if the input cannot be normalized to a valid format.
    """
    if not phone:
        return ""

    raw = str(phone).strip()
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""

    # 1. Explicit '+' prefix (E.164 input)
    if has_plus:
        candidate = f"+{digits}"
        if candidate.startswith("+233"):
            return candidate if GHANA_OFFICIAL_E164_REGEX.match(candidate) else ""
        return candidate if FOREIGN_E164_REGEX.match(candidate) else ""

    # 2. International '00' dialing prefix (e.g. 00234... or 0044...)
    if raw.startswith("00") and len(digits) > 4:
        candidate = f"+{digits[2:]}"
        if candidate.startswith("+233"):
            return candidate if GHANA_OFFICIAL_E164_REGEX.match(candidate) else ""
        return candidate if FOREIGN_E164_REGEX.match(candidate) else ""

    # 3. Local Ghana number: strictly 10 digits starting with 0 and official Ghana prefix
    if digits.startswith("0"):
        if GHANA_OFFICIAL_LOCAL_REGEX.match(digits):
            return f"+233{digits[1:]}"
        return ""

    # 4. Ghana number with 233 prefix (12 digits total: 233 + 9 official subscriber digits)
    if digits.startswith("233"):
        candidate = f"+{digits}"
        return candidate if GHANA_OFFICIAL_E164_REGEX.match(candidate) else ""

    # Any other unprefixed number is invalid (Ghana numbers must be strictly 10 digits starting with 0)
    return ""


def is_valid_phone(phone: str) -> bool:
    """
    Validates normalized phone number:
    - If Ghana (+233), must strictly match an official 10-digit Ghana number (+233 + 9 official digits).
    - If foreign (+ followed by non-233 country code), must match E.164 (8 to 15 digits).
    """
    if not phone:
        return False
    cleaned = phone.strip()
    if GHANA_OFFICIAL_LOCAL_REGEX.match(cleaned):
        return True
    if cleaned.startswith("+233"):
        return bool(GHANA_OFFICIAL_E164_REGEX.match(cleaned))
    return bool(FOREIGN_E164_REGEX.match(cleaned))


# Backward compatibility aliases
normalize_ghana_phone = normalize_phone
normalize_ghana_number = normalize_phone
validate_ghana_number = is_valid_phone

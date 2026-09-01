import re

def normalize_ghana_phone(phone: str) -> str:
    """
    Normalizes Ghana phone numbers to international format (+233...).
    Handles:
    - 0551234567 -> +233551234567
    - 233551234567 -> +233551234567
    - +233551234567 -> +233551234567
    - 024 123 4567 -> +233241234567
    """
    if not phone:
        return ""
    
    # Remove all non-numeric characters
    clean = re.sub(r"\D", "", phone)
    
    # If it starts with 0 and has 10 digits
    if clean.startswith("0") and len(clean) == 10:
        return f"+233{clean[1:]}"
    
    # If it starts with 233 and has 12 digits
    if clean.startswith("233") and len(clean) == 12:
        return f"+{clean}"
    
    # If it's 9 digits, assume it's missing the leading 0 or 233
    if len(clean) == 9:
        return f"+233{clean}"
        
    # Return as is if already correctly formatted or unknown
    if clean.startswith("233") or clean.startswith("+233"):
         return f"+{clean.lstrip('+')}"
         
    return phone.strip()

def is_valid_phone(phone: str) -> bool:
    # Ghana format: +233 followed by 9 digits
    pattern = r"^\+233\d{9}$"
    return bool(re.match(pattern, phone))

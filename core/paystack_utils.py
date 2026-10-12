import hashlib
import hmac
import requests
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session
from core.config import settings
from models import models as db_models

PAYSTACK_BASE_URL = "https://api.paystack.co"

def get_paystack_credentials(db: Session, company_id: Optional[int] = None) -> Tuple[Optional[str], Optional[str], str]:
    """
    Resolves Paystack credentials with priority:
    1. Specific Company configuration (if company_id is provided)
    2. SystemSetting in DB (configured via Master Admin UI)
    3. Environment variables (settings.PAYSTACK_SECRET_KEY, etc.)
    Returns (public_key, secret_key, currency).
    """
    public_key = None
    secret_key = None
    currency = settings.PAYSTACK_CURRENCY or "GHS"

    # 1. Company-level override
    if company_id:
        company = db.query(db_models.Company).filter(db_models.Company.id == company_id).first()
        if company and company.paystack_secret_key:
            return company.paystack_public_key, company.paystack_secret_key, currency

    # 2. SystemSetting table (from Master Admin)
    sys_pub = db.query(db_models.SystemSetting).filter(db_models.SystemSetting.key == "paystack_public_key").first()
    sys_sec = db.query(db_models.SystemSetting).filter(db_models.SystemSetting.key == "paystack_secret_key").first()
    sys_cur = db.query(db_models.SystemSetting).filter(db_models.SystemSetting.key == "paystack_currency").first()

    if sys_pub and sys_pub.value:
        public_key = sys_pub.value
    if sys_sec and sys_sec.value:
        secret_key = sys_sec.value
    if sys_cur and sys_cur.value:
        currency = sys_cur.value

    # 3. Fallback to settings / env variables
    if not public_key:
        public_key = settings.PAYSTACK_PUBLIC_KEY
    if not secret_key:
        secret_key = settings.PAYSTACK_SECRET_KEY

    return public_key, secret_key, currency

def mask_key(key: Optional[str]) -> Optional[str]:
    """Masks secret keys for safe display in UI e.g., sk_live_***1234"""
    if not key:
        return None
    if len(key) <= 8:
        return "********"
    prefix = key[:7]
    suffix = key[-4:]
    return f"{prefix}***{suffix}"

def initialize_paystack_transaction(
    secret_key: str,
    email: str,
    amount_major: float,
    currency: str,
    reference: str,
    callback_url: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Initializes a transaction with Paystack.
    Paystack expects amount in lowest currency unit (pesewas/cents/kobo), so amount_major is multiplied by 100.
    """
    amount_subunit = int(round(amount_major * 100))
    url = f"{PAYSTACK_BASE_URL}/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "email": email,
        "amount": amount_subunit,
        "currency": currency.upper(),
        "reference": reference,
    }
    if callback_url:
        payload["callback_url"] = callback_url
    if metadata:
        payload["metadata"] = metadata

    response = requests.post(url, json=payload, headers=headers, timeout=15)
    data = response.json()
    if not response.ok or not data.get("status"):
        raise ValueError(data.get("message") or f"Paystack initialization failed (HTTP {response.status_code})")
    
    return data["data"]

def verify_paystack_transaction(secret_key: str, reference: str) -> Dict[str, Any]:
    """
    Queries Paystack to verify a transaction reference.
    """
    url = f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}"
    headers = {
        "Authorization": f"Bearer {secret_key}",
    }
    response = requests.get(url, headers=headers, timeout=15)
    data = response.json()
    if not response.ok or not data.get("status"):
        raise ValueError(data.get("message") or f"Paystack verification failed (HTTP {response.status_code})")

    return data["data"]

def validate_paystack_webhook_signature(secret_key: str, raw_body: bytes, signature_header: str) -> bool:
    """
    Validates Paystack webhook HMAC SHA512 signature.
    """
    computed_hash = hmac.new(
        secret_key.encode("utf-8"),
        raw_body,
        hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(computed_hash, signature_header)

import json
import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Header, status
from sqlalchemy.orm import Session

from core.database import get_db
from models import models as db_models
from schemas import schemas as api_schemas
from core.paystack_utils import (
    get_paystack_credentials,
    initialize_paystack_transaction,
    verify_paystack_transaction,
    validate_paystack_webhook_signature,
)

router = APIRouter()

@router.get("/config")
def get_public_config(company_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    Returns the public key and currency for client-side Paystack checkout.
    """
    public_key, secret_key, currency = get_paystack_credentials(db, company_id=company_id)
    return {
        "public_key": public_key or "",
        "currency": currency or "GHS",
        "is_configured": bool(public_key and secret_key),
    }

@router.post("/initialize", response_model=api_schemas.PaymentInitializeResponse)
def initialize_payment(
    req: api_schemas.PaymentInitializeRequest,
    db: Session = Depends(get_db),
):
    """
    Initializes a new payment transaction with Paystack and records it in the database.
    """
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")

    public_key, secret_key, default_currency = get_paystack_credentials(db, company_id=req.company_id)
    if not secret_key:
        raise HTTPException(
            status_code=400,
            detail="Paystack is not configured. Please configure your Paystack API keys in the Master Admin panel.",
        )

    currency = (req.currency or default_currency or "GHS").upper()
    reference = f"CH_{uuid.uuid4().hex[:12].upper()}"

    metadata = {
        "company_id": req.company_id,
        "branch_id": req.branch_id,
        "purpose": req.purpose,
        "credits_purchased": req.credits_purchased,
    }

    try:
        paystack_data = initialize_paystack_transaction(
            secret_key=secret_key,
            email=req.customer_email,
            amount_major=req.amount,
            currency=currency,
            reference=reference,
            callback_url=req.callback_url,
            metadata=metadata,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to initialize payment with Paystack: {str(e)}")

    # Store transaction in database with status pending
    amount_subunit = int(round(req.amount * 100))
    new_tx = db_models.PaymentTransaction(
        reference=reference,
        company_id=req.company_id,
        branch_id=req.branch_id,
        amount=amount_subunit,
        currency=currency,
        customer_email=req.customer_email,
        status="pending",
        purpose=req.purpose or "sms_credits",
        credits_purchased=req.credits_purchased,
        paystack_response=json.dumps(paystack_data),
    )
    db.add(new_tx)
    db.commit()

    return api_schemas.PaymentInitializeResponse(
        authorization_url=paystack_data.get("authorization_url", ""),
        access_code=paystack_data.get("access_code", ""),
        reference=reference,
    )

@router.get("/verify/{reference}", response_model=api_schemas.PaymentTransactionSchema)
def verify_payment(
    reference: str,
    db: Session = Depends(get_db),
):
    """
    Verifies a transaction with Paystack and updates the local transaction record.
    """
    tx = db.query(db_models.PaymentTransaction).filter(db_models.PaymentTransaction.reference == reference).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    public_key, secret_key, _ = get_paystack_credentials(db, company_id=tx.company_id)
    if not secret_key:
        raise HTTPException(status_code=400, detail="Paystack secret key is not configured")

    try:
        data = verify_paystack_transaction(secret_key=secret_key, reference=reference)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Paystack verification error: {str(e)}")

    paystack_status = data.get("status")
    if paystack_status == "success":
        tx.status = "success"
        tx.channel = data.get("channel")
        tx.paid_at = datetime.utcnow()
        tx.paystack_response = json.dumps(data)
    elif paystack_status in ("failed", "abandoned"):
        tx.status = paystack_status
        tx.paystack_response = json.dumps(data)

    db.commit()
    db.refresh(tx)

    # Return formatted schema
    company_name = tx.company.name if tx.company else None
    branch_name = tx.branch.name if tx.branch else None

    return api_schemas.PaymentTransactionSchema(
        id=tx.id,
        reference=tx.reference,
        company_id=tx.company_id,
        branch_id=tx.branch_id,
        company_name=company_name,
        branch_name=branch_name,
        amount=round(tx.amount / 100.0, 2),
        currency=tx.currency,
        customer_email=tx.customer_email,
        status=tx.status,
        channel=tx.channel,
        purpose=tx.purpose,
        credits_purchased=tx.credits_purchased,
        paid_at=tx.paid_at,
        created_at=tx.created_at,
    )

@router.post("/webhook")
async def paystack_webhook(
    request: Request,
    x_paystack_signature: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """
    Paystack Webhook Listener for automated asynchronous event handling.
    """
    body_bytes = await request.body()

    # Get platform-level secret key for signature validation
    _, secret_key, _ = get_paystack_credentials(db)
    if not secret_key:
        return {"status": "ignored", "reason": "Paystack not configured"}

    if not x_paystack_signature or not validate_paystack_webhook_signature(secret_key, body_bytes, x_paystack_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = payload.get("event")
    data = payload.get("data", {})

    if event == "charge.success":
        reference = data.get("reference")
        if reference:
            tx = db.query(db_models.PaymentTransaction).filter(db_models.PaymentTransaction.reference == reference).first()
            if tx and tx.status != "success":
                tx.status = "success"
                tx.channel = data.get("channel")
                tx.paid_at = datetime.utcnow()
                tx.paystack_response = json.dumps(data)
                db.commit()

    return {"status": "ok"}

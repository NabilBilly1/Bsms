import re
from typing import List, Optional

from core.database import get_db
from core.logger import get_logger
from fastapi import APIRouter, Depends, HTTPException
from models import models as db_models
from schemas import schemas as api_schemas
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .deps import get_current_branch

logger = get_logger(__name__)

router = APIRouter()


def normalize_ghana_number(phone: str) -> str:
    # Remove all non-numeric characters
    clean = re.sub(r"\D", "", phone)

    if clean.startswith("0") and len(clean) == 10:
        return "+233" + clean[1:]
    if clean.startswith("233") and len(clean) == 12:
        return "+" + clean
    if len(clean) == 9:
        return "+233" + clean

    # If it already looks correct, just add + if missing
    if len(clean) == 12 and not phone.startswith("+"):
        return "+" + clean

    return phone


def validate_ghana_number(phone: str) -> bool:
    pattern = r"^\+233\d{9}$"
    return bool(re.match(pattern, phone))


@router.get("/stats")
def get_branch_stats(
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    try:
        customer_count = (
            db.query(db_models.Customer)
            .filter(db_models.Customer.branch_id == current_branch.id)
            .count()
        )

        # Count SMS logs for this branch
        phones = [
            p[0]
            for p in db.query(db_models.Customer.phone_number)
            .filter(db_models.Customer.branch_id == current_branch.id)
            .all()
        ]

        sms_sent_today = (
            db.query(db_models.SMSLog)
            .filter(
                db_models.SMSLog.phone_number.in_(phones),
                db_models.SMSLog.status == "sent",
                func.date(db_models.SMSLog.created_at) == func.current_date(),
            )
            .count()
        )

        pending_sms = (
            db.query(db_models.SMSLog)
            .filter(
                db_models.SMSLog.phone_number.in_(phones),
                db_models.SMSLog.status == "queued",
            )
            .count()
        )

        return {
            "total_customers": customer_count,
            "sms_sent_today": sms_sent_today,
            "pending_sms": pending_sms,
        }
    except Exception as e:
        logger.error(f"Error fetching stats for branch {current_branch.id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve dashboard metrics."
        )


@router.post("/", response_model=api_schemas.Customer)
def create_customer(
    customer_in: api_schemas.CustomerCreate,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    try:
        # Standardize and Validate Phone Number
        normalized_phone = normalize_ghana_number(customer_in.phone_number)
        if not validate_ghana_number(normalized_phone):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Ghana phone number format. Expected +233XXXXXXXXX, got {normalized_phone}",
            )

        # Check if customer already exists for this branch with normalized phone
        db_customer = (
            db.query(db_models.Customer)
            .filter(
                db_models.Customer.phone_number == normalized_phone,
                db_models.Customer.branch_id == current_branch.id,
            )
            .first()
        )

        if db_customer:
            return db_customer

        new_customer = db_models.Customer(
            full_name=customer_in.full_name,
            email=customer_in.email,
            phone_number=normalized_phone,
            branch_id=current_branch.id,
        )
        db.add(new_customer)
        db.commit()
        db.refresh(new_customer)
        return new_customer
    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating customer in branch {current_branch.id}: {str(e)}")
        raise HTTPException(
            status_code=400, detail=f"Error creating customer: {str(e)}"
        )


@router.get("/", response_model=api_schemas.PaginatedCustomer)
def read_customers(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
    search: Optional[str] = None,
):
    try:
        query = db.query(db_models.Customer).filter(
            db_models.Customer.branch_id == current_branch.id
        )

        if search:
            query = query.filter(
                or_(
                    db_models.Customer.full_name.ilike(f"%{search}%"),
                    db_models.Customer.phone_number.ilike(f"%{search}%"),
                    db_models.Customer.email.ilike(f"%{search}%"),
                )
            )

        total = query.count()
        items = (
            query.order_by(db_models.Customer.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        return {"items": items, "total": total}
    except Exception as e:
        logger.error(
            f"Error fetching customers for branch {current_branch.id}: {str(e)}"
        )
        raise HTTPException(
            status_code=500, detail="Failed to retrieve customer records."
        )


@router.put("/{customer_id}", response_model=api_schemas.Customer)
def update_customer(
    customer_id: int,
    customer_in: api_schemas.CustomerUpdate,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    db_customer = (
        db.query(db_models.Customer)
        .filter(
            db_models.Customer.id == customer_id,
            db_models.Customer.branch_id == current_branch.id,
        )
        .first()
    )

    if not db_customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    try:
        update_data = customer_in.model_dump(exclude_unset=True)

        if "phone_number" in update_data:
            normalized_phone = normalize_ghana_number(update_data["phone_number"])
            if not validate_ghana_number(normalized_phone):
                raise HTTPException(
                    status_code=400, detail=f"Invalid Ghana phone number format."
                )
            update_data["phone_number"] = normalized_phone

        for field, value in update_data.items():
            setattr(db_customer, field, value)

        db.add(db_customer)
        db.commit()
        db.refresh(db_customer)
        return db_customer
    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        logger.error(
            f"Error updating customer {customer_id} in branch {current_branch.id}: {str(e)}"
        )
        raise HTTPException(status_code=400, detail="Failed to update customer.")


@router.delete("/{customer_id}")
def delete_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    db_customer = (
        db.query(db_models.Customer)
        .filter(
            db_models.Customer.id == customer_id,
            db_models.Customer.branch_id == current_branch.id,
        )
        .first()
    )

    if not db_customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    try:
        db.delete(db_customer)
        db.commit()
        return {"message": "Customer deleted successfully"}
    except Exception as e:
        db.rollback()
        logger.error(
            f"Error deleting customer {customer_id} in branch {current_branch.id}: {str(e)}"
        )
        raise HTTPException(status_code=500, detail="Failed to delete customer.")

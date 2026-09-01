from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from core.database import get_db
from models import models as db_models
from schemas import schemas as api_schemas
from .deps import get_current_branch
from worker.tasks import send_email_task

router = APIRouter()

@router.post("/send", response_model=api_schemas.EmailLog)
def send_email(
    email_in: api_schemas.EmailLogCreate,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch)
):
    try:
        # 1. Personalization: Attempt to find customer by email in this branch
        customer = db.query(db_models.Customer).filter(
            db_models.Customer.email == email_in.recipient_email,
            db_models.Customer.branch_id == current_branch.id
        ).first()

        name_to_use = customer.full_name if customer else "Valued Customer"
        branch_name = current_branch.name or "Class House"
        
        import re
        personalized_content = re.sub(r'\{\s*name\s*\}', name_to_use, email_in.message_content, flags=re.IGNORECASE)
        personalized_content = re.sub(r'\{\s*branch\s*\}', branch_name, personalized_content, flags=re.IGNORECASE)
        
        personalized_subject = re.sub(r'\{\s*name\s*\}', name_to_use, email_in.subject, flags=re.IGNORECASE)
        personalized_subject = re.sub(r'\{\s*branch\s*\}', branch_name, personalized_subject, flags=re.IGNORECASE)

        # 2. Create Email Log
        new_log = db_models.EmailLog(
            branch_id=current_branch.id,
            recipient_email=email_in.recipient_email,
            subject=personalized_subject,
            message_content=personalized_content,
            status="queued",
            scheduled_for=email_in.scheduled_for
        )
        db.add(new_log)
        db.commit()
        db.refresh(new_log)

        # 3. Queue background task
        # Normalize scheduled_for to naive UTC for comparison
        scheduled_at = email_in.scheduled_for
        if scheduled_at and scheduled_at.tzinfo:
            scheduled_at = scheduled_at.astimezone(timezone.utc).replace(tzinfo=None)
        
        # Validation: Scheduled time cannot be in the past (allow 1 min buffer)
        now_naive = datetime.utcnow()
        if scheduled_at and scheduled_at < now_naive - timedelta(minutes=1):
            raise HTTPException(
                status_code=400,
                detail="Scheduled time cannot be in the past."
            )
        
        if scheduled_at and scheduled_at > now_naive + timedelta(seconds=30):
            send_email_task.apply_async(
                args=[email_in.recipient_email, personalized_subject, personalized_content, new_log.id],
                eta=scheduled_at
            )
        else:
            send_email_task.delay(
                email_in.recipient_email, personalized_subject, personalized_content, new_log.id
            )

        return new_log
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/send-bulk")
def send_bulk_email(
    bulk_in: api_schemas.BulkEmailCreate,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch)
):
    try:
        query = db.query(db_models.Customer).filter(
            db_models.Customer.branch_id == current_branch.id,
            db_models.Customer.email != None
        )

        now = datetime.utcnow()
        if bulk_in.filter_type == "today":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(db_models.Customer.created_at >= start_date)
        elif bulk_in.filter_type == "week":
            start_date = now - timedelta(days=7)
            query = query.filter(db_models.Customer.created_at >= start_date)
        elif bulk_in.filter_type == "month":
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(db_models.Customer.created_at >= start_date)
        # 'all' doesn't need extra filter

        customers = query.all()
        
        if not customers:
            return {"message": "No customers with email found for the selected filter", "count": 0}

        queued_count = 0
        branch_name = current_branch.name or "Class House"
        
        import re
        for customer in customers:
            # Personalization
            personalized_content = re.sub(r'\{\s*name\s*\}', customer.full_name, bulk_in.message_content, flags=re.IGNORECASE)
            personalized_content = re.sub(r'\{\s*branch\s*\}', branch_name, personalized_content, flags=re.IGNORECASE)
            
            personalized_subject = re.sub(r'\{\s*name\s*\}', customer.full_name, bulk_in.subject, flags=re.IGNORECASE)
            personalized_subject = re.sub(r'\{\s*branch\s*\}', branch_name, personalized_subject, flags=re.IGNORECASE)

            # Normalize scheduled_for to naive UTC immediately
            scheduled_at = bulk_in.scheduled_for or now
            if scheduled_at and scheduled_at.tzinfo:
                scheduled_at = scheduled_at.astimezone(timezone.utc).replace(tzinfo=None)

            # Validation: Scheduled time cannot be in the past (allow 1 min buffer)
            if scheduled_at < now - timedelta(minutes=1):
                raise HTTPException(
                    status_code=400,
                    detail="Scheduled time cannot be in the past."
                )

            # Create Log
            new_log = db_models.EmailLog(
                branch_id=current_branch.id,
                recipient_email=customer.email,
                subject=personalized_subject,
                message_content=personalized_content,
                status="queued",
                scheduled_for=scheduled_at
            )
            db.add(new_log)
            db.flush()

            # Queue task
            now_naive = datetime.utcnow()

            if scheduled_at and scheduled_at > now_naive + timedelta(seconds=30):
                send_email_task.apply_async(
                    args=[customer.email, personalized_subject, personalized_content, new_log.id],
                    eta=scheduled_at
                )
            else:
                send_email_task.delay(
                    customer.email, personalized_subject, personalized_content, new_log.id
                )
            queued_count += 1
        
        db.commit()
        return {"message": f"Successfully queued {queued_count} emails", "count": queued_count}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/", response_model=api_schemas.PaginatedEmailLog)
def read_email_logs(
    skip: int = 0, 
    limit: int = 100, 
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
    search: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
):
    query = db.query(db_models.EmailLog).filter(
        db_models.EmailLog.branch_id == current_branch.id
    )
    
    if search:
        query = query.filter(
            or_(
                db_models.EmailLog.recipient_email.ilike(f"%{search}%"),
                db_models.EmailLog.subject.ilike(f"%{search}%"),
                db_models.EmailLog.message_content.ilike(f"%{search}%")
            )
        )
        
    if status:
        query = query.filter(db_models.EmailLog.status == status)
        
    if start_date:
        query = query.filter(db_models.EmailLog.created_at >= start_date)
        
    if end_date:
        query = query.filter(db_models.EmailLog.created_at <= end_date)
        
    total = query.count()
    items = query.order_by(db_models.EmailLog.created_at.desc()).offset(skip).limit(limit).all()
    
    return {"items": items, "total": total}

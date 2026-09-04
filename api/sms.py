import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional
import io
import csv
import openpyxl

from core.config import settings
from core.database import get_db
from core.logger import get_logger
from core.utils import clean_html
from core.sms_utils import normalize_ghana_phone
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from models import models as db_models
from redis import Redis
from schemas import schemas as api_schemas
from schemas.sms_schemas import SMSCreate
from sqlalchemy import or_
from sqlalchemy.orm import Session
from worker.tasks import send_sms_task

from .deps import get_current_branch

logger = get_logger(__name__)

router = APIRouter()


def is_valid_phone(phone: str) -> bool:
    # Strict Ghana format: +233 followed by 9 digits
    pattern = r"^\+233\d{9}$"
    return bool(re.match(pattern, phone.strip()))


@router.post("/send", response_model=api_schemas.SMSLog)
def send_custom_sms(
    sms_in: SMSCreate,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    try:
        # 1. Phone Format Validation
        # Get customer first
        customer = (
            db.query(db_models.Customer)
            .filter(
                db_models.Customer.id == sms_in.customer_id,
                db_models.Customer.branch_id == current_branch.id,
            )
            .first()
        )

        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        if not is_valid_phone(customer.phone_number):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid phone format: {customer.phone_number}. Please use international format (e.g. 233...)",
            )

        # 2. Worker Connectivity Check (Redis)
        try:
            redis_client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=4, socket_timeout=4)
            redis_client.ping()
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="SMS Queue Service (Redis) is currently down. Please contact support.",
            )

        # Normalize scheduled_for to naive UTC immediately
        scheduled_at = sms_in.scheduled_for
        if scheduled_at and scheduled_at.tzinfo:
            scheduled_at = scheduled_at.astimezone(timezone.utc).replace(tzinfo=None)

        # Validation: Scheduled time cannot be in the past (allow 1 min buffer)
        now_naive = datetime.utcnow()
        if scheduled_at and scheduled_at < now_naive - timedelta(minutes=1):
            raise HTTPException(
                status_code=400,
                detail="Scheduled time cannot be in the past."
            )

        # 3. Create SMS Log
        name_to_use = customer.full_name if customer else "Valued Customer"
        branch_name = current_branch.name or "Class House"
        personalized_content = re.sub(r'\{\s*name\s*\}', name_to_use, sms_in.message_content, flags=re.IGNORECASE)
        personalized_content = re.sub(r'\{\s*branch\s*\}', branch_name, personalized_content, flags=re.IGNORECASE)

        cleaned_content = clean_html(personalized_content)
        new_log = db_models.SMSLog(
            branch_id=current_branch.id,
            phone_number=customer.phone_number,
            message_type=sms_in.message_type,
            message_content=cleaned_content,
            status="queued",
            scheduled_for=scheduled_at,
        )
        db.add(new_log)
        db.commit()
        db.refresh(new_log)

        # Queue background task
        # If scheduled_for is provided and is in the future (> 30s), use eta
        # Otherwise, process immediately
        if scheduled_at and scheduled_at > now_naive + timedelta(seconds=30):
            send_sms_task.apply_async(
                args=[customer.phone_number, cleaned_content, new_log.id],
                eta=scheduled_at,
            )
        else:
            send_sms_task.delay(customer.phone_number, cleaned_content, new_log.id)

        return new_log
    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        logger.error(
            f"Error scheduling custom SMS for branch {current_branch.id}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=400, detail=f"Failed to schedule SMS: {str(e)}")


from schemas.sms_schemas import BulkSMSCreate


@router.post("/send-bulk")
def send_bulk_sms(
    bulk_in: BulkSMSCreate,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    try:
        query = db.query(db_models.Customer).filter(
            db_models.Customer.branch_id == current_branch.id
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
        elif bulk_in.filter_type == "year":
            start_date = now.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
            query = query.filter(db_models.Customer.created_at >= start_date)
        elif bulk_in.filter_type == "custom_selection":
            if not bulk_in.customer_ids or len(bulk_in.customer_ids) < 2:
                raise HTTPException(status_code=400, detail="Please select at least two customers for custom selection.")
            query = query.filter(db_models.Customer.id.in_(bulk_in.customer_ids))
        # 'all' doesn't need extra filter

        customers = query.all()

        if not customers:
            return {"message": "No customers found for the selected filter", "count": 0}

        queued_count = 0
        branch_name = current_branch.name or "Class House"
        tasks_to_queue = []

        for customer in customers:
            # Perform dynamic placeholder replacement for this specific customer
            personalized_content = re.sub(r'\{\s*name\s*\}', customer.full_name, bulk_in.message_content, flags=re.IGNORECASE)
            personalized_content = re.sub(r'\{\s*branch\s*\}', branch_name, personalized_content, flags=re.IGNORECASE)

            # Strip HTML tags from the personalized content
            personalized_content = clean_html(personalized_content)

            # Normalize scheduled_for to naive UTC immediately
            scheduled_at = bulk_in.scheduled_for or now
            if scheduled_at and scheduled_at.tzinfo:
                scheduled_at = scheduled_at.astimezone(timezone.utc).replace(
                    tzinfo=None
                )

            # Validation: Scheduled time cannot be in the past (allow 1 min buffer)
            if scheduled_at < now - timedelta(minutes=1):
                # We use a slight buffer to account for the loop processing time
                raise HTTPException(
                    status_code=400,
                    detail="Scheduled time cannot be in the past."
                )

            # Create SMS Log
            new_log = db_models.SMSLog(
                branch_id=current_branch.id,
                phone_number=customer.phone_number,
                message_type=bulk_in.message_type,
                message_content=personalized_content,
                status="queued",
                scheduled_for=scheduled_at,
            )
            db.add(new_log)
            db.flush()  # Get ID without committing entire loop yet

            tasks_to_queue.append((customer.phone_number, personalized_content, new_log.id, scheduled_at))
            queued_count += 1

        db.commit()

        # Queue background tasks after commit is successful
        now_naive = datetime.utcnow()
        for phone_number, personalized_content, log_id, scheduled_at in tasks_to_queue:
            if scheduled_at and scheduled_at > now_naive + timedelta(seconds=30):
                send_sms_task.apply_async(
                    args=[phone_number, personalized_content, log_id],
                    eta=scheduled_at,
                )
            else:
                send_sms_task.delay(
                    phone_number, personalized_content, log_id
                )
        return {
            "message": f"Successfully queued {queued_count} messages",
            "count": queued_count,
        }

    except Exception as e:
        db.rollback()
        logger.error(
            f"Error sending bulk SMS for branch {current_branch.id}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/", response_model=api_schemas.PaginatedSMSLog)
def read_sms_logs(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
    search: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    try:
        query = db.query(db_models.SMSLog).filter(
            db_models.SMSLog.branch_id == current_branch.id
        )

        if search:
            query = query.filter(
                or_(
                    db_models.SMSLog.phone_number.ilike(f"%{search}%"),
                    db_models.SMSLog.message_content.ilike(f"%{search}%"),
                )
            )

        if status:
            query = query.filter(db_models.SMSLog.status == status)

        if start_date:
            query = query.filter(db_models.SMSLog.created_at >= start_date)

        if end_date:
            query = query.filter(db_models.SMSLog.created_at <= end_date)

        total = query.count()
        items = (
            query.order_by(db_models.SMSLog.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        return {"items": items, "total": total}
    except Exception as e:
        logger.error(
            f"Error fetching SMS logs for branch {current_branch.id}: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=f"Failed to retrieve SMS logs: {str(e)}")


@router.post("/bulk-import")
async def import_and_send_branch_bulk_sms(
    message_content: str = Form(...),
    scheduled_for: Optional[datetime] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    try:
        # 1. Parse File
        contents = await file.read()
        recipients = [] # List of dicts: {"phone": ..., "name": ...}

        filename = file.filename.lower()
        if filename.endswith(".csv"):
            stream = io.StringIO(contents.decode("utf-8"))
            reader = csv.reader(stream)
            rows = list(reader)
            if not rows:
                 raise HTTPException(status_code=400, detail="Empty CSV file")
            
            start_idx = 0
            if "phone" in rows[0][0].lower() or "name" in rows[0][0].lower():
                start_idx = 1
                
            for row in rows[start_idx:]:
                if len(row) >= 1:
                    phone = row[0].strip()
                    name = row[1].strip() if len(row) >= 2 else ""
                    recipients.append({"phone": phone, "name": name})

        elif filename.endswith((".xlsx", ".xls")):
            wb = openpyxl.load_workbook(io.BytesIO(contents))
            sheet = wb.active
            for row in sheet.iter_rows(min_row=1, values_only=True):
                if not row[0]: continue
                if str(row[0]).lower() in ["phone", "recipient", "number"]:
                    continue
                
                recipients.append({
                    "phone": str(row[0]).strip(),
                    "name": str(row[1]).strip() if len(row) >= 2 and row[1] else ""
                })
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format. Please use CSV or Excel.")

        if not recipients:
            raise HTTPException(status_code=400, detail="No valid recipients found in file")

        # 2. Normalize scheduled_for to naive UTC immediately
        scheduled_at = scheduled_for
        if scheduled_at:
            if scheduled_at.tzinfo:
                scheduled_at = scheduled_at.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            scheduled_at = datetime.utcnow()

        # Validation: Scheduled time cannot be in the past (allow 2 min buffer)
        now_naive = datetime.utcnow()
        if scheduled_at < now_naive - timedelta(minutes=2):
            raise HTTPException(
                status_code=400,
                detail="Scheduled time cannot be in the past."
            )

        # 3. Worker Connectivity Check (Redis)
        try:
            redis_client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=4, socket_timeout=4)
            redis_client.ping()
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="SMS Queue Service (Redis) is currently down. Please contact support.",
            )

        # 4. Process Recipients
        queued_count = 0
        tasks_to_queue = []
        seen_phones = set()
        for rec in recipients:
            norm_phone = normalize_ghana_phone(rec["phone"])
            if not is_valid_phone(norm_phone):
                continue # Skip invalid numbers
            
            if norm_phone in seen_phones:
                continue # Skip duplicates
            seen_phones.add(norm_phone)
            
            # Placeholder replacement
            final_content = re.sub(r'\{\s*name\s*\}', rec["name"] or "Customer", message_content, flags=re.IGNORECASE)
            final_content = re.sub(r'\{\s*branch\s*\}', current_branch.name or "Class House", final_content, flags=re.IGNORECASE)
            
            # Strip HTML tags
            final_content = clean_html(final_content)
            
            # Create Log
            new_log = db_models.SMSLog(
                branch_id=current_branch.id,
                phone_number=norm_phone,
                message_type="bulk",
                message_content=final_content,
                status="queued",
                is_bulk=True,
                scheduled_for=scheduled_at
            )
            db.add(new_log)
            db.flush() # Get ID

            tasks_to_queue.append((norm_phone, final_content, new_log.id))
            queued_count += 1
        
        db.commit()

        # Queue tasks after commit is successful
        for norm_phone, final_content, log_id in tasks_to_queue:
            if scheduled_at and scheduled_at > now_naive + timedelta(seconds=30):
                 send_sms_task.apply_async(args=[norm_phone, final_content, log_id], eta=scheduled_at)
            else:
                 send_sms_task.delay(norm_phone, final_content, log_id)

        return {"status": "success", "queued_count": queued_count}

    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        logger.error(
            f"Failed to process branch bulk import for branch {current_branch.id}: {str(e)}",
            exc_info=True
        )
        raise HTTPException(status_code=500, detail=f"Failed to process bulk import: {str(e)}")

@router.delete("/{log_id}")
def delete_sms_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    try:
        log = db.query(db_models.SMSLog).filter(
            db_models.SMSLog.id == log_id,
            db_models.SMSLog.branch_id == current_branch.id
        ).first()

        if not log:
            raise HTTPException(status_code=404, detail="SMS log not found")

        db.delete(log)
        db.commit()
        return {"message": "SMS log deleted successfully"}
    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting SMS log {log_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to delete SMS log")

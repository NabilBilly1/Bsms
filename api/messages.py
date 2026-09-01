from typing import List

from core.database import get_db
from fastapi import APIRouter, Depends, HTTPException
from models import models as db_models
from schemas import schemas as api_schemas
from sqlalchemy import func
from sqlalchemy.orm import Session

from .deps import get_current_branch

router = APIRouter()


@router.post("/", response_model=api_schemas.Message)
def create_template(
    msg_in: api_schemas.MessageCreate,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    try:
        new_msg = db_models.Message(
            template_type=msg_in.template_type,
            content=msg_in.content,
            branch_id=current_branch.id,
        )
        db.add(new_msg)
        db.commit()
        db.refresh(new_msg)
        return new_msg
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=400, detail=f"Failed to create template: {str(e)}"
        )


@router.get("/", response_model=List[api_schemas.Message])
def read_templates(
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    # Fetch branch-specific templates AND global templates (where branch_id is null)
    templates = (
        db.query(db_models.Message)
        .filter(
            (db_models.Message.branch_id == current_branch.id)
            | (db_models.Message.branch_id == None)
        )
        .all()
    )
    return templates


@router.delete("/{message_id}")
def delete_template(
    message_id: int,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    template = (
        db.query(db_models.Message)
        .filter(
            db_models.Message.id == message_id,
            db_models.Message.branch_id == current_branch.id,
        )
        .first()
    )

    if not template:
        raise HTTPException(
            status_code=404, detail="Template not found or not owned by this branch"
        )

    db.delete(template)
    db.commit()
    return {"message": "Template deleted successfully"}


@router.get("/pending")
def read_pending_messages(
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    try:
        # 1. Fetch Queued SMS
        queued_sms = (
            db.query(db_models.SMSLog)
            .filter(
                db_models.SMSLog.branch_id == current_branch.id,
                db_models.SMSLog.status == "queued",
            )
            .all()
        )

        # 2. Fetch Queued Emails
        queued_emails = (
            db.query(db_models.EmailLog)
            .filter(
                db_models.EmailLog.branch_id == current_branch.id,
                db_models.EmailLog.status == "queued",
            )
            .all()
        )

        pending_items = []

        # Grouping Heuristic: content + scheduled_for
        # SMS Grouping
        sms_groups = {}
        for log in queued_sms:
            key = (log.message_content, log.scheduled_for)
            if key not in sms_groups:
                sms_groups[key] = []
            sms_groups[key].append(log)

        for (content, scheduled_for), logs in sms_groups.items():
            is_bulk = len(logs) > 1
            pending_items.append(
                {
                    "type": "sms",
                    "recipient": f"{len(logs)} recipients"
                    if is_bulk
                    else logs[0].phone_number,
                    "content": content,
                    "scheduled_for": scheduled_for,
                    "is_bulk": is_bulk,
                    "count": len(logs),
                    "ids": [log.id for log in logs],
                    "created_at": min(log.created_at for log in logs),
                }
            )

        # Email Grouping
        email_groups = {}
        for log in queued_emails:
            key = (log.subject, log.message_content, log.scheduled_for)
            if key not in email_groups:
                email_groups[key] = []
            email_groups[key].append(log)

        for (subject, content, scheduled_for), logs in email_groups.items():
            is_bulk = len(logs) > 1
            pending_items.append(
                {
                    "type": "email",
                    "recipient": f"{len(logs)} recipients"
                    if is_bulk
                    else logs[0].recipient_email,
                    "content": subject,  # Use subject as main display content for emails
                    "message_content": content,
                    "scheduled_for": scheduled_for,
                    "is_bulk": is_bulk,
                    "count": len(logs),
                    "ids": [log.id for log in logs],
                    "created_at": min(log.created_at for log in logs),
                }
            )

        # Sort by creation time (latest first)
        pending_items.sort(key=lambda x: x["created_at"], reverse=True)

        return pending_items
    except Exception as e:
        print(f"Error fetching pending messages: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reschedule")
def reschedule_messages(
    payload: api_schemas.BulkReschedule,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    """
    Step 3: API endpoint to bulk reschedule multiple queued messages.
    """
    try:
        from datetime import datetime

        from worker.tasks import send_email_task, send_sms_task

        updated_count = 0
        now = datetime.utcnow()

        # Validation: New schedule cannot be in the past (allow 1 min buffer)
        scheduled_at = payload.new_schedule
        if scheduled_at and scheduled_at.tzinfo:
            scheduled_at = scheduled_at.astimezone(timezone.utc).replace(tzinfo=None)
            
        if scheduled_at < now - timedelta(minutes=1):
            raise HTTPException(
                status_code=400,
                detail="New schedule time cannot be in the past."
            )

        # 1. Reschedule SMS
        if payload.sms_ids:
            sms_logs = (
                db.query(db_models.SMSLog)
                .filter(
                    db_models.SMSLog.id.in_(payload.sms_ids),
                    db_models.SMSLog.branch_id == current_branch.id,
                    db_models.SMSLog.status == "queued",
                )
                .all()
            )

            for log in sms_logs:
                log.scheduled_for = payload.new_schedule
                db.add(log)
                # Queue the task again with the new ETA
                send_sms_task.apply_async(
                    args=[log.phone_number, log.message_content, log.id],
                    eta=payload.new_schedule,
                )
                updated_count += 1

        # 2. Reschedule Emails
        if payload.email_ids:
            email_logs = (
                db.query(db_models.EmailLog)
                .filter(
                    db_models.EmailLog.id.in_(payload.email_ids),
                    db_models.EmailLog.branch_id == current_branch.id,
                    db_models.EmailLog.status == "queued",
                )
                .all()
            )

            for log in email_logs:
                log.scheduled_for = payload.new_schedule
                db.add(log)
                # Queue the task again with the new ETA
                send_email_task.apply_async(
                    args=[
                        log.recipient_email,
                        log.subject,
                        log.message_content,
                        log.id,
                    ],
                    eta=payload.new_schedule,
                )
                updated_count += 1

        db.commit()
        return {
            "message": f"Successfully rescheduled {updated_count} messages",
            "count": updated_count,
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to reschedule: {str(e)}")

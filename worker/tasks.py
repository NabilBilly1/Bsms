from celery import Celery
from celery.exceptions import Retry
from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)

celery_app = Celery("worker", broker=settings.REDIS_URL)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "recover-missed-messages-every-5-mins": {
            "task": "recover_missed_messages_task",
            "schedule": 300.0,  # 5 minutes
        },
    },
)

from datetime import datetime, timedelta

from core.database import SessionLocal
from models import models as db_models
from services.sms_service import sms_service


@celery_app.task(
    name="send_sms_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 1 minute for first retry
)
def send_sms_task(self, phone_number: str, message: str, log_id: int):
    """
    Background task to send SMS via Hubtel gateway with exponential backoff.
    """
    logger.info(
        f"Received SMS task for ID: {log_id} (Attempt {self.request.retries + 1})"
    )

    # 1. Trigger actual sending
    db = SessionLocal()
    try:
        # Fetch company credentials via branch
        log_entry = db.query(db_models.SMSLog).filter(db_models.SMSLog.id == log_id).first()
        if not log_entry or not log_entry.branch_id:
            return {"status": "error", "error": "Branch not associated with log"}
            
        branch = db.query(db_models.Branch).filter(db_models.Branch.id == log_entry.branch_id).first()
        if not branch or not branch.company_id:
             return {"status": "error", "error": "Company not associated with branch"}
             
        company = db.query(db_models.Company).filter(db_models.Company.id == branch.company_id).first()
        
        result = sms_service.send_sms(
            phone_number, 
            message, 
            client_id=company.hubtel_client_id, 
            secret=company.hubtel_client_secret,
            sender_id=company.hubtel_sender_id
        )

        # If technical error, trigger a retry with exponential backoff
        if result.get("status") == "error":
            if log_entry:
                log_entry.status = "retrying"
                log_entry.delivery_report = f"Technical issue. Attempt {self.request.retries + 1} failed. Retrying..."
                db.commit()

            # Retry delays: 1m, 5m, 15m
            retry_delays = [60, 300, 900]
            delay = retry_delays[min(self.request.retries, len(retry_delays) - 1)]
            logger.warning(f"Technical error for SMS {log_id}. Retrying in {delay}s...")
            raise self.retry(exc=Exception(result.get("error")), countdown=delay)

    except Retry:
        raise
    except Exception as e:
        logger.error(f"Critical error in Hubtel SMS service for log {log_id}: {str(e)}")
        result = {"status": "error", "error": str(e)}

    # 2. Update status in database
    try:
        if log_entry:
            if result["status"] in ["success", "mock_success"]:
                log_entry.status = "sent"
                log_entry.sent_at = datetime.utcnow()
                log_entry.delivery_report = "Message sent successfully"
            else:
                log_entry.status = "failed"
                # Extract descriptive error message
                error_msg = (
                    result.get("error") or result.get("message") or "Unknown error"
                )
                log_entry.delivery_report = f"Error: {error_msg}"

            db.commit()
            logger.info(f"Finished SMS task {log_id} with status: {log_entry.status}")
        else:
            logger.warning(f"SMS Log entry {log_id} not found in database.")
    except Exception as e:
        logger.error(f"Error updating SMS log status for ID {log_id}: {str(e)}")
    finally:
        db.close()

    return result


@celery_app.task(name="recover_missed_messages_task")
def recover_missed_messages_task():
    """
    Step 1: Periodic task that finds 'queued' messages whose scheduled time
    has passed but they are still in the database.
    """
    logger.info("Starting recovery check for missed messages...")
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        # Find SMS logs stuck in queued that should have been sent by now
        missed_sms = (
            db.query(db_models.SMSLog)
            .filter(
                db_models.SMSLog.status == "queued",
                db_models.SMSLog.scheduled_for <= now - timedelta(minutes=1),
            )
            .all()
        )

        for sms in missed_sms:
            logger.info(f"Recovering missed SMS ID: {sms.id}")
            send_sms_task.delay(sms.phone_number, sms.message_content, sms.id)

        # Find Email logs stuck in queued
        missed_emails = (
            db.query(db_models.EmailLog)
            .filter(
                db_models.EmailLog.status == "queued",
                db_models.EmailLog.scheduled_for <= now - timedelta(minutes=1),
            )
            .all()
        )

        for email in missed_emails:
            logger.info(f"Recovering missed Email ID: {email.id}")
            send_email_task.delay(
                email.recipient_email, email.subject, email.message_content, email.id
            )

        return {
            "recovered_sms": len(missed_sms),
            "recovered_emails": len(missed_emails),
        }
    except Exception as e:
        logger.error(f"Error during missed message recovery: {str(e)}")
    finally:
        db.close()


from services.email_service import email_service


@celery_app.task(
    name="send_email_task", bind=True, max_retries=3, default_retry_delay=60
)
def send_email_task(self, to_email: str, subject: str, message: str, log_id: int):
    """
    Background task to send Email via Brevo with exponential backoff.
    """
    logger.info(
        f"Received EMAIL task for ID: {log_id} (Attempt {self.request.retries + 1})"
    )

    # 1. Trigger actual sending
    db = SessionLocal()
    try:
        # Fetch company credentials via branch
        log_entry = db.query(db_models.EmailLog).filter(db_models.EmailLog.id == log_id).first()
        if not log_entry or not log_entry.branch_id:
            return {"status": "error", "error": "Branch not associated with log"}
            
        branch = db.query(db_models.Branch).filter(db_models.Branch.id == log_entry.branch_id).first()
        if not branch or not branch.company_id:
             return {"status": "error", "error": "Company not associated with branch"}
             
        company = db.query(db_models.Company).filter(db_models.Company.id == branch.company_id).first()

        result = email_service.send_transactional_email(
            to_email, 
            subject, 
            message,
            api_key=company.brevo_api_key,
            sender_email=company.brevo_sender_email,
            sender_name=company.brevo_sender_name
        )

        # Retry if technical error
        if result.get("status") == "error":
            if log_entry:
                log_entry.status = "retrying"
                log_entry.delivery_report = f"Technical issue. Attempt {self.request.retries + 1} failed. Retrying..."
                db.commit()

            retry_delays = [60, 300, 900]
            delay = retry_delays[min(self.request.retries, len(retry_delays) - 1)]
            logger.warning(
                f"Technical error for Email {log_id}. Retrying in {delay}s..."
            )
            raise self.retry(exc=Exception(result.get("error")), countdown=delay)

    except Retry:
        raise
    except Exception as e:
        logger.error(
            f"Critical error in Brevo Email service for log {log_id}: {str(e)}"
        )
        result = {"status": "error", "error": str(e)}

    # 2. Update status in database
    try:
        if log_entry:
            if result["status"] in ["success", "mock_success"]:
                log_entry.status = "sent"
                log_entry.sent_at = datetime.utcnow()
                log_entry.delivery_report = "Email sent successfully"
            else:
                log_entry.status = "failed"
                error_msg = result.get("error") or "Unknown error"
                log_entry.delivery_report = f"Error: {error_msg}"

            db.commit()
            logger.info(f"Finished EMAIL task {log_id} with status: {log_entry.status}")
        else:
            logger.warning(f"Email Log entry {log_id} not found in database.")
    except Exception as e:
        logger.error(f"Error updating Email log status for ID {log_id}: {str(e)}")
    finally:
        db.close()

    return result

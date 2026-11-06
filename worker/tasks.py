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
    broker_transport_options={
        "visibility_timeout": 86400,  # 24 hours: prevents Redis from prematurely redelivering unacked scheduled tasks
    },
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
    Background task to send SMS via Hubtel gateway with idempotency and exponential backoff.
    """
    logger.info(
        f"Received SMS task for ID: {log_id} (Attempt {self.request.retries + 1})"
    )

    db = SessionLocal()
    try:
        # 1. Atomic lock and idempotency check
        log_entry = (
            db.query(db_models.SMSLog)
            .filter(db_models.SMSLog.id == log_id)
            .with_for_update()
            .first()
        )

        if not log_entry:
            logger.warning(f"SMS Log entry {log_id} not found in database.")
            return {"status": "error", "error": "Log entry not found"}

        if not log_entry.branch_id:
            return {"status": "error", "error": "Branch not associated with log"}

        # Idempotency check: Skip if already sent
        if log_entry.status == "sent":
            logger.info(f"SMS Log {log_id} is already sent. Skipping duplicate execution.")
            return {"status": "skipped", "message": "Already sent"}

        # Check if the message was rescheduled for later (> 30s ahead of now)
        now_utc = datetime.utcnow()
        if log_entry.scheduled_for:
            scheduled_naive = log_entry.scheduled_for
            if scheduled_naive.tzinfo:
                scheduled_naive = scheduled_naive.replace(tzinfo=None)
            if scheduled_naive > now_utc + timedelta(seconds=30):
                logger.info(
                    f"SMS Log {log_id} was rescheduled for future ({scheduled_naive}). Skipping premature task."
                )
                return {"status": "skipped", "message": "Rescheduled for future"}

        # Mark as processing immediately to lock out concurrent tasks
        log_entry.status = "processing"
        db.commit()

        # Re-fetch branch and company credentials
        branch = db.query(db_models.Branch).filter(db_models.Branch.id == log_entry.branch_id).first()
        if not branch or not branch.company_id:
            log_entry.status = "failed"
            log_entry.delivery_report = "Company not associated with branch"
            db.commit()
            return {"status": "error", "error": "Company not associated with branch"}

        company = db.query(db_models.Company).filter(db_models.Company.id == branch.company_id).first()

        client_id = (company.hubtel_client_id if company and company.hubtel_client_id else settings.HUBTEL_CLIENT_ID)
        secret = (company.hubtel_client_secret if company and company.hubtel_client_secret else settings.HUBTEL_CLIENT_SECRET)
        sender_id = (company.hubtel_sender_id if company and company.hubtel_sender_id else settings.HUBTEL_SENDER_ID)

        result = sms_service.send_sms(
            phone_number,
            message,
            client_id=client_id,
            secret=secret,
            sender_id=sender_id
        )

        # If technical error, trigger a retry with exponential backoff
        if result.get("status") == "error":
            log_entry.status = "retrying"
            log_entry.delivery_report = f"Technical issue. Attempt {self.request.retries + 1} failed. Retrying..."
            db.commit()

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
        log_entry = db.query(db_models.SMSLog).filter(db_models.SMSLog.id == log_id).first()
        if log_entry:
            if result.get("status") in ["success", "mock_success"]:
                log_entry.status = "sent"
                log_entry.sent_at = datetime.utcnow()
                log_entry.delivery_report = "Message sent successfully"
            else:
                log_entry.status = "failed"
                error_msg = (
                    result.get("error") or result.get("message") or "Unknown error"
                )
                log_entry.delivery_report = f"Error: {error_msg}"

            db.commit()
            logger.info(f"Finished SMS task {log_id} with status: {log_entry.status}")
    except Exception as e:
        logger.error(f"Error updating SMS log status for ID {log_id}: {str(e)}")
    finally:
        db.close()

    return result


@celery_app.task(name="recover_missed_messages_task")
def recover_missed_messages_task():
    """
    Step 1: Periodic task that finds 'queued' messages whose scheduled time
    has passed by at least 2 minutes and safely dispatches them.
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
                db_models.SMSLog.scheduled_for <= now - timedelta(minutes=2),
            )
            .with_for_update(skip_locked=True)
            .all()
        )

        for sms in missed_sms:
            logger.info(f"Recovering missed SMS ID: {sms.id}")
            sms.status = "recovering"
            db.commit()
            send_sms_task.delay(sms.phone_number, sms.message_content, sms.id)

        # Find Email logs stuck in queued
        missed_emails = (
            db.query(db_models.EmailLog)
            .filter(
                db_models.EmailLog.status == "queued",
                db_models.EmailLog.scheduled_for <= now - timedelta(minutes=2),
            )
            .with_for_update(skip_locked=True)
            .all()
        )

        for email in missed_emails:
            logger.info(f"Recovering missed Email ID: {email.id}")
            email.status = "recovering"
            db.commit()
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
    Background task to send Email via Brevo with idempotency and exponential backoff.
    """
    logger.info(
        f"Received EMAIL task for ID: {log_id} (Attempt {self.request.retries + 1})"
    )

    db = SessionLocal()
    try:
        # Atomic lock and idempotency check
        log_entry = (
            db.query(db_models.EmailLog)
            .filter(db_models.EmailLog.id == log_id)
            .with_for_update()
            .first()
        )

        if not log_entry:
            logger.warning(f"Email Log entry {log_id} not found in database.")
            return {"status": "error", "error": "Log entry not found"}

        if not log_entry.branch_id:
            return {"status": "error", "error": "Branch not associated with log"}

        # Idempotency check: Skip if already sent
        if log_entry.status == "sent":
            logger.info(f"Email Log {log_id} is already sent. Skipping duplicate execution.")
            return {"status": "skipped", "message": "Already sent"}

        # Check if the message was rescheduled for later
        now_utc = datetime.utcnow()
        if log_entry.scheduled_for:
            scheduled_naive = log_entry.scheduled_for
            if scheduled_naive.tzinfo:
                scheduled_naive = scheduled_naive.replace(tzinfo=None)
            if scheduled_naive > now_utc + timedelta(seconds=30):
                logger.info(
                    f"Email Log {log_id} was rescheduled for future ({scheduled_naive}). Skipping premature task."
                )
                return {"status": "skipped", "message": "Rescheduled for future"}

        # Mark as processing
        log_entry.status = "processing"
        db.commit()

        branch = db.query(db_models.Branch).filter(db_models.Branch.id == log_entry.branch_id).first()
        if not branch or not branch.company_id:
            log_entry.status = "failed"
            log_entry.delivery_report = "Company not associated with branch"
            db.commit()
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

    # Update final status in database
    try:
        log_entry = db.query(db_models.EmailLog).filter(db_models.EmailLog.id == log_id).first()
        if log_entry:
            if result.get("status") in ["success", "mock_success"]:
                log_entry.status = "sent"
                log_entry.sent_at = datetime.utcnow()
                log_entry.delivery_report = "Email sent successfully"
            else:
                log_entry.status = "failed"
                error_msg = result.get("error") or "Unknown error"
                log_entry.delivery_report = f"Error: {error_msg}"

            db.commit()
            logger.info(f"Finished EMAIL task {log_id} with status: {log_entry.status}")
    except Exception as e:
        logger.error(f"Error updating Email log status for ID {log_id}: {str(e)}")
    finally:
        db.close()

    return result

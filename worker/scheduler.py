from core.database import SessionLocal
from models.models import SMSLog
from .tasks import celery_app, send_sms_task
from datetime import datetime, timedelta

# Transaction scheduling has been removed as per the new simplified architecture.
# SMS logs are now created via the direct /sms/send endpoint.

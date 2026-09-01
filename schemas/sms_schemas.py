from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class SMSCreate(BaseModel):
    customer_id: int
    message_content: str
    message_type: str
    scheduled_for: datetime

class BulkSMSCreate(BaseModel):
    filter_type: str # 'today', 'week', 'month', 'year', 'all', 'custom_selection'
    message_content: str
    message_type: str
    scheduled_for: Optional[datetime] = None
    customer_ids: Optional[list[int]] = None

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr, field_validator


# Auth Schemas
class BranchLogin(BaseModel):
    company_slug: str
    branch_code: str
    pin: str


class Token(BaseModel):
    access_token: str
    token_type: str
    branch_id: int
    branch_name: str


class TokenData(BaseModel):
    branch_id: Optional[str] = None
    company_id: Optional[int] = None


# Company Schemas
class CompanyBase(BaseModel):
    name: str
    slug: str
    is_active: Optional[bool] = True

class CompanyCreate(CompanyBase):
    hubtel_client_id: Optional[str] = None
    hubtel_client_secret: Optional[str] = None
    hubtel_sender_id: Optional[str] = None
    brevo_api_key: Optional[str] = None
    brevo_sender_email: Optional[EmailStr] = None
    brevo_sender_name: Optional[str] = None
    paystack_public_key: Optional[str] = None
    paystack_secret_key: Optional[str] = None

class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    is_active: Optional[bool] = None
    hubtel_client_id: Optional[str] = None
    hubtel_client_secret: Optional[str] = None
    hubtel_sender_id: Optional[str] = None
    brevo_api_key: Optional[str] = None
    brevo_sender_email: Optional[EmailStr] = None
    brevo_sender_name: Optional[str] = None
    paystack_public_key: Optional[str] = None
    paystack_secret_key: Optional[str] = None

class Company(CompanyBase):
    id: int
    created_at: datetime
    hubtel_client_id: Optional[str] = None
    hubtel_client_secret: Optional[str] = None
    hubtel_sender_id: Optional[str] = None
    brevo_api_key: Optional[str] = None
    brevo_sender_email: Optional[EmailStr] = None
    brevo_sender_name: Optional[str] = None
    paystack_public_key: Optional[str] = None
    paystack_secret_key: Optional[str] = None

    class Config:
        from_attributes = True


# Branch Schemas
class BranchBase(BaseModel):
    name: str
    branch_code: str
    company_id: Optional[int] = None


class BranchCreate(BranchBase):
    pin: str


class Branch(BranchBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Customer Schemas
class CustomerBase(BaseModel):
    phone_number: str
    full_name: str
    email: Optional[EmailStr] = None


class CustomerCreate(CustomerBase):
    pass


class CustomerUpdate(BaseModel):
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[EmailStr] = None


class Customer(CustomerBase):
    id: int
    branch_id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Message (Template) Schemas
class MessageBase(BaseModel):
    template_type: str
    content: str
    branch_id: Optional[int] = None


class MessageCreate(MessageBase):
    pass


class MessageUpdate(BaseModel):
    template_type: Optional[str] = None
    content: Optional[str] = None


class Message(MessageBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# SMS Log Schemas
class SMSLog(BaseModel):
    id: int
    phone_number: str
    message_type: str
    message_content: str
    status: str
    is_bulk: Optional[bool] = False
    delivery_report: Optional[str] = None
    scheduled_for: datetime
    sent_at: Optional[datetime] = None
    created_at: datetime

    @field_validator("is_bulk", mode="before")
    @classmethod
    def default_is_bulk(cls, v):
        if v is None:
            return False
        return v

    class Config:
        from_attributes = True


# Email Log Schemas
class EmailLogBase(BaseModel):
    recipient_email: EmailStr
    subject: str
    message_content: str
    scheduled_for: Optional[datetime] = None


class EmailLogCreate(EmailLogBase):
    pass


class EmailLog(EmailLogBase):
    id: int
    branch_id: int
    status: str
    delivery_report: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class BulkEmailCreate(BaseModel):
    filter_type: str  # 'today', 'week', 'month', 'year', 'all', 'custom_selection', 'folder'
    subject: str
    message_content: str
    scheduled_for: Optional[datetime] = None
    customer_ids: Optional[List[int]] = None
    folder_id: Optional[int] = None


# Contact Folder Schemas
class FolderCustomerOut(BaseModel):
    id: int
    full_name: str
    phone_number: str
    email: Optional[str] = None

    class Config:
        from_attributes = True


class FolderCreate(BaseModel):
    name: str
    description: Optional[str] = None
    customer_ids: List[int] = []


class FolderUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    customer_ids: Optional[List[int]] = None


class FolderOut(BaseModel):
    id: int
    branch_id: int
    name: str
    description: Optional[str] = None
    created_at: datetime
    total_contacts: int = 0
    customers: List[FolderCustomerOut] = []

    class Config:
        from_attributes = True



# Pagination Wrapper Schemas
class PaginatedCustomer(BaseModel):
    items: List[Customer]
    total: int


class PaginatedSMSLog(BaseModel):
    items: List[SMSLog]
    total: int


class PaginatedEmailLog(BaseModel):
    items: List[EmailLog]
    total: int


class BulkReschedule(BaseModel):
    sms_ids: List[int] = []
    email_ids: List[int] = []
    new_schedule: datetime


# Admin Specific Schemas
class AdminSMSLog(SMSLog):
    company_name: str
    branch_name: str

class AdminEmailLog(EmailLog):
    company_name: str
    branch_name: str

class PaginatedAdminSMSLog(BaseModel):
    items: List[AdminSMSLog]
    total: int

class PaginatedAdminEmailLog(BaseModel):
    items: List[AdminEmailLog]
    total: int

class AdminCustomer(BaseModel):
    id: int
    full_name: str
    phone_number: str
    email: Optional[str] = None
    branch_name: str
    company_name: str
    created_at: datetime

    class Config:
        from_attributes = True

class PaginatedAdminCustomer(BaseModel):
    items: List[AdminCustomer]
    total: int

class GlobalStats(BaseModel):
    total_companies: int
    total_branches: int
    total_sms: int
    total_emails: int
    total_customers: int


# Paystack & Payment Schemas
class PaystackSettings(BaseModel):
    public_key: Optional[str] = None
    secret_key: Optional[str] = None
    currency: Optional[str] = "GHS"
    is_configured: bool = False

class PaystackSettingsUpdate(BaseModel):
    public_key: Optional[str] = None
    secret_key: Optional[str] = None
    currency: Optional[str] = "GHS"

class PaymentInitializeRequest(BaseModel):
    amount: float  # In major currency unit (e.g. 50.00 GHS)
    customer_email: EmailStr
    currency: Optional[str] = None
    company_id: Optional[int] = None
    branch_id: Optional[int] = None
    purpose: Optional[str] = "sms_credits"
    credits_purchased: Optional[int] = None
    callback_url: Optional[str] = None

class PaymentInitializeResponse(BaseModel):
    authorization_url: str
    access_code: str
    reference: str

class PaymentTransactionSchema(BaseModel):
    id: int
    reference: str
    company_id: Optional[int] = None
    branch_id: Optional[int] = None
    company_name: Optional[str] = None
    branch_name: Optional[str] = None
    amount: float
    currency: str
    customer_email: str
    status: str
    channel: Optional[str] = None
    purpose: str
    credits_purchased: Optional[int] = None
    paid_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True

class PaginatedPaymentTransaction(BaseModel):
    items: List[PaymentTransactionSchema]
    total: int



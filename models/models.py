from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from core.database import Base

class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    slug = Column(String, unique=True, index=True)
    is_active = Column(Boolean, default=True)
    
    # SMS Gateway (Hubtel)
    hubtel_client_id = Column(String, nullable=True)
    hubtel_client_secret = Column(String, nullable=True)
    hubtel_sender_id = Column(String, nullable=True)
    
    # Email Gateway (Brevo)
    brevo_api_key = Column(String, nullable=True)
    brevo_sender_email = Column(String, nullable=True)
    brevo_sender_name = Column(String, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    branches = relationship("Branch", back_populates="company", cascade="all, delete-orphan")

class Branch(Base):
    __tablename__ = "branches"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True) # Temporarily nullable for migration
    name = Column(String, unique=True, index=True)
    pin = Column(String)  # Hashed PIN
    branch_code = Column(String, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    company = relationship("Company", back_populates="branches")
    customers = relationship("Customer", back_populates="branch", cascade="all, delete-orphan")

class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    branch_id = Column(Integer, ForeignKey("branches.id"))
    phone_number = Column(String, index=True)
    full_name = Column(String)
    email = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    branch = relationship("Branch", back_populates="customers")

class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    branch_id = Column(Integer, ForeignKey("branches.id"), nullable=True) # Optional global templates if null
    template_type = Column(String)  # e.g., "Promotion", "Reminder"
    content = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    branch = relationship("Branch")

class SMSLog(Base):
    __tablename__ = "sms_logs"

    id = Column(Integer, primary_key=True, index=True)
    branch_id = Column(Integer, ForeignKey("branches.id"), nullable=True) # Link to branch
    phone_number = Column(String)
    message_type = Column(String)  # e.g., "confirmation", "followup"
    message_content = Column(Text)
    status = Column(String)  # "queued", "sent", "failed"
    is_bulk = Column(Boolean, default=False)
    delivery_report = Column(Text, nullable=True)
    scheduled_for = Column(DateTime(timezone=True))
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    branch = relationship("Branch")

class EmailLog(Base):
    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True, index=True)
    branch_id = Column(Integer, ForeignKey("branches.id"))
    recipient_email = Column(String, index=True)
    subject = Column(String)
    message_content = Column(Text)
    status = Column(String)  # "queued", "sent", "failed"
    delivery_report = Column(Text, nullable=True)
    scheduled_for = Column(DateTime(timezone=True))
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    branch = relationship("Branch")

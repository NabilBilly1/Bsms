import os
from typing import Optional
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "Classhouse Bulk SMS"
    DATABASE_URL: str = "sqlite:///./classhouse.db"
    SECRET_KEY: str = "SUPER_SECRET_KEY_FOR_DEV_ONLY"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 1 day

    # Redis for Celery
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    
    # Frontend URL for CORS
    FRONTEND_URL: str = "http://localhost:5173"

    # Paystack Payment Gateway
    PAYSTACK_SECRET_KEY: Optional[str] = None
    PAYSTACK_PUBLIC_KEY: Optional[str] = None
    PAYSTACK_CURRENCY: str = "GHS"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()

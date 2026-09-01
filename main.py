import traceback

from api import admin, auth, customers, email, messages, sms
from core.config import settings
from core.database import Base, engine, get_db
from core.logger import logger
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from models import models as db_models
from schemas import schemas as api_schemas
from sqlalchemy.orm import Session

# Initialize database
db_models.Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.PROJECT_NAME)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://react-frontend-production-fa2a.up.railway.app",
        settings.FRONTEND_URL,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch-all for any unhandled exceptions.
    Logs the full traceback and returns a clean 500 response.
    """
    error_details = traceback.format_exc()
    logger.error(
        f"Global Exception caught at {request.url.path}: {str(exc)}\n{error_details}"
    )

    return JSONResponse(
        status_code=500,
        content={
            "detail": "An unexpected internal server error occurred. Please contact support.",
            "error_type": exc.__class__.__name__,
        },
    )


from sqlalchemy.exc import ProgrammingError


@app.exception_handler(ProgrammingError)
async def programming_error_handler(request: Request, exc: ProgrammingError):
    return JSONResponse(
        status_code=500,
        content={
            "detail": str(exc.orig),  # real DB error
            "statement": exc.statement,  # offending SQL
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """
    Override for standard HTTPExceptions to ensure they are logged.
    """
    logger.warning(f"HTTP {exc.status_code} at {request.url.path}: {exc.detail}")
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/")
def read_root():
    return {"message": "Welcome to Classhouse Bulk SMS API"}


# Include routers
app.include_router(admin.router, prefix="/admin", tags=["Super Admin"])
app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(customers.router, prefix="/customers", tags=["Customers"])
app.include_router(sms.router, prefix="/sms", tags=["SMS Logs"])
app.include_router(email.router, prefix="/email", tags=["Email Logs"])
app.include_router(messages.router, prefix="/messages", tags=["Message Templates"])

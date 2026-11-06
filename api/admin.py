import time

from typing import List, Optional
import io
import csv
import openpyxl
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import text, or_, func, case, distinct
from core.database import get_db
from core.config import settings
from models import models as db_models
from schemas import schemas as api_schemas
from core.sms_utils import normalize_phone, is_valid_phone
from worker.tasks import send_sms_task
from datetime import datetime, timedelta, timezone

router = APIRouter()


@router.get("/health")
def get_infrastructure_health(db: Session = Depends(get_db)):
    """
    Real-time health check for all infrastructure services.
    Returns status of Redis, Celery Workers, and Database.
    """
    health = {}

    # 1. Redis Check
    try:
        import redis as redis_lib
        r = redis_lib.from_url(settings.REDIS_URL, socket_connect_timeout=3)
        start = time.time()
        r.ping()
        latency = round((time.time() - start) * 1000, 1)
        health["redis"] = {"status": "connected", "latency_ms": latency}
    except Exception as e:
        health["redis"] = {"status": "disconnected", "error": str(e)}

    # 2. Celery Worker Check
    try:
        from worker.tasks import celery_app
        inspector = celery_app.control.inspect(timeout=3)
        ping_result = inspector.ping()
        if ping_result:
            worker_names = list(ping_result.keys())
            health["celery_worker"] = {
                "status": "connected",
                "active_workers": len(worker_names),
                "workers": worker_names,
            }
        else:
            health["celery_worker"] = {
                "status": "disconnected",
                "error": "No workers responded to ping",
                "active_workers": 0,
            }
    except Exception as e:
        health["celery_worker"] = {
            "status": "disconnected",
            "error": str(e),
            "active_workers": 0,
        }

    # 3. Database Check
    try:
        start = time.time()
        db.execute(text("SELECT 1"))
        latency = round((time.time() - start) * 1000, 1)
        health["database"] = {"status": "connected", "latency_ms": latency}
    except Exception as e:
        health["database"] = {"status": "disconnected", "error": str(e)}

    # 4. Overall Summary
    services_down = sum(1 for s in health.values() if s["status"] == "disconnected")
    if services_down == 0:
        health["overall"] = "All Systems Operational"
    elif services_down == len(health):
        health["overall"] = "Critical: All Services Down"
    else:
        health["overall"] = f"{services_down} Service{'s' if services_down > 1 else ''} Down"

    return health


@router.post("/companies", response_model=api_schemas.Company)
def create_company(company_in: api_schemas.CompanyCreate, db: Session = Depends(get_db)):
    # Check if slug exists
    db_company = db.query(db_models.Company).filter(db_models.Company.slug == company_in.slug).first()
    if db_company:
        raise HTTPException(status_code=400, detail="Company slug already exists")
    
    new_company = db_models.Company(
        name=company_in.name,
        slug=company_in.slug,
        is_active=company_in.is_active,
        hubtel_client_id=company_in.hubtel_client_id,
        hubtel_client_secret=company_in.hubtel_client_secret,
        hubtel_sender_id=company_in.hubtel_sender_id,
        brevo_api_key=company_in.brevo_api_key,
        brevo_sender_email=company_in.brevo_sender_email,
        brevo_sender_name=company_in.brevo_sender_name,
        paystack_public_key=company_in.paystack_public_key,
        paystack_secret_key=company_in.paystack_secret_key,
    )
    db.add(new_company)
    db.commit()
    db.refresh(new_company)
    return new_company

@router.get("/companies", response_model=List[api_schemas.Company])
def get_companies(db: Session = Depends(get_db)):
    return db.query(db_models.Company).all()

@router.get("/companies/{company_id}", response_model=api_schemas.Company)
def get_company(company_id: int, db: Session = Depends(get_db)):
    company = db.query(db_models.Company).filter(db_models.Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company

@router.put("/companies/{company_id}", response_model=api_schemas.Company)
def update_company(company_id: int, company_in: api_schemas.CompanyUpdate, db: Session = Depends(get_db)):
    company = db.query(db_models.Company).filter(db_models.Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    
    update_data = company_in.dict(exclude_unset=True)
    for field, value in update_data.items():
        setattr(company, field, value)
    
    db.add(company)
    db.commit()
    db.refresh(company)
    return company
 
@router.delete("/companies/{company_id}")
def delete_company(company_id: int, db: Session = Depends(get_db)):
    company = db.query(db_models.Company).filter(db_models.Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    
    db.delete(company)
    db.commit()
    return {"detail": "Company and all associated data cleared"}

from core.security import get_pin_hash

@router.get("/stats", response_model=api_schemas.GlobalStats)
def get_global_stats(db: Session = Depends(get_db)):
    total_companies = db.query(db_models.Company).count()
    total_branches = db.query(db_models.Branch).count()
    total_sms = db.query(db_models.SMSLog).count()
    total_emails = db.query(db_models.EmailLog).count()
    total_customers = db.query(db_models.Customer).count()
    
    return {
        "total_companies": total_companies,
        "total_branches": total_branches,
        "total_sms": total_sms,
        "total_emails": total_emails,
        "total_customers": total_customers
    }


def compute_company_sending_breakdown(db: Session, timeframe: str = "all", company_id: Optional[int] = None):
    now_utc = datetime.utcnow()
    since_dt = None
    if timeframe == "today":
        since_dt = datetime(now_utc.year, now_utc.month, now_utc.day)
    elif timeframe == "7d":
        since_dt = now_utc - timedelta(days=7)
    elif timeframe == "30d":
        since_dt = now_utc - timedelta(days=30)

    # 1. Base company query
    comp_query = db.query(db_models.Company)
    if company_id:
        comp_query = comp_query.filter(db_models.Company.id == company_id)
    companies = comp_query.order_by(db_models.Company.name.asc()).all()

    # 2. Branch counts per company
    branch_counts_raw = (
        db.query(
            db_models.Branch.company_id,
            func.count(db_models.Branch.id)
        )
        .group_by(db_models.Branch.company_id)
        .all()
    )
    branch_counts = {cid: cnt for cid, cnt in branch_counts_raw}

    # 3. Customer counts per company
    customer_counts_raw = (
        db.query(
            db_models.Branch.company_id,
            func.count(db_models.Customer.id)
        )
        .join(db_models.Customer, db_models.Customer.branch_id == db_models.Branch.id)
        .group_by(db_models.Branch.company_id)
        .all()
    )
    customer_counts = {cid: cnt for cid, cnt in customer_counts_raw}

    # 4. SMS counts per company
    sms_q = (
        db.query(
            db_models.Branch.company_id,
            func.count(db_models.SMSLog.id).label("total"),
            func.sum(case((db_models.SMSLog.status.in_(["sent", "delivered"]), 1), else_=0)).label("sent"),
            func.sum(case((db_models.SMSLog.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((db_models.SMSLog.status.in_(["queued", "pending"]), 1), else_=0)).label("queued"),
            func.max(db_models.SMSLog.created_at).label("last_activity")
        )
        .join(db_models.SMSLog, db_models.SMSLog.branch_id == db_models.Branch.id)
    )
    if since_dt:
        sms_q = sms_q.filter(db_models.SMSLog.created_at >= since_dt)
    sms_stats_raw = sms_q.group_by(db_models.Branch.company_id).all()
    sms_by_company = {
        row[0]: {
            "total": row[1] or 0,
            "sent": row[2] or 0,
            "failed": row[3] or 0,
            "queued": row[4] or 0,
            "last_activity": row[5]
        }
        for row in sms_stats_raw
    }

    # 5. Email counts per company
    email_q = (
        db.query(
            db_models.Branch.company_id,
            func.count(db_models.EmailLog.id).label("total"),
            func.sum(case((db_models.EmailLog.status.in_(["sent", "delivered"]), 1), else_=0)).label("sent"),
            func.sum(case((db_models.EmailLog.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((db_models.EmailLog.status.in_(["queued", "pending"]), 1), else_=0)).label("queued"),
            func.max(db_models.EmailLog.created_at).label("last_activity")
        )
        .join(db_models.EmailLog, db_models.EmailLog.branch_id == db_models.Branch.id)
    )
    if since_dt:
        email_q = email_q.filter(db_models.EmailLog.created_at >= since_dt)
    email_stats_raw = email_q.group_by(db_models.Branch.company_id).all()
    email_by_company = {
        row[0]: {
            "total": row[1] or 0,
            "sent": row[2] or 0,
            "failed": row[3] or 0,
            "queued": row[4] or 0,
            "last_activity": row[5]
        }
        for row in email_stats_raw
    }

    # 6. Global today counts (always today)
    today_start = datetime(now_utc.year, now_utc.month, now_utc.day)
    sms_today_count = db.query(func.count(db_models.SMSLog.id)).filter(db_models.SMSLog.created_at >= today_start).scalar() or 0
    email_today_count = db.query(func.count(db_models.EmailLog.id)).filter(db_models.EmailLog.created_at >= today_start).scalar() or 0

    # 7. Build company items
    company_items = []
    tot_sms = 0
    tot_sms_sent = 0
    tot_sms_failed = 0
    tot_sms_queued = 0
    tot_email = 0
    tot_email_sent = 0
    tot_email_failed = 0
    tot_email_queued = 0

    for comp in companies:
        s_data = sms_by_company.get(comp.id, {"total": 0, "sent": 0, "failed": 0, "queued": 0, "last_activity": None})
        e_data = email_by_company.get(comp.id, {"total": 0, "sent": 0, "failed": 0, "queued": 0, "last_activity": None})

        s_tot = s_data["total"]
        s_sent = s_data["sent"]
        s_rate = round((s_sent / s_tot * 100), 1) if s_tot > 0 else 0.0

        e_tot = e_data["total"]
        e_sent = e_data["sent"]
        e_rate = round((e_sent / e_tot * 100), 1) if e_tot > 0 else 0.0

        tot_sms += s_tot
        tot_sms_sent += s_sent
        tot_sms_failed += s_data["failed"]
        tot_sms_queued += s_data["queued"]

        tot_email += e_tot
        tot_email_sent += e_sent
        tot_email_failed += e_data["failed"]
        tot_email_queued += e_data["queued"]

        last_acts = [t for t in [s_data["last_activity"], e_data["last_activity"]] if t]
        latest_act = max(last_acts) if last_acts else None

        gateways = {
            "hubtel": bool(comp.hubtel_client_id and comp.hubtel_client_secret),
            "brevo": bool(comp.brevo_api_key),
            "paystack": bool(comp.paystack_public_key and comp.paystack_secret_key),
        }

        company_items.append(
            api_schemas.CompanySendingBreakdown(
                id=comp.id,
                name=comp.name,
                slug=comp.slug,
                is_active=comp.is_active,
                created_at=comp.created_at,
                branches_count=branch_counts.get(comp.id, 0),
                customers_count=customer_counts.get(comp.id, 0),
                sms=api_schemas.SendingStats(
                    total=s_tot,
                    sent=s_sent,
                    failed=s_data["failed"],
                    queued=s_data["queued"],
                    success_rate=s_rate,
                ),
                email=api_schemas.SendingStats(
                    total=e_tot,
                    sent=e_sent,
                    failed=e_data["failed"],
                    queued=e_data["queued"],
                    success_rate=e_rate,
                ),
                last_activity=latest_act,
                gateways=gateways,
            )
        )

    # 8. Totals
    sms_overall_rate = round((tot_sms_sent / tot_sms * 100), 1) if tot_sms > 0 else 0.0
    email_overall_rate = round((tot_email_sent / tot_email * 100), 1) if tot_email > 0 else 0.0

    totals = api_schemas.DashboardTotals(
        total_companies=len(companies),
        total_branches=sum(branch_counts.values()),
        total_customers=sum(customer_counts.values()),
        total_sms=tot_sms,
        sms_sent=tot_sms_sent,
        sms_failed=tot_sms_failed,
        sms_queued=tot_sms_queued,
        sms_success_rate=sms_overall_rate,
        total_email=tot_email,
        email_sent=tot_email_sent,
        email_failed=tot_email_failed,
        email_queued=tot_email_queued,
        email_success_rate=email_overall_rate,
        sms_today=sms_today_count,
        email_today=email_today_count,
        total_messages=tot_sms + tot_email,
    )

    # 9. Daily trend (last 7 days)
    seven_days_ago = datetime(now_utc.year, now_utc.month, now_utc.day) - timedelta(days=6)
    sms_daily_rows = (
        db.query(
            func.date(db_models.SMSLog.created_at).label("day"),
            func.count(db_models.SMSLog.id).label("cnt")
        )
        .filter(db_models.SMSLog.created_at >= seven_days_ago)
        .group_by(func.date(db_models.SMSLog.created_at))
        .all()
    )
    email_daily_rows = (
        db.query(
            func.date(db_models.EmailLog.created_at).label("day"),
            func.count(db_models.EmailLog.id).label("cnt")
        )
        .filter(db_models.EmailLog.created_at >= seven_days_ago)
        .group_by(func.date(db_models.EmailLog.created_at))
        .all()
    )
    sms_daily_map = {str(r[0]): (r[1] or 0) for r in sms_daily_rows}
    email_daily_map = {str(r[0]): (r[1] or 0) for r in email_daily_rows}

    daily_trend = []
    for i in range(7):
        day_dt = seven_days_ago + timedelta(days=i)
        day_str = day_dt.strftime("%Y-%m-%d")
        s_cnt = sms_daily_map.get(day_str, 0)
        e_cnt = email_daily_map.get(day_str, 0)
        daily_trend.append(
            api_schemas.DailyTrendPoint(
                date=day_str,
                label=day_dt.strftime("%a %d %b"),
                sms=s_cnt,
                email=e_cnt,
                total=s_cnt + e_cnt,
            )
        )

    return {
        "timeframe": timeframe,
        "totals": totals,
        "companies": company_items,
        "daily_trend": daily_trend,
    }


@router.get("/stats/companies", response_model=api_schemas.CompanyBreakdownResponse)
def get_company_sending_stats(
    timeframe: str = "all",
    company_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """
    Returns breakdown of SMS and Email sending stats per company,
    along with platform totals, delivery success rates, and today's activity.
    """
    data = compute_company_sending_breakdown(db, timeframe=timeframe, company_id=company_id)
    return api_schemas.CompanyBreakdownResponse(**data)


@router.get("/stats/companies/export")
def export_company_sending_stats(
    timeframe: str = "all",
    db: Session = Depends(get_db)
):
    """
    Exports the company sending breakdown report as a CSV file.
    """
    data = compute_company_sending_breakdown(db, timeframe=timeframe)
    output = io.StringIO()
    writer = csv.writer(output)

    # Headers
    writer.writerow([
        "Company Name",
        "Company Slug",
        "Status",
        "Branches Count",
        "Contacts Count",
        "SMS Total",
        "SMS Sent",
        "SMS Failed",
        "SMS Queued",
        "SMS Success Rate (%)",
        "Email Total",
        "Email Sent",
        "Email Failed",
        "Email Queued",
        "Email Success Rate (%)",
        "Total Messages",
        "Hubtel Configured",
        "Brevo Configured",
        "Paystack Configured",
        "Last Activity",
    ])

    for comp in data["companies"]:
        tot_msgs = comp.sms.total + comp.email.total
        last_act = comp.last_activity.strftime("%Y-%m-%d %H:%M:%S") if comp.last_activity else "Never"
        writer.writerow([
            comp.name,
            comp.slug,
            "Active" if comp.is_active else "Inactive",
            comp.branches_count,
            comp.customers_count,
            comp.sms.total,
            comp.sms.sent,
            comp.sms.failed,
            comp.sms.queued,
            f"{comp.sms.success_rate}%",
            comp.email.total,
            comp.email.sent,
            comp.email.failed,
            comp.email.queued,
            f"{comp.email.success_rate}%",
            tot_msgs,
            "Yes" if comp.gateways.get("hubtel") else "No",
            "Yes" if comp.gateways.get("brevo") else "No",
            "Yes" if comp.gateways.get("paystack") else "No",
            last_act,
        ])

    output.seek(0)
    filename = f"company_sending_report_{timeframe}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        headers=headers,
        media_type="text/csv",
    )

@router.get("/logs/sms", response_model=api_schemas.PaginatedAdminSMSLog)
def get_global_sms_logs(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    query = (
        db.query(db_models.SMSLog, db_models.Company.name, db_models.Branch.name)
        .join(db_models.Branch, db_models.SMSLog.branch_id == db_models.Branch.id)
        .join(db_models.Company, db_models.Branch.company_id == db_models.Company.id)
    )
    
    total = query.count()
    results = query.order_by(db_models.SMSLog.created_at.desc()).offset(skip).limit(limit).all()
    
    items = []
    for log, company_name, branch_name in results:
        log_dict = {c.name: getattr(log, c.name) for c in log.__table__.columns}
        log_dict["company_name"] = company_name
        log_dict["branch_name"] = branch_name
        items.append(log_dict)
        
    return {"items": items, "total": total}

@router.get("/logs/email", response_model=api_schemas.PaginatedAdminEmailLog)
def get_global_email_logs(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    query = (
        db.query(db_models.EmailLog, db_models.Company.name, db_models.Branch.name)
        .join(db_models.Branch, db_models.EmailLog.branch_id == db_models.Branch.id)
        .join(db_models.Company, db_models.Branch.company_id == db_models.Company.id)
    )
    
    total = query.count()
    results = query.order_by(db_models.EmailLog.created_at.desc()).offset(skip).limit(limit).all()
    
    items = []
    for log, company_name, branch_name in results:
        log_dict = {c.name: getattr(log, c.name) for c in log.__table__.columns}
        log_dict["company_name"] = company_name
        log_dict["branch_name"] = branch_name
        items.append(log_dict)
        
    return {"items": items, "total": total}

@router.delete("/logs/sms/clear-all")
def clear_all_sms_logs(db: Session = Depends(get_db)):
    """
    Deletes all SMS logs from the database.
    """
    try:
        deleted_count = db.query(db_models.SMSLog).delete(synchronize_session=False)
        db.commit()
        return {"message": f"Successfully deleted all {deleted_count} SMS logs", "count": deleted_count}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to clear SMS logs: {str(e)}")

@router.delete("/logs/email/clear-all")
def clear_all_email_logs(db: Session = Depends(get_db)):
    """
    Deletes all Email logs from the database.
    """
    try:
        deleted_count = db.query(db_models.EmailLog).delete(synchronize_session=False)
        db.commit()
        return {"message": f"Successfully deleted all {deleted_count} Email logs", "count": deleted_count}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to clear Email logs: {str(e)}")

@router.delete("/logs/clear-all")
def clear_all_logs(db: Session = Depends(get_db)):
    """
    Deletes all SMS and Email logs from the database.
    """
    try:
        sms_count = db.query(db_models.SMSLog).delete(synchronize_session=False)
        email_count = db.query(db_models.EmailLog).delete(synchronize_session=False)
        db.commit()
        return {
            "message": f"Successfully deleted {sms_count} SMS logs and {email_count} Email logs",
            "sms_count": sms_count,
            "email_count": email_count,
            "total_deleted": sms_count + email_count
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to clear all logs: {str(e)}")

@router.get("/contacts", response_model=api_schemas.PaginatedAdminCustomer)
def get_global_contacts(
    skip: int = 0,
    limit: int = 50,
    search: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = (
        db.query(db_models.Customer, db_models.Company.name, db_models.Branch.name)
        .join(db_models.Branch, db_models.Customer.branch_id == db_models.Branch.id)
        .join(db_models.Company, db_models.Branch.company_id == db_models.Company.id)
    )
    
    if search:
        query = query.filter(
            or_(
                db_models.Customer.full_name.ilike(f"%{search}%"),
                db_models.Customer.phone_number.ilike(f"%{search}%"),
                db_models.Customer.email.ilike(f"%{search}%"),
                db_models.Branch.name.ilike(f"%{search}%"),
                db_models.Company.name.ilike(f"%{search}%")
            )
        )
        
    total = query.count()
    results = query.order_by(db_models.Customer.created_at.desc()).offset(skip).limit(limit).all()
    
    items = []
    for customer, company_name, branch_name in results:
        items.append({
            "id": customer.id,
            "full_name": customer.full_name,
            "phone_number": customer.phone_number,
            "email": customer.email,
            "branch_name": branch_name,
            "company_name": company_name,
            "created_at": customer.created_at
        })
        
    return {"items": items, "total": total}

@router.get("/contacts/export")
def export_global_contacts(
    format: str = "csv",
    db: Session = Depends(get_db)
):
    query = (
        db.query(db_models.Customer, db_models.Company.name, db_models.Branch.name)
        .join(db_models.Branch, db_models.Customer.branch_id == db_models.Branch.id)
        .join(db_models.Company, db_models.Branch.company_id == db_models.Company.id)
        .order_by(db_models.Customer.created_at.desc())
    )
    
    results = query.all()
    
    if format.lower() == "xlsx":
        # Create Excel
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Contacts"
        
        # Headers
        headers = ["Name", "Phone Number", "Email", "Company", "Branch", "Signup Date"]
        ws.append(headers)
        
        for customer, company_name, branch_name in results:
            ws.append([
                customer.full_name,
                customer.phone_number,
                customer.email or "",
                company_name,
                branch_name,
                customer.created_at.strftime("%Y-%m-%d %H:%M:%S") if customer.created_at else ""
            ])
            
        # Write to memory
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        
        headers = {
            'Content-Disposition': 'attachment; filename="contacts.xlsx"'
        }
        return StreamingResponse(
            output,
            headers=headers,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        # Default to CSV
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Headers
        writer.writerow(["Name", "Phone Number", "Email", "Company", "Branch", "Signup Date"])
        
        for customer, company_name, branch_name in results:
            writer.writerow([
                customer.full_name,
                customer.phone_number,
                customer.email or "",
                company_name,
                branch_name,
                customer.created_at.strftime("%Y-%m-%d %H:%M:%S") if customer.created_at else ""
            ])
            
        output.seek(0)
        
        headers = {
            'Content-Disposition': 'attachment; filename="contacts.csv"'
        }
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            headers=headers,
            media_type="text/csv"
        )

@router.post("/contacts/import")
async def import_global_contacts(
    file: UploadFile = File(...),
    default_company_id: Optional[int] = Form(None),
    default_branch_id: Optional[int] = Form(None),
    db: Session = Depends(get_db)
):
    try:
        contents = await file.read()
        rows_data = [] # List of dicts
        
        filename = file.filename.lower()
        if filename.endswith(".csv"):
            stream = io.StringIO(contents.decode("utf-8"))
            reader = csv.reader(stream)
            rows = list(reader)
            if not rows:
                raise HTTPException(status_code=400, detail="Empty CSV file")
            
            # Find header indices
            headers = [h.strip().lower() for h in rows[0]]
            
            # Map headers to indices
            name_idx = next((i for i, h in enumerate(headers) if h in ["name", "full_name", "full name"]), -1)
            phone_idx = next((i for i, h in enumerate(headers) if h in ["phone", "phone_number", "phone number"]), -1)
            email_idx = next((i for i, h in enumerate(headers) if h in ["email", "email_address", "email address"]), -1)
            company_idx = next((i for i, h in enumerate(headers) if h in ["company", "company_slug", "company_name"]), -1)
            branch_idx = next((i for i, h in enumerate(headers) if h in ["branch", "branch_code", "branch_name"]), -1)
            
            # If name or phone headers aren't explicitly matched, assume column 0 is phone/name
            if name_idx == -1 and len(headers) > 1:
                name_idx = 1
            if phone_idx == -1:
                phone_idx = 0
                
            start_row = 1 if len(headers) > 0 and (name_idx != -1 or phone_idx != -1) else 0
            
            for row in rows[start_row:]:
                if not row:
                    continue
                name = row[name_idx].strip() if name_idx != -1 and name_idx < len(row) else ""
                phone = row[phone_idx].strip() if phone_idx != -1 and phone_idx < len(row) else ""
                email = row[email_idx].strip() if email_idx != -1 and email_idx < len(row) else None
                company = row[company_idx].strip() if company_idx != -1 and company_idx < len(row) else None
                branch = row[branch_idx].strip() if branch_idx != -1 and branch_idx < len(row) else None
                
                if phone:
                    rows_data.append({
                        "name": name or "Customer",
                        "phone": phone,
                        "email": email,
                        "company": company,
                        "branch": branch
                    })
                    
        elif filename.endswith((".xlsx", ".xls")):
            wb = openpyxl.load_workbook(io.BytesIO(contents))
            sheet = wb.active
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                raise HTTPException(status_code=400, detail="Empty Excel file")
                
            # Header matching
            headers = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
            name_idx = next((i for i, h in enumerate(headers) if h in ["name", "full_name", "full name"]), -1)
            phone_idx = next((i for i, h in enumerate(headers) if h in ["phone", "phone_number", "phone number"]), -1)
            email_idx = next((i for i, h in enumerate(headers) if h in ["email", "email_address", "email address"]), -1)
            company_idx = next((i for i, h in enumerate(headers) if h in ["company", "company_slug", "company_name"]), -1)
            branch_idx = next((i for i, h in enumerate(headers) if h in ["branch", "branch_code", "branch_name"]), -1)
            
            if name_idx == -1 and len(headers) > 1:
                name_idx = 1
            if phone_idx == -1:
                phone_idx = 0
                
            start_row = 1
            for row in rows[start_row:]:
                if not row or row[0] is None:
                    continue
                name = str(row[name_idx]).strip() if name_idx != -1 and name_idx < len(row) and row[name_idx] is not None else ""
                phone = str(row[phone_idx]).strip() if phone_idx != -1 and phone_idx < len(row) and row[phone_idx] is not None else ""
                email = str(row[email_idx]).strip() if email_idx != -1 and email_idx < len(row) and row[email_idx] is not None else None
                company = str(row[company_idx]).strip() if company_idx != -1 and company_idx < len(row) and row[company_idx] is not None else None
                branch = str(row[branch_idx]).strip() if branch_idx != -1 and branch_idx < len(row) and row[branch_idx] is not None else None
                
                if phone:
                    rows_data.append({
                        "name": name or "Customer",
                        "phone": phone,
                        "email": email,
                        "company": company,
                        "branch": branch
                    })
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format. Please use CSV or Excel.")
            
        if not rows_data:
            raise HTTPException(status_code=400, detail="No valid contact records found in file")
            
        imported_count = 0
        skipped_count = 0
        
        # Load all companies and branches cache to speed up lookup
        companies_cache = {c.slug.lower(): c.id for c in db.query(db_models.Company).all()}
        companies_name_cache = {c.name.lower(): c.id for c in db.query(db_models.Company).all()}
        
        # Cache branches
        branches_list = db.query(db_models.Branch).all()
        branches_code_cache = {(b.company_id, b.branch_code.lower()): b.id for b in branches_list}
        branches_name_cache = {(b.company_id, b.name.lower()): b.id for b in branches_list}

        # Preload existing (branch_id, phone_number) pairs and track in-file seen keys
        existing_branch_phones = {
            (c.branch_id, normalize_phone(c.phone_number))
            for c in db.query(db_models.Customer.branch_id, db_models.Customer.phone_number).all()
        }
        seen_in_file = set()
        
        for r_data in rows_data:
            norm_phone = normalize_phone(r_data["phone"])
            if not is_valid_phone(norm_phone):
                skipped_count += 1
                continue
                
            # Determine target company and branch
            company_id = None
            if r_data["company"]:
                c_str = r_data["company"].lower()
                company_id = companies_cache.get(c_str) or companies_name_cache.get(c_str)
            
            if not company_id:
                company_id = default_company_id
                
            if not company_id:
                skipped_count += 1
                continue
                
            branch_id = None
            if r_data["branch"]:
                b_str = r_data["branch"].lower()
                branch_id = branches_code_cache.get((company_id, b_str)) or branches_name_cache.get((company_id, b_str))
                
            if not branch_id:
                branch_id = default_branch_id
                
            if not branch_id:
                # Try to pick first branch of company
                fb = db.query(db_models.Branch).filter(db_models.Branch.company_id == company_id).first()
                if fb:
                    branch_id = fb.id
                    
            if not branch_id:
                skipped_count += 1
                continue

            dedup_key = (branch_id, norm_phone)
            if dedup_key in seen_in_file or dedup_key in existing_branch_phones:
                skipped_count += 1
                continue

            seen_in_file.add(dedup_key)
            existing_branch_phones.add(dedup_key)

            new_cust = db_models.Customer(
                full_name=r_data["name"],
                phone_number=norm_phone,
                email=r_data["email"],
                branch_id=branch_id
            )
            db.add(new_cust)
            imported_count += 1
                
        db.commit()
        return {"status": "success", "imported_count": imported_count, "skipped_count": skipped_count}
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to process imports: {str(e)}")

@router.post("/companies/{company_id}/branches", response_model=api_schemas.Branch)
def create_company_branch(company_id: int, branch_in: api_schemas.BranchCreate, db: Session = Depends(get_db)):
    # Check if company exists
    company = db.query(db_models.Company).filter(db_models.Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
        
    # Check if branch code exists
    existing = db.query(db_models.Branch).filter(db_models.Branch.branch_code == branch_in.branch_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Branch code already exists")
        
    new_branch = db_models.Branch(
        name=branch_in.name,
        branch_code=branch_in.branch_code,
        pin=get_pin_hash(branch_in.pin),
        company_id=company_id
    )
    db.add(new_branch)
    db.commit()
    db.refresh(new_branch)
    return new_branch

@router.get("/public/companies/{slug}/branches", response_model=List[api_schemas.Branch])
def get_company_branches(slug: str, db: Session = Depends(get_db)):
    company = db.query(db_models.Company).filter(db_models.Company.slug == slug).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company.branches

@router.post("/bulk-sms-import")
async def import_and_send_bulk_sms(
    company_id: int = Form(...),
    branch_id: int = Form(...),
    message_content: str = Form(...),
    scheduled_for: Optional[datetime] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        # 1. Validate Company and Branch
        branch = db.query(db_models.Branch).filter(
            db_models.Branch.id == branch_id,
            db_models.Branch.company_id == company_id
        ).first()
        
        if not branch:
            raise HTTPException(status_code=404, detail="Branch not found or doesn't belong to the company")

        # 2. Parse File
        contents = await file.read()
        recipients = [] # List of dicts: {"phone": ..., "name": ...}

        filename = file.filename.lower()
        if filename.endswith(".csv"):
            stream = io.StringIO(contents.decode("utf-8"))
            reader = csv.reader(stream)
            # Try to detect header
            rows = list(reader)
            if not rows:
                 raise HTTPException(status_code=400, detail="Empty CSV file")
            
            # Simple heuristic: if first row looks like data, it's not a header
            # We'll assume columns are Phone, Name
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
            # Assume Col A: Phone, Col B: Name
            for row in sheet.iter_rows(min_row=1, values_only=True):
                if not row[0]: continue
                # Skip header if any
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

        # 3. Normalize scheduled_for to naive UTC
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

        # 4. Process Recipients
        queued_count = 0
        tasks_to_queue = []
        seen_phones = set()
        for rec in recipients:
            norm_phone = normalize_phone(rec["phone"])
            if not is_valid_phone(norm_phone):
                continue # Skip invalid numbers
            
            if norm_phone in seen_phones:
                continue # Skip duplicates
            seen_phones.add(norm_phone)
            
            # Placeholder replacement
            final_content = message_content.replace("{name}", rec["name"] or "Customer")
            final_content = final_content.replace("{branch}", branch.name)
            
            # Create Log
            new_log = db_models.SMSLog(
                branch_id=branch.id,
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

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to process bulk import: {str(e)}")


# ──────────────────────────────────────────────────────────
# Paystack Management Endpoints (Master Admin)
# ──────────────────────────────────────────────────────────

from core.paystack_utils import get_paystack_credentials, mask_key

@router.get("/paystack/settings", response_model=api_schemas.PaystackSettings)
def get_admin_paystack_settings(db: Session = Depends(get_db)):
    """
    Returns platform-wide Paystack settings with the secret key masked.
    """
    public_key, secret_key, currency = get_paystack_credentials(db)
    return api_schemas.PaystackSettings(
        public_key=public_key or "",
        secret_key=mask_key(secret_key) if secret_key else "",
        currency=currency or "GHS",
        is_configured=bool(public_key and secret_key),
    )


@router.post("/paystack/settings")
def update_admin_paystack_settings(
    settings_in: api_schemas.PaystackSettingsUpdate,
    db: Session = Depends(get_db),
):
    """
    Updates platform-wide Paystack settings in the SystemSetting table.
    """
    try:
        def set_setting(key: str, val: Optional[str]):
            if val is not None:
                record = db.query(db_models.SystemSetting).filter(db_models.SystemSetting.key == key).first()
                if not record:
                    record = db_models.SystemSetting(key=key, value=val.strip() if val else "")
                    db.add(record)
                else:
                    record.value = val.strip() if val else ""

        if settings_in.public_key is not None:
            set_setting("paystack_public_key", settings_in.public_key)

        # Only update secret key if not masked and not empty
        if settings_in.secret_key is not None and not settings_in.secret_key.startswith("sk_") and "***" in settings_in.secret_key:
            pass  # Do not overwrite with masked key
        elif settings_in.secret_key is not None and settings_in.secret_key.strip():
            set_setting("paystack_secret_key", settings_in.secret_key.strip())

        if settings_in.currency is not None:
            set_setting("paystack_currency", settings_in.currency.strip().upper())

        db.commit()
        return {"status": "success", "message": "Paystack settings updated successfully"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update Paystack settings: {str(e)}")


@router.get("/payments", response_model=api_schemas.PaginatedPaymentTransaction)
def get_admin_payments(
    skip: int = 0,
    limit: int = 15,
    search: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Returns paginated list of all payment transactions across the platform.
    """
    query = db.query(db_models.PaymentTransaction)

    if status:
        query = query.filter(db_models.PaymentTransaction.status == status.lower())

    if search:
        s = f"%{search.strip()}%"
        query = query.filter(
            or_(
                db_models.PaymentTransaction.reference.ilike(s),
                db_models.PaymentTransaction.customer_email.ilike(s),
            )
        )

    total = query.count()
    txs = query.order_by(db_models.PaymentTransaction.created_at.desc()).offset(skip).limit(limit).all()

    items = []
    for tx in txs:
        items.append(
            api_schemas.PaymentTransactionSchema(
                id=tx.id,
                reference=tx.reference,
                company_id=tx.company_id,
                branch_id=tx.branch_id,
                company_name=tx.company.name if tx.company else None,
                branch_name=tx.branch.name if tx.branch else None,
                amount=round(tx.amount / 100.0, 2),
                currency=tx.currency,
                customer_email=tx.customer_email,
                status=tx.status,
                channel=tx.channel,
                purpose=tx.purpose,
                credits_purchased=tx.credits_purchased,
                paid_at=tx.paid_at,
                created_at=tx.created_at,
            )
        )

    return api_schemas.PaginatedPaymentTransaction(items=items, total=total)


@router.get("/branches", response_model=List[api_schemas.AdminBranch])
def get_all_admin_branches(db: Session = Depends(get_db)):
    """
    Returns all branches across all companies for admin settings management.
    """
    rows = (
        db.query(db_models.Branch, db_models.Company)
        .join(db_models.Company, db_models.Branch.company_id == db_models.Company.id)
        .order_by(db_models.Company.name.asc(), db_models.Branch.name.asc())
        .all()
    )
    return [
        api_schemas.AdminBranch(
            id=branch.id,
            name=branch.name,
            branch_code=branch.branch_code,
            company_id=branch.company_id,
            company_name=comp.name,
            company_slug=comp.slug,
            created_at=branch.created_at,
        )
        for branch, comp in rows
    ]


@router.put("/branches/{branch_id}", response_model=api_schemas.Branch)
def update_company_branch(
    branch_id: int,
    branch_in: api_schemas.BranchUpdate,
    db: Session = Depends(get_db),
):
    """
    Updates a branch's password/PIN, name, or branch code from the Master Admin settings.
    """
    branch = db.query(db_models.Branch).filter(db_models.Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")

    if branch_in.name is not None and branch_in.name.strip():
        branch.name = branch_in.name.strip()

    if branch_in.branch_code is not None and branch_in.branch_code.strip():
        new_code = branch_in.branch_code.strip()
        if new_code != branch.branch_code:
            existing = (
                db.query(db_models.Branch)
                .filter(
                    db_models.Branch.branch_code == new_code,
                    db_models.Branch.id != branch_id,
                )
                .first()
            )
            if existing:
                raise HTTPException(status_code=400, detail="Branch code already exists")
            branch.branch_code = new_code

    if branch_in.pin is not None and branch_in.pin.strip():
        branch.pin = get_pin_hash(branch_in.pin.strip())

    db.add(branch)
    db.commit()
    db.refresh(branch)
    return branch


# ──────────────────────────────────────────────────────────
# Message Template Management Endpoints (Master Admin)
# ──────────────────────────────────────────────────────────

@router.get("/templates", response_model=List[api_schemas.AdminTemplate])
def get_admin_templates(
    search: Optional[str] = None,
    company_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    scope: Optional[str] = None,  # "all", "global", "branch"
    db: Session = Depends(get_db),
):
    """
    Returns all message templates (both Global and Branch-specific) with branch and company details.
    """
    query = (
        db.query(db_models.Message, db_models.Branch, db_models.Company)
        .outerjoin(db_models.Branch, db_models.Message.branch_id == db_models.Branch.id)
        .outerjoin(db_models.Company, db_models.Branch.company_id == db_models.Company.id)
    )

    if scope == "global":
        query = query.filter(db_models.Message.branch_id == None)
    elif scope == "branch":
        query = query.filter(db_models.Message.branch_id != None)

    if company_id is not None:
        query = query.filter(db_models.Branch.company_id == company_id)

    if branch_id is not None:
        query = query.filter(db_models.Message.branch_id == branch_id)

    if search and search.strip():
        s = f"%{search.strip()}%"
        query = query.filter(
            or_(
                db_models.Message.template_type.ilike(s),
                db_models.Message.content.ilike(s),
                db_models.Branch.name.ilike(s),
                db_models.Company.name.ilike(s),
            )
        )

    rows = query.order_by(db_models.Message.created_at.desc()).all()

    return [
        api_schemas.AdminTemplate(
            id=msg.id,
            template_type=msg.template_type,
            content=msg.content,
            branch_id=msg.branch_id,
            branch_name=branch.name if branch else "Global Template (All Branches)",
            company_id=comp.id if comp else None,
            company_name=comp.name if comp else "All Companies",
            created_at=msg.created_at,
        )
        for msg, branch, comp in rows
    ]


import re


def _normalize_template_text(val: Optional[str]) -> str:
    if not val:
        return ""
    text = re.sub(r"<[^>]*>", " ", str(val))
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


@router.post("/templates", response_model=api_schemas.AdminTemplate)
def create_admin_template(
    msg_in: api_schemas.MessageCreate,
    db: Session = Depends(get_db),
):
    """
    Creates a new message template (Global if branch_id is None, or assigned to a specific branch).
    Rejects duplicates with the same normalized name + content or identical content in the same scope.
    """
    if not msg_in.template_type or not msg_in.template_type.strip():
        raise HTTPException(status_code=400, detail="Template name/category is required")
    if not msg_in.content or not msg_in.content.strip():
        raise HTTPException(status_code=400, detail="Template content is required")

    branch = None
    comp = None
    if msg_in.branch_id is not None:
        branch = db.query(db_models.Branch).filter(db_models.Branch.id == msg_in.branch_id).first()
        if not branch:
            raise HTTPException(status_code=404, detail="Selected branch not found")
        comp = branch.company

    norm_type = _normalize_template_text(msg_in.template_type)
    norm_content = _normalize_template_text(msg_in.content)

    # Check existing templates in the same scope (or global)
    scope_templates = (
        db.query(db_models.Message)
        .filter(
            (db_models.Message.branch_id == msg_in.branch_id)
            | (db_models.Message.branch_id == None)
        )
        .all()
    )
    for existing in scope_templates:
        ex_type = _normalize_template_text(existing.template_type)
        ex_content = _normalize_template_text(existing.content)
        if ex_content == norm_content or (ex_type == norm_type and existing.branch_id == msg_in.branch_id):
            raise HTTPException(
                status_code=400,
                detail="Duplicate template: A template with the same name or message content already exists in this scope.",
            )

    new_msg = db_models.Message(
        template_type=msg_in.template_type.strip(),
        content=msg_in.content.strip(),
        branch_id=msg_in.branch_id,
    )
    db.add(new_msg)
    db.commit()
    db.refresh(new_msg)

    return api_schemas.AdminTemplate(
        id=new_msg.id,
        template_type=new_msg.template_type,
        content=new_msg.content,
        branch_id=new_msg.branch_id,
        branch_name=branch.name if branch else "Global Template (All Branches)",
        company_id=comp.id if comp else None,
        company_name=comp.name if comp else "All Companies",
        created_at=new_msg.created_at,
    )


@router.put("/templates/{template_id}", response_model=api_schemas.AdminTemplate)
def update_admin_template(
    template_id: int,
    msg_in: api_schemas.MessageUpdate,
    db: Session = Depends(get_db),
):
    """
    Updates an existing message template's name/type, content, or branch assignment.
    """
    msg = db.query(db_models.Message).filter(db_models.Message.id == template_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Template not found")

    new_type = msg_in.template_type.strip() if msg_in.template_type is not None and msg_in.template_type.strip() else msg.template_type
    new_content = msg_in.content.strip() if msg_in.content is not None and msg_in.content.strip() else msg.content
    new_branch_id = msg.branch_id

    # Allow setting branch_id to a specific branch OR None (Global)
    if "branch_id" in msg_in.model_fields_set:
        if msg_in.branch_id is not None:
            branch_check = db.query(db_models.Branch).filter(db_models.Branch.id == msg_in.branch_id).first()
            if not branch_check:
                raise HTTPException(status_code=404, detail="Selected branch not found")
        new_branch_id = msg_in.branch_id

    norm_type = _normalize_template_text(new_type)
    norm_content = _normalize_template_text(new_content)

    scope_templates = (
        db.query(db_models.Message)
        .filter(
            db_models.Message.id != template_id,
            (db_models.Message.branch_id == new_branch_id)
            | (db_models.Message.branch_id == None),
        )
        .all()
    )
    for existing in scope_templates:
        ex_type = _normalize_template_text(existing.template_type)
        ex_content = _normalize_template_text(existing.content)
        if ex_content == norm_content or (ex_type == norm_type and existing.branch_id == new_branch_id):
            raise HTTPException(
                status_code=400,
                detail="Duplicate template: Another template with the same name or message content already exists.",
            )

    msg.template_type = new_type
    msg.content = new_content
    msg.branch_id = new_branch_id

    db.add(msg)
    db.commit()
    db.refresh(msg)

    branch = db.query(db_models.Branch).filter(db_models.Branch.id == msg.branch_id).first() if msg.branch_id else None
    comp = branch.company if branch else None

    return api_schemas.AdminTemplate(
        id=msg.id,
        template_type=msg.template_type,
        content=msg.content,
        branch_id=msg.branch_id,
        branch_name=branch.name if branch else "Global Template (All Branches)",
        company_id=comp.id if comp else None,
        company_name=comp.name if comp else "All Companies",
        created_at=msg.created_at,
    )


@router.delete("/templates/{template_id}")
def delete_admin_template(
    template_id: int,
    db: Session = Depends(get_db),
):
    """
    Deletes a message template by ID.
    """
    msg = db.query(db_models.Message).filter(db_models.Message.id == template_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Template not found")

    db.delete(msg)
    db.commit()
    return {"status": "success", "message": "Template deleted successfully"}


@router.post("/templates/import")
async def import_admin_templates(
    branch_id: Optional[int] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Imports message templates from a CSV, Excel (.xlsx/.xls), or JSON file.
    Strictly prevents duplicates both within the uploaded file and against existing database templates.
    """
    try:
        if branch_id is not None:
            branch_check = db.query(db_models.Branch).filter(db_models.Branch.id == branch_id).first()
            if not branch_check:
                raise HTTPException(status_code=404, detail="Target branch not found")

        contents = await file.read()
        filename = (file.filename or "").lower()
        parsed_templates = []

        if filename.endswith(".json"):
            import json
            data = json.loads(contents.decode("utf-8-sig"))
            items = data if isinstance(data, list) else data.get("templates", [])
            for item in items:
                if isinstance(item, dict):
                    t_type = (
                        item.get("template_type")
                        or item.get("name")
                        or item.get("title")
                        or item.get("category")
                        or "General"
                    )
                    t_content = (
                        item.get("content")
                        or item.get("message")
                        or item.get("body")
                        or item.get("text")
                        or ""
                    )
                    if str(t_content).strip():
                        parsed_templates.append({
                            "template_type": str(t_type).strip(),
                            "content": str(t_content).strip(),
                        })

        elif filename.endswith(".csv"):
            stream = io.StringIO(contents.decode("utf-8-sig"))
            reader = csv.reader(stream)
            rows = [r for r in reader if any(cell.strip() for cell in r)]
            if not rows:
                raise HTTPException(status_code=400, detail="CSV file is empty")

            header = [c.strip().lower() for c in rows[0]]
            start_idx = 0
            type_col, content_col = 0, 1

            if any(h in ["template_type", "type", "name", "title", "category", "content", "message", "body"] for h in header):
                start_idx = 1
                for idx, h in enumerate(header):
                    if h in ["template_type", "type", "name", "title", "category"]:
                        type_col = idx
                    elif h in ["content", "message", "body", "text", "template"]:
                        content_col = idx

            for row in rows[start_idx:]:
                if len(row) == 1 and row[0].strip():
                    parsed_templates.append({"template_type": "General", "content": row[0].strip()})
                elif len(row) >= 2:
                    t_type = row[type_col].strip() if type_col < len(row) and row[type_col].strip() else "General"
                    t_content = row[content_col].strip() if content_col < len(row) else ""
                    if t_content:
                        parsed_templates.append({"template_type": t_type, "content": t_content})

        elif filename.endswith((".xlsx", ".xls")):
            wb = openpyxl.load_workbook(io.BytesIO(contents))
            sheet = wb.active
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                raise HTTPException(status_code=400, detail="Excel file is empty")

            header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]
            start_idx = 0
            type_col, content_col = 0, 1

            if any(h in ["template_type", "type", "name", "title", "category", "content", "message", "body"] for h in header):
                start_idx = 1
                for idx, h in enumerate(header):
                    if h in ["template_type", "type", "name", "title", "category"]:
                        type_col = idx
                    elif h in ["content", "message", "body", "text", "template"]:
                        content_col = idx

            for row in rows[start_idx:]:
                if not row:
                    continue
                if len(row) == 1 and row[0]:
                    parsed_templates.append({"template_type": "General", "content": str(row[0]).strip()})
                elif len(row) >= 2:
                    t_type = str(row[type_col]).strip() if type_col < len(row) and row[type_col] is not None else "General"
                    t_content = str(row[content_col]).strip() if content_col < len(row) and row[content_col] is not None else ""
                    if t_content:
                        parsed_templates.append({"template_type": t_type or "General", "content": t_content})
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format. Please upload CSV, Excel (.xlsx), or JSON.")

        if not parsed_templates:
            raise HTTPException(status_code=400, detail="No valid templates found in the uploaded file.")

        # ── Two-Layer Deduplication (DB + In-File) ──
        existing_db_templates = (
            db.query(db_models.Message)
            .filter(
                (db_models.Message.branch_id == branch_id)
                | (db_models.Message.branch_id == None)
            )
            .all()
        )
        seen_contents = {_normalize_template_text(m.content) for m in existing_db_templates}
        seen_type_and_scope = {
            (m.branch_id, _normalize_template_text(m.template_type))
            for m in existing_db_templates
        }

        imported_count = 0
        skipped_count = 0

        for item in parsed_templates:
            norm_type = _normalize_template_text(item["template_type"])
            norm_content = _normalize_template_text(item["content"])

            if not norm_content:
                skipped_count += 1
                continue

            if norm_content in seen_contents or (branch_id, norm_type) in seen_type_and_scope:
                skipped_count += 1
                continue

            seen_contents.add(norm_content)
            seen_type_and_scope.add((branch_id, norm_type))

            new_msg = db_models.Message(
                template_type=item["template_type"],
                content=item["content"],
                branch_id=branch_id,
            )
            db.add(new_msg)
            imported_count += 1

        db.commit()
        return {
            "status": "success",
            "imported_count": imported_count,
            "skipped_count": skipped_count,
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to import templates: {str(e)}")


# ──────────────────────────────────────────────────────────
# Full System Backup & Restore Endpoints (Master Admin)
# ──────────────────────────────────────────────────────────

import json
from fastapi.responses import StreamingResponse


def _parse_iso_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


@router.get("/backup/summary")
def get_system_backup_summary(db: Session = Depends(get_db)):
    """
    Returns live record counts across every database table for the System Backup & Restore page.
    """
    folder_links_count = db.execute(db_models.folder_customers.select()).fetchall()
    return {
        "companies": db.query(db_models.Company).count(),
        "branches": db.query(db_models.Branch).count(),
        "customers": db.query(db_models.Customer).count(),
        "contact_folders": db.query(db_models.ContactFolder).count(),
        "folder_links": len(folder_links_count),
        "templates": db.query(db_models.Message).count(),
        "sms_logs": db.query(db_models.SMSLog).count(),
        "email_logs": db.query(db_models.EmailLog).count(),
        "system_settings": db.query(db_models.SystemSetting).count(),
        "payment_transactions": db.query(db_models.PaymentTransaction).count(),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


@router.get("/backup/export")
def export_full_system_backup(db: Session = Depends(get_db)):
    """
    Exports every single record across the entire platform into a complete, self-contained JSON backup archive.
    Includes Companies, Branches (with hashed PINs), Customers, Contact Folders, Folder-Customer Links,
    Message Templates, SMS Logs, Email Logs, System Settings, and Payment Transactions.
    """
    companies = db.query(db_models.Company).order_by(db_models.Company.id.asc()).all()
    branches = db.query(db_models.Branch).order_by(db_models.Branch.id.asc()).all()
    customers = db.query(db_models.Customer).order_by(db_models.Customer.id.asc()).all()
    folders = db.query(db_models.ContactFolder).order_by(db_models.ContactFolder.id.asc()).all()
    templates = db.query(db_models.Message).order_by(db_models.Message.id.asc()).all()
    sms_logs = db.query(db_models.SMSLog).order_by(db_models.SMSLog.id.asc()).all()
    email_logs = db.query(db_models.EmailLog).order_by(db_models.EmailLog.id.asc()).all()
    settings_rows = db.query(db_models.SystemSetting).order_by(db_models.SystemSetting.id.asc()).all()
    payments = db.query(db_models.PaymentTransaction).order_by(db_models.PaymentTransaction.id.asc()).all()

    company_by_id = {c.id: c for c in companies}
    branch_by_id = {b.id: b for b in branches}

    backup_payload = {
        "metadata": {
            "platform": "PeakSend Admin Dashboard",
            "backup_version": "1.0",
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "counts": {
                "companies": len(companies),
                "branches": len(branches),
                "customers": len(customers),
                "contact_folders": len(folders),
                "templates": len(templates),
                "sms_logs": len(sms_logs),
                "email_logs": len(email_logs),
                "system_settings": len(settings_rows),
                "payment_transactions": len(payments),
            },
        },
        "system_settings": [
            {
                "key": s.key,
                "value": s.value,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in settings_rows
        ],
        "companies": [
            {
                "id": c.id,
                "name": c.name,
                "slug": c.slug,
                "is_active": c.is_active,
                "hubtel_client_id": c.hubtel_client_id,
                "hubtel_client_secret": c.hubtel_client_secret,
                "hubtel_sender_id": c.hubtel_sender_id,
                "brevo_api_key": c.brevo_api_key,
                "brevo_sender_email": c.brevo_sender_email,
                "brevo_sender_name": c.brevo_sender_name,
                "paystack_public_key": c.paystack_public_key,
                "paystack_secret_key": c.paystack_secret_key,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in companies
        ],
        "branches": [
            {
                "id": b.id,
                "company_id": b.company_id,
                "company_slug": company_by_id[b.company_id].slug if b.company_id in company_by_id else None,
                "name": b.name,
                "branch_code": b.branch_code,
                "pin_hash": b.pin,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in branches
        ],
        "customers": [
            {
                "id": cust.id,
                "branch_id": cust.branch_id,
                "branch_code": branch_by_id[cust.branch_id].branch_code if cust.branch_id in branch_by_id else None,
                "full_name": cust.full_name,
                "phone_number": cust.phone_number,
                "email": cust.email,
                "created_at": cust.created_at.isoformat() if cust.created_at else None,
            }
            for cust in customers
        ],
        "contact_folders": [
            {
                "id": f.id,
                "branch_id": f.branch_id,
                "branch_code": branch_by_id[f.branch_id].branch_code if f.branch_id in branch_by_id else None,
                "name": f.name,
                "description": f.description,
                "customer_phones": [c.phone_number for c in f.customers],
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in folders
        ],
        "templates": [
            {
                "id": m.id,
                "branch_id": m.branch_id,
                "branch_code": branch_by_id[m.branch_id].branch_code if m.branch_id in branch_by_id else None,
                "template_type": m.template_type,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in templates
        ],
        "sms_logs": [
            {
                "id": l.id,
                "branch_id": l.branch_id,
                "branch_code": branch_by_id[l.branch_id].branch_code if l.branch_id in branch_by_id else None,
                "phone_number": l.phone_number,
                "message_type": l.message_type,
                "message_content": l.message_content,
                "status": l.status,
                "is_bulk": bool(l.is_bulk),
                "delivery_report": l.delivery_report,
                "scheduled_for": l.scheduled_for.isoformat() if l.scheduled_for else None,
                "sent_at": l.sent_at.isoformat() if l.sent_at else None,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in sms_logs
        ],
        "email_logs": [
            {
                "id": e.id,
                "branch_id": e.branch_id,
                "branch_code": branch_by_id[e.branch_id].branch_code if e.branch_id in branch_by_id else None,
                "recipient_email": e.recipient_email,
                "subject": e.subject,
                "message_content": e.message_content,
                "status": e.status,
                "delivery_report": e.delivery_report,
                "scheduled_for": e.scheduled_for.isoformat() if e.scheduled_for else None,
                "sent_at": e.sent_at.isoformat() if e.sent_at else None,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in email_logs
        ],
        "payment_transactions": [
            {
                "id": p.id,
                "reference": p.reference,
                "company_id": p.company_id,
                "company_slug": company_by_id[p.company_id].slug if p.company_id in company_by_id else None,
                "branch_id": p.branch_id,
                "branch_code": branch_by_id[p.branch_id].branch_code if p.branch_id in branch_by_id else None,
                "amount": p.amount,
                "currency": p.currency,
                "customer_email": p.customer_email,
                "status": p.status,
                "channel": p.channel,
                "purpose": p.purpose,
                "credits_purchased": p.credits_purchased,
                "paystack_response": p.paystack_response,
                "paid_at": p.paid_at.isoformat() if p.paid_at else None,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in payments
        ],
    }

    json_bytes = json.dumps(backup_payload, indent=2).encode("utf-8")
    timestamp_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"peaksend_full_backup_{timestamp_str}.json"

    return StreamingResponse(
        io.BytesIO(json_bytes),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/backup/import")
async def import_full_system_backup(
    mode: str = Form("merge"),  # "merge" (safe deduplicating restore) or "update" (also overwrite existing company/setting keys)
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Restores a full PeakSend JSON backup archive with relational ID mapping and strict duplicate prevention.
    """
    try:
        contents = await file.read()
        try:
            data = json.loads(contents.decode("utf-8-sig"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON backup file.")

        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Backup file root must be a JSON object.")

        stats = {
            "companies": {"imported": 0, "skipped": 0, "updated": 0},
            "branches": {"imported": 0, "skipped": 0, "updated": 0},
            "customers": {"imported": 0, "skipped": 0},
            "contact_folders": {"imported": 0, "skipped": 0},
            "templates": {"imported": 0, "skipped": 0},
            "sms_logs": {"imported": 0, "skipped": 0},
            "email_logs": {"imported": 0, "skipped": 0},
            "system_settings": {"imported": 0, "skipped": 0, "updated": 0},
            "payment_transactions": {"imported": 0, "skipped": 0},
        }

        # 1. Restore System Settings
        existing_settings = {
            s.key: s for s in db.query(db_models.SystemSetting).all()
        }
        for s_item in data.get("system_settings", []):
            key = (s_item.get("key") or "").strip()
            if not key:
                continue
            val = s_item.get("value")
            if key in existing_settings:
                if mode == "update" and val is not None:
                    existing_settings[key].value = val
                    stats["system_settings"]["updated"] += 1
                else:
                    stats["system_settings"]["skipped"] += 1
            else:
                new_s = db_models.SystemSetting(key=key, value=val)
                db.add(new_s)
                existing_settings[key] = new_s
                stats["system_settings"]["imported"] += 1

        db.flush()

        # 2. Restore Companies & Build ID Map (old_company_id -> current_company_id, slug -> current_company_id)
        company_id_map = {}
        company_slug_map = {}
        for c in db.query(db_models.Company).all():
            company_slug_map[c.slug.lower()] = c

        for c_item in data.get("companies", []):
            slug = (c_item.get("slug") or "").strip()
            name = (c_item.get("name") or "").strip()
            old_id = c_item.get("id")
            if not slug or not name:
                stats["companies"]["skipped"] += 1
                continue

            existing_comp = (
                company_slug_map.get(slug.lower())
                or db.query(db_models.Company).filter(db_models.Company.name.ilike(name)).first()
            )
            if existing_comp:
                if mode == "update":
                    for field in [
                        "hubtel_client_id",
                        "hubtel_client_secret",
                        "hubtel_sender_id",
                        "brevo_api_key",
                        "brevo_sender_email",
                        "brevo_sender_name",
                        "paystack_public_key",
                        "paystack_secret_key",
                    ]:
                        if c_item.get(field):
                            setattr(existing_comp, field, c_item.get(field))
                    if "is_active" in c_item and c_item["is_active"] is not None:
                        existing_comp.is_active = bool(c_item["is_active"])
                    stats["companies"]["updated"] += 1
                else:
                    stats["companies"]["skipped"] += 1
                if old_id is not None:
                    company_id_map[old_id] = existing_comp.id
                company_slug_map[slug.lower()] = existing_comp
            else:
                new_comp = db_models.Company(
                    name=name,
                    slug=slug,
                    is_active=bool(c_item.get("is_active", True)),
                    hubtel_client_id=c_item.get("hubtel_client_id"),
                    hubtel_client_secret=c_item.get("hubtel_client_secret"),
                    hubtel_sender_id=c_item.get("hubtel_sender_id"),
                    brevo_api_key=c_item.get("brevo_api_key"),
                    brevo_sender_email=c_item.get("brevo_sender_email"),
                    brevo_sender_name=c_item.get("brevo_sender_name"),
                    paystack_public_key=c_item.get("paystack_public_key"),
                    paystack_secret_key=c_item.get("paystack_secret_key"),
                )
                created_dt = _parse_iso_dt(c_item.get("created_at"))
                if created_dt:
                    new_comp.created_at = created_dt
                db.add(new_comp)
                db.flush()
                if old_id is not None:
                    company_id_map[old_id] = new_comp.id
                company_slug_map[slug.lower()] = new_comp
                stats["companies"]["imported"] += 1

        # 3. Restore Branches & Build ID Map (old_branch_id -> current_branch_id, branch_code -> current_branch_id)
        branch_id_map = {}
        branch_code_map = {}
        for b in db.query(db_models.Branch).all():
            branch_code_map[b.branch_code.lower()] = b

        for b_item in data.get("branches", []):
            old_b_id = b_item.get("id")
            b_code = (b_item.get("branch_code") or "").strip()
            b_name = (b_item.get("name") or "").strip()
            if not b_code or not b_name:
                stats["branches"]["skipped"] += 1
                continue

            # Resolve company_id
            resolved_comp_id = None
            c_slug = (b_item.get("company_slug") or "").strip().lower()
            if c_slug and c_slug in company_slug_map:
                resolved_comp_id = company_slug_map[c_slug].id
            elif b_item.get("company_id") in company_id_map:
                resolved_comp_id = company_id_map[b_item.get("company_id")]

            existing_branch = (
                branch_code_map.get(b_code.lower())
                or db.query(db_models.Branch).filter(db_models.Branch.name.ilike(b_name)).first()
            )
            if existing_branch:
                if mode == "update" and b_item.get("pin_hash"):
                    existing_branch.pin = b_item["pin_hash"]
                    stats["branches"]["updated"] += 1
                else:
                    stats["branches"]["skipped"] += 1
                if old_b_id is not None:
                    branch_id_map[old_b_id] = existing_branch.id
                branch_code_map[b_code.lower()] = existing_branch
            else:
                pin_val = b_item.get("pin_hash")
                if not pin_val and b_item.get("pin"):
                    pin_val = get_pin_hash(str(b_item.get("pin")))
                elif not pin_val:
                    pin_val = get_pin_hash("1234")

                new_branch = db_models.Branch(
                    company_id=resolved_comp_id,
                    name=b_name,
                    branch_code=b_code,
                    pin=pin_val,
                )
                created_dt = _parse_iso_dt(b_item.get("created_at"))
                if created_dt:
                    new_branch.created_at = created_dt
                db.add(new_branch)
                db.flush()
                if old_b_id is not None:
                    branch_id_map[old_b_id] = new_branch.id
                branch_code_map[b_code.lower()] = new_branch
                stats["branches"]["imported"] += 1

        # Helper to resolve branch_id from backup item
        def resolve_branch_id(item_dict: dict) -> Optional[int]:
            b_code_str = (item_dict.get("branch_code") or "").strip().lower()
            if b_code_str and b_code_str in branch_code_map:
                return branch_code_map[b_code_str].id
            old_bid = item_dict.get("branch_id")
            if old_bid is not None and old_bid in branch_id_map:
                return branch_id_map[old_bid]
            return None

        # 4. Restore Customers (Deduplicate by (branch_id, normalized_phone))
        existing_customers = {}
        for cust in db.query(db_models.Customer).all():
            existing_customers[(cust.branch_id, normalize_phone(cust.phone_number))] = cust

        for cust_item in data.get("customers", []):
            target_bid = resolve_branch_id(cust_item)
            raw_phone = (cust_item.get("phone_number") or "").strip()
            norm_p = normalize_phone(raw_phone)
            if not target_bid or not norm_p:
                stats["customers"]["skipped"] += 1
                continue

            key = (target_bid, norm_p)
            if key in existing_customers:
                stats["customers"]["skipped"] += 1
                continue

            new_cust = db_models.Customer(
                branch_id=target_bid,
                full_name=(cust_item.get("full_name") or "Customer").strip(),
                phone_number=norm_p,
                email=cust_item.get("email"),
            )
            created_dt = _parse_iso_dt(cust_item.get("created_at"))
            if created_dt:
                new_cust.created_at = created_dt
            db.add(new_cust)
            db.flush()
            existing_customers[key] = new_cust
            stats["customers"]["imported"] += 1

        # 5. Restore Contact Folders & Customer Memberships
        existing_folders = {
            (f.branch_id, f.name.strip().lower()): f
            for f in db.query(db_models.ContactFolder).all()
        }
        for f_item in data.get("contact_folders", []):
            target_bid = resolve_branch_id(f_item)
            f_name = (f_item.get("name") or "").strip()
            if not target_bid or not f_name:
                stats["contact_folders"]["skipped"] += 1
                continue

            f_key = (target_bid, f_name.lower())
            if f_key in existing_folders:
                folder_obj = existing_folders[f_key]
                stats["contact_folders"]["skipped"] += 1
            else:
                folder_obj = db_models.ContactFolder(
                    branch_id=target_bid,
                    name=f_name,
                    description=f_item.get("description"),
                )
                created_dt = _parse_iso_dt(f_item.get("created_at"))
                if created_dt:
                    folder_obj.created_at = created_dt
                db.add(folder_obj)
                db.flush()
                existing_folders[f_key] = folder_obj
                stats["contact_folders"]["imported"] += 1

            # Link customers in folder
            existing_member_ids = {c.id for c in folder_obj.customers}
            for phone_str in f_item.get("customer_phones", []):
                norm_p = normalize_phone(phone_str)
                cust_obj = existing_customers.get((target_bid, norm_p))
                if cust_obj and cust_obj.id not in existing_member_ids:
                    folder_obj.customers.append(cust_obj)
                    existing_member_ids.add(cust_obj.id)

        # 6. Restore Message Templates (Deduplicate by normalized content & (branch_id, normalized_type))
        existing_templates = db.query(db_models.Message).all()
        seen_tpl_Keys = {
            (m.branch_id, _normalize_template_text(m.content))
            for m in existing_templates
        }
        seen_tpl_names = {
            (m.branch_id, _normalize_template_text(m.template_type))
            for m in existing_templates
        }

        for t_item in data.get("templates", []):
            target_bid = resolve_branch_id(t_item) if t_item.get("branch_id") is not None or t_item.get("branch_code") else None
            t_type = (t_item.get("template_type") or "General").strip()
            t_content = (t_item.get("content") or "").strip()
            norm_t = _normalize_template_text(t_type)
            norm_c = _normalize_template_text(t_content)

            if not norm_c:
                stats["templates"]["skipped"] += 1
                continue

            if (target_bid, norm_c) in seen_tpl_Keys or (target_bid, norm_t) in seen_tpl_names:
                stats["templates"]["skipped"] += 1
                continue

            seen_tpl_Keys.add((target_bid, norm_c))
            seen_tpl_names.add((target_bid, norm_t))

            new_tpl = db_models.Message(
                branch_id=target_bid,
                template_type=t_type,
                content=t_content,
            )
            created_dt = _parse_iso_dt(t_item.get("created_at"))
            if created_dt:
                new_tpl.created_at = created_dt
            db.add(new_tpl)
            stats["templates"]["imported"] += 1

        # 7. Restore SMS Logs (Deduplicate by branch_id + phone_number + message_content + created_at)
        existing_sms_keys = {
            (
                s.branch_id,
                s.phone_number,
                s.message_content,
                s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else "",
            )
            for s in db.query(db_models.SMSLog).all()
        }
        for s_item in data.get("sms_logs", []):
            target_bid = resolve_branch_id(s_item)
            phone = s_item.get("phone_number") or ""
            content = s_item.get("message_content") or ""
            created_dt = _parse_iso_dt(s_item.get("created_at"))
            created_key = created_dt.strftime("%Y-%m-%d %H:%M:%S") if created_dt else ""

            s_key = (target_bid, phone, content, created_key)
            if s_key in existing_sms_keys:
                stats["sms_logs"]["skipped"] += 1
                continue

            existing_sms_keys.add(s_key)
            new_sms = db_models.SMSLog(
                branch_id=target_bid,
                phone_number=phone,
                message_type=s_item.get("message_type") or "single",
                message_content=content,
                status=s_item.get("status") or "sent",
                is_bulk=bool(s_item.get("is_bulk", False)),
                delivery_report=s_item.get("delivery_report"),
                scheduled_for=_parse_iso_dt(s_item.get("scheduled_for")) or created_dt or datetime.utcnow(),
                sent_at=_parse_iso_dt(s_item.get("sent_at")),
            )
            if created_dt:
                new_sms.created_at = created_dt
            db.add(new_sms)
            stats["sms_logs"]["imported"] += 1

        # 8. Restore Email Logs (Deduplicate by branch_id + recipient_email + subject + created_at)
        existing_email_keys = {
            (
                e.branch_id,
                e.recipient_email,
                e.subject,
                e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else "",
            )
            for e in db.query(db_models.EmailLog).all()
        }
        for e_item in data.get("email_logs", []):
            target_bid = resolve_branch_id(e_item)
            if not target_bid:
                stats["email_logs"]["skipped"] += 1
                continue
            rec_email = e_item.get("recipient_email") or ""
            subj = e_item.get("subject") or ""
            created_dt = _parse_iso_dt(e_item.get("created_at"))
            created_key = created_dt.strftime("%Y-%m-%d %H:%M:%S") if created_dt else ""

            e_key = (target_bid, rec_email, subj, created_key)
            if e_key in existing_email_keys:
                stats["email_logs"]["skipped"] += 1
                continue

            existing_email_keys.add(e_key)
            new_email = db_models.EmailLog(
                branch_id=target_bid,
                recipient_email=rec_email,
                subject=subj,
                message_content=e_item.get("message_content") or "",
                status=e_item.get("status") or "sent",
                delivery_report=e_item.get("delivery_report"),
                scheduled_for=_parse_iso_dt(e_item.get("scheduled_for")) or created_dt or datetime.utcnow(),
                sent_at=_parse_iso_dt(e_item.get("sent_at")),
            )
            if created_dt:
                new_email.created_at = created_dt
            db.add(new_email)
            stats["email_logs"]["imported"] += 1

        # 9. Restore Payment Transactions (Deduplicate by unique reference)
        existing_pay_refs = {
            p.reference for p in db.query(db_models.PaymentTransaction.reference).all()
        }
        for p_item in data.get("payment_transactions", []):
            ref = (p_item.get("reference") or "").strip()
            if not ref or ref in existing_pay_refs:
                stats["payment_transactions"]["skipped"] += 1
                continue

            existing_pay_refs.add(ref)
            resolved_comp_id = None
            c_slug = (p_item.get("company_slug") or "").strip().lower()
            if c_slug and c_slug in company_slug_map:
                resolved_comp_id = company_slug_map[c_slug].id
            elif p_item.get("company_id") in company_id_map:
                resolved_comp_id = company_id_map[p_item.get("company_id")]

            target_bid = resolve_branch_id(p_item)
            created_dt = _parse_iso_dt(p_item.get("created_at"))

            new_tx = db_models.PaymentTransaction(
                reference=ref,
                company_id=resolved_comp_id,
                branch_id=target_bid,
                amount=int(p_item.get("amount") or 0),
                currency=p_item.get("currency") or "GHS",
                customer_email=p_item.get("customer_email") or "",
                status=p_item.get("status") or "pending",
                channel=p_item.get("channel"),
                purpose=p_item.get("purpose") or "sms_credits",
                credits_purchased=p_item.get("credits_purchased"),
                paystack_response=p_item.get("paystack_response"),
                paid_at=_parse_iso_dt(p_item.get("paid_at")),
            )
            if created_dt:
                new_tx.created_at = created_dt
            db.add(new_tx)
            stats["payment_transactions"]["imported"] += 1

        db.commit()
        return {
            "status": "success",
            "mode": mode,
            "summary": stats,
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to restore system backup: {str(e)}")





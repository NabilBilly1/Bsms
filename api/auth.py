from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from core.database import get_db
from core.security import verify_pin, create_access_token, get_pin_hash
from models import models as db_models
from schemas import schemas as api_schemas
from datetime import timedelta
from core.config import settings

router = APIRouter()

@router.post("/login", response_model=api_schemas.Token)
def login(login_data: api_schemas.BranchLogin, db: Session = Depends(get_db)):
    try:
        print(f"Login attempt: company={login_data.company_slug}, branch_code={login_data.branch_code}")
        
        # Verify company and branch together
        branch = (
            db.query(db_models.Branch)
            .join(db_models.Company)
            .filter(
                db_models.Company.slug == login_data.company_slug,
                db_models.Branch.branch_code == login_data.branch_code
            ).first()
        )
        
        if not branch:
            print(f"Branch not found: {login_data.branch_code}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid branch code or PIN",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        is_pin_valid = verify_pin(login_data.pin, branch.pin)
        print(f"Branch found: {branch.name}, PIN valid: {is_pin_valid}")
        
        if not is_pin_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid branch code or PIN",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": str(branch.id)}, expires_delta=access_token_expires
        )
        
        return {
            "access_token": access_token, 
            "token_type": "bearer",
            "branch_id": branch.id,
            "branch_name": branch.name
        }
    except HTTPException as e:
        # Re-raise FastAPIs HTTPExceptions
        raise e
    except Exception as e:
        print(f"Login error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred during login: {str(e)}"
        )

@router.post("/setup-branch", response_model=api_schemas.Branch)
def setup_branch(branch_in: api_schemas.BranchCreate, db: Session = Depends(get_db)):
    # Check if branch exists
    db_branch = db.query(db_models.Branch).filter(db_models.Branch.branch_code == branch_in.branch_code).first()
    if db_branch:
        raise HTTPException(status_code=400, detail="Branch code already registered")
    
    # Check if company exists
    if not branch_in.company_id:
        raise HTTPException(status_code=400, detail="Company ID is required to setup a branch")
        
    company = db.query(db_models.Company).filter(db_models.Company.id == branch_in.company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    new_branch = db_models.Branch(
        name=branch_in.name,
        branch_code=branch_in.branch_code,
        pin=get_pin_hash(branch_in.pin),
        company_id=branch_in.company_id
    )
    db.add(new_branch)
    db.commit()
    db.refresh(new_branch)
    return new_branch

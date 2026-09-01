from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from core.config import settings
from core.database import get_db
from models import models as db_models
from schemas import schemas as api_schemas

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

def get_current_branch(db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        branch_id: str = payload.get("sub")
        if branch_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    branch = db.query(db_models.Branch).filter(db_models.Branch.id == int(branch_id)).first()
    if branch is None:
        raise credentials_exception
    return branch

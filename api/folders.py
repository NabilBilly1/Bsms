from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from core.database import get_db
from models import models as db_models
from schemas import schemas as api_schemas
from .deps import get_current_branch

router = APIRouter()


@router.get("/", response_model=List[api_schemas.FolderOut])
def list_folders(
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    """
    List all contact folders belonging to the current branch.
    """
    folders = (
        db.query(db_models.ContactFolder)
        .filter(db_models.ContactFolder.branch_id == current_branch.id)
        .order_by(db_models.ContactFolder.created_at.desc())
        .all()
    )

    result = []
    for f in folders:
        result.append(
            api_schemas.FolderOut(
                id=f.id,
                branch_id=f.branch_id,
                name=f.name,
                description=f.description,
                created_at=f.created_at,
                total_contacts=len(f.customers),
                customers=[
                    api_schemas.FolderCustomerOut(
                        id=c.id,
                        full_name=c.full_name,
                        phone_number=c.phone_number,
                        email=c.email,
                    )
                    for c in f.customers
                ],
            )
        )
    return result


@router.post("/", response_model=api_schemas.FolderOut, status_code=status.HTTP_201_CREATED)
def create_folder(
    folder_in: api_schemas.FolderCreate,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    """
    Create a new contact folder and add selected branch contacts to it.
    Requires at least 2 contacts.
    """
    if not folder_in.name or not folder_in.name.strip():
        raise HTTPException(status_code=400, detail="Folder name is required.")

    if not folder_in.customer_ids or len(folder_in.customer_ids) < 2:
        raise HTTPException(
            status_code=400,
            detail="Please select at least two contacts for this folder.",
        )

    # Verify that existing folder with same name does not already exist in branch
    existing = (
        db.query(db_models.ContactFolder)
        .filter(
            db_models.ContactFolder.branch_id == current_branch.id,
            func.lower(db_models.ContactFolder.name) == folder_in.name.strip().lower(),
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"A folder named '{folder_in.name.strip()}' already exists in your branch.",
        )

    # Fetch and validate branch customers
    customers = (
        db.query(db_models.Customer)
        .filter(
            db_models.Customer.branch_id == current_branch.id,
            db_models.Customer.id.in_(folder_in.customer_ids),
        )
        .all()
    )

    if len(customers) < 2:
        raise HTTPException(
            status_code=400,
            detail="At least two valid contacts from your branch must be selected.",
        )

    new_folder = db_models.ContactFolder(
        branch_id=current_branch.id,
        name=folder_in.name.strip(),
        description=folder_in.description.strip() if folder_in.description else None,
        customers=customers,
    )
    db.add(new_folder)
    db.commit()
    db.refresh(new_folder)

    return api_schemas.FolderOut(
        id=new_folder.id,
        branch_id=new_folder.branch_id,
        name=new_folder.name,
        description=new_folder.description,
        created_at=new_folder.created_at,
        total_contacts=len(new_folder.customers),
        customers=[
            api_schemas.FolderCustomerOut(
                id=c.id,
                full_name=c.full_name,
                phone_number=c.phone_number,
                email=c.email,
            )
            for c in new_folder.customers
        ],
    )


@router.get("/{folder_id}", response_model=api_schemas.FolderOut)
def get_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    """
    Get detailed information and contacts of a single folder.
    """
    folder = (
        db.query(db_models.ContactFolder)
        .filter(
            db_models.ContactFolder.id == folder_id,
            db_models.ContactFolder.branch_id == current_branch.id,
        )
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")

    return api_schemas.FolderOut(
        id=folder.id,
        branch_id=folder.branch_id,
        name=folder.name,
        description=folder.description,
        created_at=folder.created_at,
        total_contacts=len(folder.customers),
        customers=[
            api_schemas.FolderCustomerOut(
                id=c.id,
                full_name=c.full_name,
                phone_number=c.phone_number,
                email=c.email,
            )
            for c in folder.customers
        ],
    )


@router.put("/{folder_id}", response_model=api_schemas.FolderOut)
def update_folder(
    folder_id: int,
    folder_in: api_schemas.FolderUpdate,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    """
    Update folder metadata or customer membership.
    """
    folder = (
        db.query(db_models.ContactFolder)
        .filter(
            db_models.ContactFolder.id == folder_id,
            db_models.ContactFolder.branch_id == current_branch.id,
        )
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")

    if folder_in.name is not None:
        name_clean = folder_in.name.strip()
        if not name_clean:
            raise HTTPException(status_code=400, detail="Folder name cannot be empty.")
        # Check duplicate name
        existing = (
            db.query(db_models.ContactFolder)
            .filter(
                db_models.ContactFolder.branch_id == current_branch.id,
                db_models.ContactFolder.id != folder_id,
                func.lower(db_models.ContactFolder.name) == name_clean.lower(),
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Another folder named '{name_clean}' already exists.",
            )
        folder.name = name_clean

    if folder_in.description is not None:
        folder.description = folder_in.description.strip() if folder_in.description else None

    if folder_in.customer_ids is not None:
        if len(folder_in.customer_ids) < 2:
            raise HTTPException(
                status_code=400,
                detail="A folder must contain at least 2 contacts.",
            )
        customers = (
            db.query(db_models.Customer)
            .filter(
                db_models.Customer.branch_id == current_branch.id,
                db_models.Customer.id.in_(folder_in.customer_ids),
            )
            .all()
        )
        folder.customers = customers

    db.commit()
    db.refresh(folder)

    return api_schemas.FolderOut(
        id=folder.id,
        branch_id=folder.branch_id,
        name=folder.name,
        description=folder.description,
        created_at=folder.created_at,
        total_contacts=len(folder.customers),
        customers=[
            api_schemas.FolderCustomerOut(
                id=c.id,
                full_name=c.full_name,
                phone_number=c.phone_number,
                email=c.email,
            )
            for c in folder.customers
        ],
    )


@router.delete("/{folder_id}", status_code=status.HTTP_200_OK)
def delete_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    """
    Delete a contact folder. (This only removes the folder categorization, NOT the customer records).
    """
    folder = (
        db.query(db_models.ContactFolder)
        .filter(
            db_models.ContactFolder.id == folder_id,
            db_models.ContactFolder.branch_id == current_branch.id,
        )
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")

    db.delete(folder)
    db.commit()
    return {"message": "Folder deleted successfully", "folder_id": folder_id}


@router.post("/{folder_id}/contacts", response_model=api_schemas.FolderOut)
def add_contacts_to_folder(
    folder_id: int,
    customer_ids: List[int],
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    """
    Add additional contacts to an existing folder.
    """
    folder = (
        db.query(db_models.ContactFolder)
        .filter(
            db_models.ContactFolder.id == folder_id,
            db_models.ContactFolder.branch_id == current_branch.id,
        )
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")

    customers_to_add = (
        db.query(db_models.Customer)
        .filter(
            db_models.Customer.branch_id == current_branch.id,
            db_models.Customer.id.in_(customer_ids),
        )
        .all()
    )

    current_customer_ids = {c.id for c in folder.customers}
    for c in customers_to_add:
        if c.id not in current_customer_ids:
            folder.customers.append(c)

    db.commit()
    db.refresh(folder)

    return api_schemas.FolderOut(
        id=folder.id,
        branch_id=folder.branch_id,
        name=folder.name,
        description=folder.description,
        created_at=folder.created_at,
        total_contacts=len(folder.customers),
        customers=[
            api_schemas.FolderCustomerOut(
                id=c.id,
                full_name=c.full_name,
                phone_number=c.phone_number,
                email=c.email,
            )
            for c in folder.customers
        ],
    )


@router.delete("/{folder_id}/contacts/{customer_id}", response_model=api_schemas.FolderOut)
def remove_contact_from_folder(
    folder_id: int,
    customer_id: int,
    db: Session = Depends(get_db),
    current_branch: db_models.Branch = Depends(get_current_branch),
):
    """
    Remove a single contact from a folder.
    """
    folder = (
        db.query(db_models.ContactFolder)
        .filter(
            db_models.ContactFolder.id == folder_id,
            db_models.ContactFolder.branch_id == current_branch.id,
        )
        .first()
    )
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")

    folder.customers = [c for c in folder.customers if c.id != customer_id]
    db.commit()
    db.refresh(folder)

    return api_schemas.FolderOut(
        id=folder.id,
        branch_id=folder.branch_id,
        name=folder.name,
        description=folder.description,
        created_at=folder.created_at,
        total_contacts=len(folder.customers),
        customers=[
            api_schemas.FolderCustomerOut(
                id=c.id,
                full_name=c.full_name,
                phone_number=c.phone_number,
                email=c.email,
            )
            for c in folder.customers
        ],
    )

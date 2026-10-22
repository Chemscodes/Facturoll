from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Supplier, User
from app.schemas import SupplierCreate, SupplierOut, SupplierUpdate

router = APIRouter(prefix="/suppliers", tags=["suppliers"], dependencies=[Depends(get_current_user)])


def _supplier_query(db: Session, current_user: User):
    query = db.query(Supplier)
    if not current_user.is_admin:
        query = query.filter(
            Supplier.tenant_id == current_user.tenant_id,
        )
    return query


def _get_supplier(db: Session, supplier_id: int, current_user: User) -> Supplier:
    query = db.query(Supplier).filter(Supplier.id == supplier_id)
    if not current_user.is_admin:
        query = query.filter(
            Supplier.tenant_id == current_user.tenant_id,
        )
    supplier = query.first()
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    return supplier


@router.get("", response_model=list[SupplierOut])
def list_suppliers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[SupplierOut]:
    return _supplier_query(db, current_user).all()


@router.post("", response_model=SupplierOut)
def create_supplier(
    payload: SupplierCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SupplierOut:
    email = (payload.email or "").strip().lower()
    if email:
        existing = (
            db.query(Supplier)
            .filter(
                Supplier.tenant_id == current_user.tenant_id,
                Supplier.email.isnot(None),
                func.lower(Supplier.email) == email,
            )
            .first()
        )
        if existing:
            existing.name = payload.name
            existing.supplier_type = payload.supplier_type
            existing.phone = payload.phone
            existing.notes = payload.notes
            existing.active = payload.active
            db.commit()
            db.refresh(existing)
            return existing

    supplier = Supplier(
        **payload.dict(),
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.get("/{supplier_id}", response_model=SupplierOut)
def get_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SupplierOut:
    return _get_supplier(db, supplier_id, current_user)


@router.put("/{supplier_id}", response_model=SupplierOut)
def update_supplier(
    supplier_id: int,
    payload: SupplierUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SupplierOut:
    supplier = _get_supplier(db, supplier_id, current_user)
    for key, value in payload.dict(exclude_unset=True).items():
        setattr(supplier, key, value)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.delete("/{supplier_id}")
def delete_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    supplier = _get_supplier(db, supplier_id, current_user)
    supplier.active = False
    db.commit()
    return {"status": "ok"}

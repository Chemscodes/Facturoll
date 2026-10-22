import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Product, Supplier, SupplierType, User
from app.schemas import ImportError, ImportResult

router = APIRouter(prefix="/bulk", tags=["bulk"], dependencies=[Depends(get_current_user)])


def _parse_csv(file: UploadFile) -> list[dict[str, str]]:
    raw = file.file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be utf-8 encoded")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    for row in reader:
        normalized = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        rows.append(normalized)
    return rows


def _parse_bool(value: str) -> bool | None:
    if value == "":
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes", "y"}:
        return True
    if lowered in {"false", "0", "no", "n"}:
        return False
    return None


@router.post("/suppliers", response_model=ImportResult)
def import_suppliers(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ImportResult:
    rows = _parse_csv(file)
    errors: list[ImportError] = []
    inserted = 0

    for idx, row in enumerate(rows, start=2):
        name = row.get("name", "").strip()
        email = row.get("email", "").strip()
        supplier_type = row.get("supplier_type", "").strip().upper()
        supplier_category = row.get("supplier_category", "").strip()
        phone = row.get("phone", "").strip()
        notes = row.get("notes", "").strip()
        active_raw = row.get("active", "").strip()

        if not name:
            errors.append(ImportError(row=idx, error="Missing name"))
            continue
        if supplier_type not in {t.value for t in SupplierType}:
            errors.append(ImportError(row=idx, error="Invalid supplier_type"))
            continue
        active = _parse_bool(active_raw)
        existing = None
        if email:
            existing = (
                db.query(Supplier)
                .filter(
                    Supplier.tenant_id == current_user.tenant_id,
                    Supplier.email.isnot(None),
                    func.lower(Supplier.email) == email.lower(),
                )
                .first()
            )
        if existing:
            existing.name = name
            existing.supplier_type = SupplierType(supplier_type)
            existing.supplier_category = supplier_category or None
            existing.phone = phone or None
            existing.notes = notes or None
            existing.active = True if active is None else active
            continue

        supplier = Supplier(
            name=name,
            email=email,
            supplier_type=SupplierType(supplier_type),
            supplier_category=supplier_category or None,
            phone=phone or None,
            notes=notes or None,
            active=True if active is None else active,
            tenant_id=current_user.tenant_id,
            owner_user_id=current_user.id,
        )
        db.add(supplier)
        inserted += 1

    db.commit()
    return ImportResult(inserted=inserted, errors=errors)


@router.post("/products", response_model=ImportResult)
def import_products(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ImportResult:
    rows = _parse_csv(file)
    errors: list[ImportError] = []
    inserted = 0

    for idx, row in enumerate(rows, start=2):
        name = row.get("name", "").strip()
        sku = row.get("sku", "").strip()
        unit = row.get("unit", "").strip()

        if not name:
            errors.append(ImportError(row=idx, error="Missing name"))
            continue

        product = Product(
            name=name,
            sku=sku or None,
            unit=unit or None,
            tenant_id=current_user.tenant_id,
            owner_user_id=current_user.id,
        )
        db.add(product)
        inserted += 1

    db.commit()
    return ImportResult(inserted=inserted, errors=errors)

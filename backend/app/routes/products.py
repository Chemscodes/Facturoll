from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Product, User
from app.schemas import ProductCreate, ProductOut, ProductUpdate

router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(get_current_user)])


def _product_query(db: Session, current_user: User):
    query = db.query(Product)
    if not current_user.is_admin:
        query = query.filter(
            Product.tenant_id == current_user.tenant_id,
        )
    return query


def _get_product(db: Session, product_id: int, current_user: User) -> Product:
    query = db.query(Product).filter(Product.id == product_id)
    if not current_user.is_admin:
        query = query.filter(
            Product.tenant_id == current_user.tenant_id,
        )
    product = query.first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.get("", response_model=list[ProductOut])
def list_products(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ProductOut]:
    return _product_query(db, current_user).all()


@router.post("", response_model=ProductOut)
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProductOut:
    product = Product(
        **payload.dict(),
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@router.get("/{product_id}", response_model=ProductOut)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProductOut:
    return _get_product(db, product_id, current_user)


@router.put("/{product_id}", response_model=ProductOut)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProductOut:
    product = _get_product(db, product_id, current_user)
    for key, value in payload.dict(exclude_unset=True).items():
        setattr(product, key, value)
    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}")
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    product = _get_product(db, product_id, current_user)
    db.delete(product)
    db.commit()
    return {"status": "ok"}

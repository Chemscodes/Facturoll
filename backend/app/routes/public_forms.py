from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CampaignItem, CampaignRecipient, Response, ResponseItem, Supplier

router = APIRouter(tags=["public"])

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _get_recipient(db: Session, token: str) -> CampaignRecipient:
    recipient = db.query(CampaignRecipient).filter(CampaignRecipient.token == token).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="Invalid token")
    if recipient.token_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Token expired")
    return recipient


@router.get("/r/{token}", response_class=HTMLResponse)
def render_reply_form(token: str, request: Request, db: Session = Depends(get_db)):
    recipient = _get_recipient(db, token)
    supplier = (
        db.query(Supplier)
        .filter(Supplier.id == recipient.supplier_id, Supplier.tenant_id == recipient.tenant_id)
        .first()
    )
    items = (
        db.query(CampaignItem)
        .filter(
            CampaignItem.campaign_id == recipient.campaign_id,
            CampaignItem.tenant_id == recipient.tenant_id,
        )
        .all()
    )
    return templates.TemplateResponse(
        "reply_form.html",
        {
            "request": request,
            "supplier": supplier,
            "campaign_id": recipient.campaign_id,
            "token": token,
            "items": items,
        },
    )


@router.post("/r/{token}", response_class=HTMLResponse)
async def submit_reply_form(token: str, request: Request, db: Session = Depends(get_db)):
    recipient = _get_recipient(db, token)
    form = await request.form()
    responder_email = form.get("email", "").strip()
    if not responder_email:
        raise HTTPException(status_code=400, detail="Email is required")

    response = (
        db.query(Response)
        .filter(
            Response.campaign_id == recipient.campaign_id,
            Response.supplier_id == recipient.supplier_id,
            Response.tenant_id == recipient.tenant_id,
        )
        .first()
    )

    if response:
        response.submitted_at = datetime.utcnow()
        response.responder_email = responder_email
        response.ip = request.client.host if request.client else None
        db.query(ResponseItem).filter(
            ResponseItem.response_id == response.id,
            ResponseItem.tenant_id == recipient.tenant_id,
        ).delete()
    else:
        response = Response(
            campaign_id=recipient.campaign_id,
            supplier_id=recipient.supplier_id,
            responder_email=responder_email,
            ip=request.client.host if request.client else None,
            tenant_id=recipient.tenant_id,
        )
        db.add(response)
        db.flush()

    items = (
        db.query(CampaignItem)
        .filter(
            CampaignItem.campaign_id == recipient.campaign_id,
            CampaignItem.tenant_id == recipient.tenant_id,
        )
        .all()
    )

    for item in items:
        qty_raw = form.get(f"qty_{item.product_id}", "").strip()
        price_raw = form.get(f"price_{item.product_id}", "").strip()
        conform_raw = form.get(f"conform_{item.product_id}", "").strip().lower()
        alt_name = form.get(f"alt_{item.product_id}", "").strip()
        if not qty_raw and not price_raw and not conform_raw and not alt_name:
            continue
        if conform_raw not in {"yes", "no"}:
            raise HTTPException(status_code=400, detail="Please select if the product is conforming")
        if conform_raw == "no" and not alt_name:
            raise HTTPException(status_code=400, detail="Similar product name is required")
        try:
            qty = float(qty_raw) if qty_raw else 0.0
            price = float(price_raw) if price_raw else 0.0
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid numeric value")
        notes = None
        if conform_raw == "yes":
            notes = "Conforme"
        elif alt_name:
            notes = f"Non conforme: {alt_name}"
        response_item = ResponseItem(
            response_id=response.id,
            product_id=item.product_id,
            offered_qty=qty,
            unit_price=price,
            currency="DZD",
            notes=notes if notes else None,
            tenant_id=recipient.tenant_id,
        )
        db.add(response_item)

    db.commit()

    return templates.TemplateResponse(
        "success.html",
        {"request": request, "supplier": recipient.supplier_id},
    )

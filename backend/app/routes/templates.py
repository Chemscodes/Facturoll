from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import EmailTemplate, User
from app.schemas import EmailTemplateCreate, EmailTemplateOut, EmailTemplateUpdate

router = APIRouter(prefix="/templates", tags=["templates"], dependencies=[Depends(get_current_user)])


def _template_query(db: Session, current_user: User):
    query = db.query(EmailTemplate).filter(EmailTemplate.tenant_id == current_user.tenant_id)
    if not current_user.is_admin:
        query = query.filter(EmailTemplate.owner_user_id == current_user.id)
    return query


def _get_template(db: Session, template_id: int, current_user: User) -> EmailTemplate:
    query = db.query(EmailTemplate).filter(
        EmailTemplate.id == template_id,
        EmailTemplate.tenant_id == current_user.tenant_id,
    )
    if not current_user.is_admin:
        query = query.filter(EmailTemplate.owner_user_id == current_user.id)
    template = query.first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.get("", response_model=list[EmailTemplateOut])
def list_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[EmailTemplateOut]:
    return _template_query(db, current_user).order_by(EmailTemplate.created_at.desc()).all()


@router.post("", response_model=EmailTemplateOut)
def create_template(
    payload: EmailTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EmailTemplateOut:
    name = payload.name.strip()
    subject = payload.subject.strip()
    body_html = payload.body_html.strip()
    if not name or not subject or not body_html:
        raise HTTPException(status_code=400, detail="Name, subject, and body are required")
    template = EmailTemplate(
        name=name,
        subject=subject,
        body_html=body_html,
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.get("/{template_id}", response_model=EmailTemplateOut)
def get_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EmailTemplateOut:
    return _get_template(db, template_id, current_user)


@router.put("/{template_id}", response_model=EmailTemplateOut)
def update_template(
    template_id: int,
    payload: EmailTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EmailTemplateOut:
    template = _get_template(db, template_id, current_user)
    for key, value in payload.dict(exclude_unset=True).items():
        if isinstance(value, str):
            value = value.strip()
        setattr(template, key, value)
    db.commit()
    db.refresh(template)
    return template


@router.delete("/{template_id}")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    template = _get_template(db, template_id, current_user)
    db.delete(template)
    db.commit()
    return {"status": "ok"}

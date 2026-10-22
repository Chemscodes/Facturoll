from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Group, GroupMember, Supplier, User
from app.schemas import GroupCreate, GroupMembersUpdate, GroupOut, GroupUpdate, GroupMemberOut

router = APIRouter(prefix="/groups", tags=["groups"], dependencies=[Depends(get_current_user)])


def _get_group(db: Session, group_id: int, current_user: User) -> Group:
    query = db.query(Group).filter(Group.id == group_id)
    if not current_user.is_admin:
        query = query.filter(
            Group.tenant_id == current_user.tenant_id,
        )
    group = query.first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@router.get("", response_model=list[GroupOut])
def list_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[GroupOut]:
    query = db.query(Group)
    if not current_user.is_admin:
        query = query.filter(
            Group.tenant_id == current_user.tenant_id,
        )
    return query.all()


@router.post("", response_model=GroupOut)
def create_group(
    payload: GroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GroupOut:
    group = Group(
        **payload.dict(),
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


@router.get("/{group_id}", response_model=GroupOut)
def get_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GroupOut:
    return _get_group(db, group_id, current_user)


@router.put("/{group_id}", response_model=GroupOut)
def update_group(
    group_id: int,
    payload: GroupUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GroupOut:
    group = _get_group(db, group_id, current_user)
    for key, value in payload.dict(exclude_unset=True).items():
        setattr(group, key, value)
    db.commit()
    db.refresh(group)
    return group


@router.delete("/{group_id}")
def delete_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    group = _get_group(db, group_id, current_user)
    db.delete(group)
    db.commit()
    return {"status": "ok"}


@router.post("/{group_id}/members", response_model=list[GroupMemberOut])
def replace_members(
    group_id: int,
    payload: GroupMembersUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[GroupMemberOut]:
    group = _get_group(db, group_id, current_user)
    tenant_id = group.tenant_id

    if payload.supplier_ids:
        supplier_rows = (
            db.query(Supplier.id)
            .filter(
                Supplier.id.in_(payload.supplier_ids),
                Supplier.tenant_id == tenant_id,
            )
            .all()
        )
        found = {row[0] for row in supplier_rows}
        missing = set(payload.supplier_ids) - found
        if missing:
            raise HTTPException(status_code=404, detail="Supplier not found")

    db.query(GroupMember).filter(
        GroupMember.group_id == group_id,
        GroupMember.tenant_id == tenant_id,
    ).delete()
    db.flush()

    members = []
    for supplier_id in payload.supplier_ids:
        member = GroupMember(group_id=group_id, supplier_id=supplier_id, tenant_id=tenant_id)
        db.add(member)
        members.append(member)

    db.commit()
    return members


@router.get("/{group_id}/members", response_model=list[GroupMemberOut])
def list_members(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[GroupMemberOut]:
    group = _get_group(db, group_id, current_user)
    return (
        db.query(GroupMember)
        .filter(GroupMember.group_id == group_id, GroupMember.tenant_id == group.tenant_id)
        .all()
    )

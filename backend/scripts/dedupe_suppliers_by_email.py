from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys

from sqlalchemy import func
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db import SessionLocal
from app.models import (
    CampaignRecipient,
    DeliveryStatus,
    EmailJob,
    GroupMember,
    InvoiceExtractionItem,
    Response,
    Supplier,
    TechSheet,
    TechSheetLink,
)


@dataclass
class MergeStats:
    groups_processed: int = 0
    suppliers_deleted: int = 0
    suppliers_merged: int = 0
    group_members_updated: int = 0
    group_members_deleted: int = 0
    campaign_recipients_updated: int = 0
    campaign_recipients_deleted: int = 0
    email_jobs_supplier_updated: int = 0
    email_jobs_recipient_updated: int = 0
    responses_updated: int = 0
    tech_sheets_updated: int = 0
    tech_sheet_links_updated: int = 0
    invoice_items_updated: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "groups_processed": self.groups_processed,
            "suppliers_deleted": self.suppliers_deleted,
            "suppliers_merged": self.suppliers_merged,
            "group_members_updated": self.group_members_updated,
            "group_members_deleted": self.group_members_deleted,
            "campaign_recipients_updated": self.campaign_recipients_updated,
            "campaign_recipients_deleted": self.campaign_recipients_deleted,
            "email_jobs_supplier_updated": self.email_jobs_supplier_updated,
            "email_jobs_recipient_updated": self.email_jobs_recipient_updated,
            "responses_updated": self.responses_updated,
            "tech_sheets_updated": self.tech_sheets_updated,
            "tech_sheet_links_updated": self.tech_sheet_links_updated,
            "invoice_items_updated": self.invoice_items_updated,
        }


def _supplier_sort_key(supplier: Supplier) -> tuple:
    created = supplier.created_at or datetime.max
    return (0 if supplier.active else 1, created, supplier.id)


def _delivery_rank(status: DeliveryStatus) -> int:
    if status == DeliveryStatus.SENT:
        return 3
    if status == DeliveryStatus.PENDING:
        return 2
    return 1


def _merge_supplier_fields(primary: Supplier, duplicate: Supplier) -> None:
    if not primary.name and duplicate.name:
        primary.name = duplicate.name
    if not primary.phone and duplicate.phone:
        primary.phone = duplicate.phone
    if not primary.notes and duplicate.notes:
        primary.notes = duplicate.notes
    if primary.supplier_type != duplicate.supplier_type and duplicate.active:
        primary.supplier_type = duplicate.supplier_type
    if not primary.active and duplicate.active:
        primary.active = True


def _merge_recipient_fields(primary: CampaignRecipient, duplicate: CampaignRecipient) -> None:
    primary_rank = _delivery_rank(primary.delivery_status)
    duplicate_rank = _delivery_rank(duplicate.delivery_status)
    if duplicate_rank > primary_rank:
        primary.delivery_status = duplicate.delivery_status

    if duplicate.sent_at and (not primary.sent_at or duplicate.sent_at > primary.sent_at):
        primary.sent_at = duplicate.sent_at

    if duplicate.token_expires_at and (
        not primary.token_expires_at or duplicate.token_expires_at > primary.token_expires_at
    ):
        primary.token_expires_at = duplicate.token_expires_at

    if (not primary.last_error) and duplicate.last_error:
        primary.last_error = duplicate.last_error


def _normalized_email_expression():
    return func.lower(func.trim(Supplier.email))


def _list_duplicate_groups(db: Session, tenant_id: int | None) -> list[tuple[int, str, int]]:
    norm_email = _normalized_email_expression()
    query = (
        db.query(
            Supplier.tenant_id,
            norm_email.label("email_norm"),
            func.count(Supplier.id).label("count"),
        )
        .filter(
            Supplier.email.isnot(None),
            func.trim(Supplier.email) != "",
        )
        .group_by(Supplier.tenant_id, norm_email)
        .having(func.count(Supplier.id) > 1)
        .order_by(Supplier.tenant_id.asc(), norm_email.asc())
    )
    if tenant_id is not None:
        query = query.filter(Supplier.tenant_id == tenant_id)
    return [(int(t), str(e), int(c)) for t, e, c in query.all()]


def _suppliers_by_email(db: Session, tenant_id: int, email_norm: str) -> list[Supplier]:
    norm_email = _normalized_email_expression()
    return (
        db.query(Supplier)
        .filter(
            Supplier.tenant_id == tenant_id,
            norm_email == email_norm,
        )
        .order_by(Supplier.id.asc())
        .all()
    )


def _groupmember_merge(db: Session, tenant_id: int, from_supplier_id: int, to_supplier_id: int, stats: MergeStats) -> None:
    rows = (
        db.query(GroupMember)
        .filter(
            GroupMember.tenant_id == tenant_id,
            GroupMember.supplier_id == from_supplier_id,
        )
        .all()
    )
    for row in rows:
        exists = (
            db.query(GroupMember.id)
            .filter(
                GroupMember.tenant_id == tenant_id,
                GroupMember.group_id == row.group_id,
                GroupMember.supplier_id == to_supplier_id,
            )
            .first()
        )
        if exists:
            db.delete(row)
            stats.group_members_deleted += 1
        else:
            row.supplier_id = to_supplier_id
            stats.group_members_updated += 1


def _campaign_recipients_merge(
    db: Session, tenant_id: int, from_supplier_id: int, to_supplier_id: int, stats: MergeStats
) -> None:
    db.flush()
    targets = (
        db.query(CampaignRecipient)
        .filter(
            CampaignRecipient.tenant_id == tenant_id,
            CampaignRecipient.supplier_id == to_supplier_id,
        )
        .all()
    )
    target_by_campaign: dict[int, CampaignRecipient] = {
        int(row.campaign_id): row for row in targets
    }

    rows = (
        db.query(CampaignRecipient)
        .filter(
            CampaignRecipient.tenant_id == tenant_id,
            CampaignRecipient.supplier_id == from_supplier_id,
        )
        .all()
    )
    for row in rows:
        target = target_by_campaign.get(int(row.campaign_id))
        if target is None:
            row.supplier_id = to_supplier_id
            target_by_campaign[int(row.campaign_id)] = row
            stats.campaign_recipients_updated += 1
            continue

        if target.id == row.id:
            continue

        _merge_recipient_fields(target, row)
        moved_jobs = (
            db.query(EmailJob)
            .filter(
                EmailJob.tenant_id == tenant_id,
                EmailJob.recipient_id == row.id,
            )
            .update({EmailJob.recipient_id: target.id}, synchronize_session=False)
        )
        stats.email_jobs_recipient_updated += int(moved_jobs or 0)
        db.delete(row)
        stats.campaign_recipients_deleted += 1
    db.flush()


def _merge_supplier_references(
    db: Session, tenant_id: int, from_supplier_id: int, to_supplier_id: int, stats: MergeStats
) -> None:
    _groupmember_merge(db, tenant_id, from_supplier_id, to_supplier_id, stats)
    _campaign_recipients_merge(db, tenant_id, from_supplier_id, to_supplier_id, stats)

    stats.email_jobs_supplier_updated += int(
        db.query(EmailJob)
        .filter(EmailJob.tenant_id == tenant_id, EmailJob.supplier_id == from_supplier_id)
        .update({EmailJob.supplier_id: to_supplier_id}, synchronize_session=False)
        or 0
    )
    stats.responses_updated += int(
        db.query(Response)
        .filter(Response.tenant_id == tenant_id, Response.supplier_id == from_supplier_id)
        .update({Response.supplier_id: to_supplier_id}, synchronize_session=False)
        or 0
    )
    stats.tech_sheets_updated += int(
        db.query(TechSheet)
        .filter(TechSheet.tenant_id == tenant_id, TechSheet.supplier_id == from_supplier_id)
        .update({TechSheet.supplier_id: to_supplier_id}, synchronize_session=False)
        or 0
    )
    stats.tech_sheet_links_updated += int(
        db.query(TechSheetLink)
        .filter(TechSheetLink.tenant_id == tenant_id, TechSheetLink.supplier_id == from_supplier_id)
        .update({TechSheetLink.supplier_id: to_supplier_id}, synchronize_session=False)
        or 0
    )
    stats.invoice_items_updated += int(
        db.query(InvoiceExtractionItem)
        .filter(
            InvoiceExtractionItem.tenant_id == tenant_id,
            InvoiceExtractionItem.supplier_id == from_supplier_id,
        )
        .update({InvoiceExtractionItem.supplier_id: to_supplier_id}, synchronize_session=False)
        or 0
    )


def _reference_counts(db: Session, tenant_id: int, supplier_ids: list[int]) -> dict[str, int]:
    if not supplier_ids:
        return {}
    data: dict[str, int] = defaultdict(int)
    data["group_members"] = int(
        db.query(GroupMember.id)
        .filter(GroupMember.tenant_id == tenant_id, GroupMember.supplier_id.in_(supplier_ids))
        .count()
    )
    data["campaign_recipients"] = int(
        db.query(CampaignRecipient.id)
        .filter(CampaignRecipient.tenant_id == tenant_id, CampaignRecipient.supplier_id.in_(supplier_ids))
        .count()
    )
    data["email_jobs"] = int(
        db.query(EmailJob.id)
        .filter(EmailJob.tenant_id == tenant_id, EmailJob.supplier_id.in_(supplier_ids))
        .count()
    )
    data["responses"] = int(
        db.query(Response.id)
        .filter(Response.tenant_id == tenant_id, Response.supplier_id.in_(supplier_ids))
        .count()
    )
    data["tech_sheets"] = int(
        db.query(TechSheet.id)
        .filter(TechSheet.tenant_id == tenant_id, TechSheet.supplier_id.in_(supplier_ids))
        .count()
    )
    data["tech_sheet_links"] = int(
        db.query(TechSheetLink.id)
        .filter(TechSheetLink.tenant_id == tenant_id, TechSheetLink.supplier_id.in_(supplier_ids))
        .count()
    )
    data["invoice_items"] = int(
        db.query(InvoiceExtractionItem.id)
        .filter(
            InvoiceExtractionItem.tenant_id == tenant_id,
            InvoiceExtractionItem.supplier_id.in_(supplier_ids),
        )
        .count()
    )
    return dict(data)


def run(tenant_id: int | None, apply: bool) -> int:
    stats = MergeStats()
    db = SessionLocal()
    try:
        groups = _list_duplicate_groups(db, tenant_id=tenant_id)
        if not groups:
            print("No duplicate supplier emails found.")
            return 0

        print(f"Duplicate groups found: {len(groups)}")
        for group_tenant_id, email_norm, count in groups:
            suppliers = _suppliers_by_email(db, group_tenant_id, email_norm)
            suppliers_sorted = sorted(suppliers, key=_supplier_sort_key)
            primary = suppliers_sorted[0]
            duplicates = suppliers_sorted[1:]
            duplicate_ids = [s.id for s in duplicates]

            refs = _reference_counts(db, group_tenant_id, duplicate_ids)
            print(
                f"[tenant={group_tenant_id}] email={email_norm} "
                f"primary={primary.id} duplicates={duplicate_ids} refs={refs}"
            )

            stats.groups_processed += 1
            if not apply:
                continue

            for duplicate in duplicates:
                _merge_supplier_fields(primary, duplicate)
                _merge_supplier_references(
                    db,
                    tenant_id=group_tenant_id,
                    from_supplier_id=duplicate.id,
                    to_supplier_id=primary.id,
                    stats=stats,
                )
                db.delete(duplicate)
                stats.suppliers_deleted += 1
                stats.suppliers_merged += 1
                db.flush()

            db.commit()
            db.refresh(primary)
            print(f"  -> merged into supplier {primary.id}")

        print("Summary:", stats.to_dict())
        if not apply:
            print("Dry-run only. Re-run with --apply to execute.")
        return 0
    except Exception as exc:
        db.rollback()
        print("ERROR:", exc)
        return 1
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deduplicate suppliers by normalized email within each tenant."
    )
    parser.add_argument("--tenant-id", type=int, default=None, help="Limit to one tenant.")
    parser.add_argument("--apply", action="store_true", help="Execute changes (default is dry-run).")
    args = parser.parse_args()
    raise SystemExit(run(tenant_id=args.tenant_id, apply=args.apply))


if __name__ == "__main__":
    main()

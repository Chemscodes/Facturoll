import os
import shutil
import subprocess
import json
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import config
from app.auth import get_password_hash, require_admin
from app.db import get_db
from app.models import (
    Campaign,
    CampaignAttachment,
    CampaignRecipient,
    CampaignStatus,
    DeliveryStatus,
    EmailJob,
    EmailJobStatus,
    InboundInvoice,
    InvoiceExtractionItem,
    OfferAnalysisResult,
    OpenAIPayloadCache,
    OpenAIUsageLog,
    Response,
    SuppressedEmail,
    Tenant,
    TechSheet,
    User,
)
from app.schemas import (
    AdminUserCreate,
    AdminUserOut,
    AdminUserStats,
    EmailQueueStats,
    SchedulerStatus,
    TenantCreate,
    TenantOut,
    TenantStats,
)
from app.settings_service import ensure_defaults

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

BACKEND_DIR = Path(__file__).resolve().parents[2]
INVOICES_DIR = BACKEND_DIR / "invoices"
UNMATCHED_INDEX_PATH = BACKEND_DIR / "reports" / "gmail_unmatched_index.jsonl"

WINDOWS_SCHEDULED_TASK_NAME = (os.getenv("GMAIL_SYNC_TASK_NAME") or "SAAS Gmail Sync").strip()
SYSTEMD_TIMER_UNIT = (os.getenv("GMAIL_SYNC_TIMER_UNIT") or "saas-gmail-sync.timer").strip()
SYSTEMD_SERVICE_UNIT = (os.getenv("GMAIL_SYNC_SERVICE_UNIT") or "saas-gmail-sync.service").strip()


def _normalize_file_key(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except Exception:
        resolved = path
    return str(resolved).lower()


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


def _collect_old_files(root: Path, cutoff: datetime) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            mtime = datetime.utcfromtimestamp(path.stat().st_mtime)
        except Exception:
            continue
        if mtime < cutoff:
            candidates.append(path)
    return candidates


def _sum_file_sizes(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += int(path.stat().st_size)
        except Exception:
            continue
    return total


def _delete_files(paths: list[Path]) -> tuple[int, int]:
    deleted = 0
    failed = 0
    for path in paths:
        try:
            path.unlink()
            deleted += 1
        except Exception:
            failed += 1
    return deleted, failed


def _pdf_pages(file_path: str, cache: dict[str, int | None]) -> int | None:
    if file_path in cache:
        return cache[file_path]
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        cache[file_path] = None
        return None
    try:
        path = Path(file_path)
        if not path.exists():
            cache[file_path] = None
            return None
        reader = PdfReader(str(path))
        pages = len(reader.pages)
    except Exception:
        pages = None
    cache[file_path] = pages
    return pages


def _parse_schtasks_list(output: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip().lower()] = value.strip()
    return data


def _parse_systemctl_show(output: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"Command not found: {args[0]}")


def _read_gmail_unmatched_state() -> dict[str, dict]:
    state: dict[str, dict] = {}
    if not UNMATCHED_INDEX_PATH.exists():
        return state
    for line in UNMATCHED_INDEX_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        key = str(payload.get("attachment_key") or "").strip()
        if not key:
            continue
        state[key] = payload
    return state


def _append_gmail_unmatched_event(payload: dict[str, object]) -> None:
    UNMATCHED_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with UNMATCHED_INDEX_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _list_open_gmail_unmatched_records(*, tenant_id: int | None, limit: int) -> list[dict]:
    state = _read_gmail_unmatched_state()
    rows: list[dict] = []
    for payload in state.values():
        status = str(payload.get("status") or "unmatched").strip().lower()
        if status != "unmatched":
            continue
        row_tenant_id = payload.get("tenant_id")
        if tenant_id is not None and row_tenant_id is not None:
            try:
                if int(row_tenant_id) != int(tenant_id):
                    continue
            except Exception:
                continue
        file_path = str(payload.get("file") or "").strip()
        if file_path and not Path(file_path).exists():
            continue
        rows.append(payload)
    rows.sort(
        key=lambda row: (
            str(row.get("date") or ""),
            str(row.get("attachment_key") or ""),
        ),
        reverse=True,
    )
    return rows[: max(1, min(limit, 500))]


def _scheduler_backend() -> str:
    if os.name == "nt":
        if shutil.which("schtasks"):
            return "windows"
        raise HTTPException(status_code=500, detail="Windows scheduler command 'schtasks' not found.")
    if shutil.which("systemctl"):
        return "systemd"
    if shutil.which("schtasks"):
        return "windows"
    raise HTTPException(status_code=500, detail="No supported scheduler backend found.")


@router.get("/gmail-unmatched")
def list_gmail_unmatched(
    tenant_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[dict]:
    return _list_open_gmail_unmatched_records(tenant_id=tenant_id, limit=limit)


@router.get("/campaigns")
def list_admin_campaigns(
    tenant_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = db.query(Campaign)
    if tenant_id is not None:
        query = query.filter(Campaign.tenant_id == tenant_id)
    rows = query.order_by(Campaign.created_at.desc(), Campaign.id.desc()).limit(limit).all()
    return [
        {
            "id": int(row.id),
            "tenant_id": int(row.tenant_id),
            "title": row.title,
            "status": row.status.value if row.status is not None else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.post("/gmail-unmatched/attach")
def attach_gmail_unmatched_to_campaign(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
) -> dict:
    attachment_key = str(payload.get("attachment_key") or "").strip()
    if not attachment_key:
        raise HTTPException(status_code=400, detail="attachment_key is required")
    try:
        campaign_id = int(payload.get("campaign_id"))
    except Exception:
        raise HTTPException(status_code=400, detail="campaign_id is required")

    state = _read_gmail_unmatched_state()
    record = state.get(attachment_key)
    if not isinstance(record, dict) or str(record.get("status") or "unmatched").strip().lower() != "unmatched":
        raise HTTPException(status_code=404, detail="Unmatched email record not found")

    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    source_path = Path(str(record.get("file") or "").strip())
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source attachment file not found")

    sender_raw = str(record.get("sender") or "").strip()
    sender_email = str(record.get("sender_email") or "").strip() or sender_raw or "unknown"
    sender_name = str(record.get("sender") or "").strip()
    subject = str(record.get("subject") or "").strip() or None
    message_id = str(record.get("message_id") or "").strip() or None
    target_dir = INVOICES_DIR / f"CAMP-{campaign.id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / source_path.name
    if target_path.resolve() != source_path.resolve():
        if target_path.exists():
            stem = target_path.stem
            suffix = target_path.suffix
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            target_path = target_dir / f"{stem}_{ts}{suffix}"
        shutil.move(str(source_path), str(target_path))

    invoice_exists = (
        db.query(InboundInvoice)
        .filter(
            InboundInvoice.tenant_id == campaign.tenant_id,
            InboundInvoice.campaign_id == campaign.id,
            InboundInvoice.message_id == message_id,
            InboundInvoice.file_name == target_path.name,
        )
        .first()
    )
    if invoice_exists is not None:
        invoice = invoice_exists
    else:
        invoice = InboundInvoice(
            tenant_id=campaign.tenant_id,
            campaign_id=campaign.id,
            sender_email=sender_email,
            sender_name=sender_name,
            subject=subject,
            message_id=message_id,
            file_name=target_path.name,
            file_path=str(target_path),
        )
        db.add(invoice)
        db.commit()
        db.refresh(invoice)

    from scripts.gmail_fetch import _resolve_pipeline_user, _run_invoice_pipeline

    current_user = _resolve_pipeline_user(db, campaign)
    items = _run_invoice_pipeline(
        invoice=invoice,
        db=db,
        current_user=current_user,
    )

    _append_gmail_unmatched_event(
        {
            "attachment_key": attachment_key,
            "status": "attached",
            "campaign_id": campaign.id,
            "invoice_id": invoice.id,
            "resolved_at": datetime.utcnow().isoformat(),
            "file": str(target_path),
        }
    )
    return {
        "status": "ok",
        "campaign_id": campaign.id,
        "invoice_id": invoice.id,
        "items_count": len(items or []),
        "file": str(target_path),
    }


def _query_task_status_windows() -> SchedulerStatus:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", WINDOWS_SCHEDULED_TASK_NAME, "/FO", "LIST", "/V"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Task not found"
        raise HTTPException(status_code=404, detail=message)

    data = _parse_schtasks_list(result.stdout)
    status = data.get("status")
    enabled = status is not None and status.lower() != "disabled"
    return SchedulerStatus(
        task_name=WINDOWS_SCHEDULED_TASK_NAME,
        enabled=enabled,
        status=status,
        last_run_time=data.get("last run time"),
        next_run_time=data.get("next run time"),
    )


def _set_task_enabled_windows(enable: bool) -> SchedulerStatus:
    flag = "/ENABLE" if enable else "/DISABLE"
    result = subprocess.run(
        ["schtasks", "/Change", "/TN", WINDOWS_SCHEDULED_TASK_NAME, flag],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Failed to update task"
        raise HTTPException(status_code=400, detail=message)
    return _query_task_status_windows()


def _systemd_show(unit_name: str, properties: list[str]) -> dict[str, str]:
    args = ["systemctl", "show", unit_name]
    for prop in properties:
        args.extend(["--property", prop])
    result = _run_command(args)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"Failed to query {unit_name}"
        message_low = message.lower()
        if "not found" in message_low or "could not be found" in message_low:
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=500, detail=message)
    return _parse_systemctl_show(result.stdout)


def _clean_systemd_time(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or text.lower() in {"n/a", "0"}:
        return None
    return text


def _query_task_status_systemd() -> SchedulerStatus:
    timer = _systemd_show(
        SYSTEMD_TIMER_UNIT,
        [
            "ActiveState",
            "SubState",
            "UnitFileState",
            "Result",
            "LastTriggerUSec",
            "NextElapseUSecRealtime",
        ],
    )
    service = _systemd_show(
        SYSTEMD_SERVICE_UNIT,
        [
            "ActiveState",
            "SubState",
            "Result",
        ],
    )

    unit_file_state = (timer.get("UnitFileState") or "").lower()
    enabled = unit_file_state not in {"disabled", "masked", "bad"}
    status_parts = [
        f"timer={timer.get('ActiveState', 'unknown')}/{timer.get('SubState', 'unknown')}",
        f"service={service.get('ActiveState', 'unknown')}/{service.get('SubState', 'unknown')}",
    ]
    if timer.get("Result"):
        status_parts.append(f"timer_result={timer['Result']}")
    if service.get("Result"):
        status_parts.append(f"service_result={service['Result']}")

    return SchedulerStatus(
        task_name=SYSTEMD_TIMER_UNIT,
        enabled=enabled,
        status=" ".join(status_parts),
        last_run_time=_clean_systemd_time(timer.get("LastTriggerUSec")),
        next_run_time=_clean_systemd_time(timer.get("NextElapseUSecRealtime")),
    )


def _set_task_enabled_systemd(enable: bool) -> SchedulerStatus:
    args = ["systemctl", "enable" if enable else "disable", "--now", SYSTEMD_TIMER_UNIT]
    result = _run_command(args)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Failed to update scheduler"
        raise HTTPException(status_code=400, detail=message)
    return _query_task_status_systemd()


def _query_task_status() -> SchedulerStatus:
    backend = _scheduler_backend()
    if backend == "systemd":
        return _query_task_status_systemd()
    return _query_task_status_windows()


def _set_task_enabled(enable: bool) -> SchedulerStatus:
    backend = _scheduler_backend()
    if backend == "systemd":
        return _set_task_enabled_systemd(enable)
    return _set_task_enabled_windows(enable)


def _tenant_stats_for(db: Session, tenant_ids: set[int]) -> dict[int, TenantStats]:
    if not tenant_ids:
        return {}

    user_counts = dict(
        db.query(User.tenant_id, func.count(User.id))
        .filter(User.tenant_id.in_(tenant_ids))
        .group_by(User.tenant_id)
        .all()
    )
    campaign_counts = dict(
        db.query(Campaign.tenant_id, func.count(Campaign.id))
        .filter(
            Campaign.tenant_id.in_(tenant_ids),
            Campaign.status == CampaignStatus.SENT,
        )
        .group_by(Campaign.tenant_id)
        .all()
    )
    response_counts = dict(
        db.query(Response.tenant_id, func.count(Response.id))
        .filter(Response.tenant_id.in_(tenant_ids))
        .group_by(Response.tenant_id)
        .all()
    )

    return {
        tenant_id: TenantStats(
            tenant_id=tenant_id,
            tenant_name="",
            users_count=user_counts.get(tenant_id, 0),
            campaigns_sent_count=campaign_counts.get(tenant_id, 0),
            responses_count=response_counts.get(tenant_id, 0),
        )
        for tenant_id in tenant_ids
    }


@router.get("/tenants", response_model=list[TenantStats])
def list_tenant_stats(db: Session = Depends(get_db)) -> list[TenantStats]:
    tenants = db.query(Tenant).order_by(Tenant.created_at.desc()).all()
    tenant_ids = {tenant.id for tenant in tenants}
    stats_map = _tenant_stats_for(db, tenant_ids)
    results = []
    for tenant in tenants:
        stats = stats_map.get(tenant.id)
        if not stats:
            stats = TenantStats(
                tenant_id=tenant.id,
                tenant_name=tenant.name,
                users_count=0,
                campaigns_sent_count=0,
                responses_count=0,
            )
        else:
            stats.tenant_name = tenant.name
        results.append(stats)
    return results


@router.get("/tenants/{tenant_id}", response_model=TenantStats)
def tenant_stats(tenant_id: int, db: Session = Depends(get_db)) -> TenantStats:
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    stats_map = _tenant_stats_for(db, {tenant_id})
    stats = stats_map.get(tenant_id)
    if not stats:
        return TenantStats(
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            users_count=0,
            campaigns_sent_count=0,
            responses_count=0,
        )
    stats.tenant_name = tenant.name
    return stats


@router.post("/tenants", response_model=TenantOut)
def create_tenant(payload: TenantCreate, db: Session = Depends(get_db)) -> TenantOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tenant name is required")
    existing = db.query(Tenant).filter(Tenant.name == name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Tenant already exists")
    tenant = Tenant(name=name)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    ensure_defaults(db, tenant.id)
    return tenant


@router.post("/users", response_model=AdminUserOut)
def create_user(payload: AdminUserCreate, db: Session = Depends(get_db)) -> AdminUserOut:
    tenant = db.query(Tenant).filter(Tenant.id == payload.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    email = payload.email.strip().lower()
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already exists")
    user = User(
        tenant_id=tenant.id,
        email=email,
        password_hash=get_password_hash(payload.password),
        is_admin=payload.is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/users/stats", response_model=list[AdminUserStats])
def user_stats(
    tenant_id: int | None = None,
    since_days: int = 30,
    db: Session = Depends(get_db),
) -> list[AdminUserStats]:
    users_query = db.query(User)
    if tenant_id is not None:
        users_query = users_query.filter(User.tenant_id == tenant_id)
    users = users_query.order_by(User.created_at.desc()).all()
    if not users:
        return []

    days = max(1, min(int(since_days), 90))
    since = datetime.utcnow() - timedelta(days=days)

    user_ids = [user.id for user in users]
    campaign_counts = dict(
        db.query(Campaign.owner_user_id, func.count(Campaign.id))
        .filter(
            Campaign.owner_user_id.in_(user_ids),
            Campaign.status == CampaignStatus.SENT,
        )
        .group_by(Campaign.owner_user_id)
        .all()
    )
    email_counts = dict(
        db.query(Campaign.owner_user_id, func.count(CampaignRecipient.id))
        .join(Campaign, CampaignRecipient.campaign_id == Campaign.id)
        .filter(
            Campaign.owner_user_id.in_(user_ids),
            CampaignRecipient.delivery_status == DeliveryStatus.SENT,
        )
        .group_by(Campaign.owner_user_id)
        .all()
    )

    # Campaign ids per user (for scoped consumption metrics)
    campaigns_by_user: dict[int, list[int]] = {}
    for owner_user_id, campaign_id in (
        db.query(Campaign.owner_user_id, Campaign.id)
        .filter(Campaign.owner_user_id.in_(user_ids))
        .all()
    ):
        campaigns_by_user.setdefault(owner_user_id, []).append(campaign_id)

    page_cache: dict[str, int | None] = {}
    consumption_by_user: dict[int, dict[str, int]] = {}
    for user in users:
        consumption_by_user[user.id] = {
            "ocr_pages_estimated": 0,
            "ocr_documents_count": 0,
            "invoice_ocr_documents_count": 0,
            "attachment_ocr_documents_count": 0,
            "tech_sheet_ocr_documents_count": 0,
            "attachment_reextract_count": 0,
            "embedding_items_count": 0,
        }

        campaign_ids = campaigns_by_user.get(user.id, [])
        if not campaign_ids:
            continue

        # Attachments OCR (Document AI only)
        att_rows = (
            db.query(CampaignAttachment)
            .filter(
                CampaignAttachment.campaign_id.in_(campaign_ids),
                CampaignAttachment.tenant_id == user.tenant_id,
                CampaignAttachment.extracted_at.isnot(None),
                CampaignAttachment.extracted_at >= since,
                CampaignAttachment.parse_engine == "document_ai_ocr",
            )
            .all()
        )
        att_pages = 0
        for row in att_rows:
            pages = _pdf_pages(row.file_path, page_cache)
            if pages is not None:
                att_pages += pages
        consumption_by_user[user.id]["ocr_pages_estimated"] += att_pages
        consumption_by_user[user.id]["attachment_ocr_documents_count"] = len(att_rows)
        consumption_by_user[user.id]["ocr_documents_count"] += len(att_rows)

        # Re-extracted attachments estimate
        reextract_count = (
            db.query(CampaignAttachment.id)
            .filter(
                CampaignAttachment.campaign_id.in_(campaign_ids),
                CampaignAttachment.tenant_id == user.tenant_id,
                CampaignAttachment.extracted_at.isnot(None),
                CampaignAttachment.created_at.isnot(None),
                CampaignAttachment.extracted_at > CampaignAttachment.created_at,
                CampaignAttachment.extracted_at >= since,
            )
            .count()
        )
        consumption_by_user[user.id]["attachment_reextract_count"] = int(reextract_count)

        # Tech sheets OCR (Document AI only)
        tech_rows = (
            db.query(TechSheet)
            .filter(
                TechSheet.campaign_id.in_(campaign_ids),
                TechSheet.tenant_id == user.tenant_id,
                TechSheet.created_at >= since,
                TechSheet.parse_engine == "document_ai_ocr",
            )
            .all()
        )
        tech_pages = 0
        for row in tech_rows:
            pages = _pdf_pages(row.file_path, page_cache)
            if pages is not None:
                tech_pages += pages
        consumption_by_user[user.id]["ocr_pages_estimated"] += tech_pages
        consumption_by_user[user.id]["tech_sheet_ocr_documents_count"] = len(tech_rows)
        consumption_by_user[user.id]["ocr_documents_count"] += len(tech_rows)

        # Invoice OCR (Document AI line extraction items)
        invoice_ids = [
            r[0]
            for r in (
                db.query(InvoiceExtractionItem.invoice_id)
                .filter(
                    InvoiceExtractionItem.campaign_id.in_(campaign_ids),
                    InvoiceExtractionItem.tenant_id == user.tenant_id,
                    InvoiceExtractionItem.created_at >= since,
                    InvoiceExtractionItem.source_engine == "document_ai",
                )
                .distinct()
                .all()
            )
        ]
        invoice_rows = []
        if invoice_ids:
            invoice_rows = (
                db.query(InboundInvoice)
                .filter(
                    InboundInvoice.id.in_(invoice_ids),
                    InboundInvoice.tenant_id == user.tenant_id,
                )
                .all()
            )
        inv_pages = 0
        for row in invoice_rows:
            pages = _pdf_pages(row.file_path, page_cache)
            if pages is not None:
                inv_pages += pages
        consumption_by_user[user.id]["ocr_pages_estimated"] += inv_pages
        consumption_by_user[user.id]["invoice_ocr_documents_count"] = len(invoice_rows)
        consumption_by_user[user.id]["ocr_documents_count"] += len(invoice_rows)

        # Embedding activity proxy: lines that had embedding score or pending embedding.
        embedding_items = (
            db.query(InvoiceExtractionItem.id)
            .filter(
                InvoiceExtractionItem.campaign_id.in_(campaign_ids),
                InvoiceExtractionItem.tenant_id == user.tenant_id,
                InvoiceExtractionItem.created_at >= since,
                (InvoiceExtractionItem.notes.like("%emb_score=%"))
                | (InvoiceExtractionItem.notes == "embedding_pending"),
            )
            .count()
        )
        consumption_by_user[user.id]["embedding_items_count"] = int(embedding_items)

    return [
        AdminUserStats(
            user_id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            is_admin=user.is_admin,
            created_at=user.created_at,
            campaigns_sent_count=campaign_counts.get(user.id, 0),
            emails_sent_count=email_counts.get(user.id, 0),
            ocr_pages_estimated=consumption_by_user[user.id]["ocr_pages_estimated"],
            ocr_documents_count=consumption_by_user[user.id]["ocr_documents_count"],
            invoice_ocr_documents_count=consumption_by_user[user.id][
                "invoice_ocr_documents_count"
            ],
            attachment_ocr_documents_count=consumption_by_user[user.id][
                "attachment_ocr_documents_count"
            ],
            tech_sheet_ocr_documents_count=consumption_by_user[user.id][
                "tech_sheet_ocr_documents_count"
            ],
            attachment_reextract_count=consumption_by_user[user.id][
                "attachment_reextract_count"
            ],
            embedding_items_count=consumption_by_user[user.id]["embedding_items_count"],
        )
        for user in users
    ]


@router.get("/technical/dashboard", response_model=dict)
def technical_dashboard(
    since_hours: int = 24,
    tenant_id: int | None = None,
    campaign_id: int | None = None,
    limit_errors: int = 20,
    db: Session = Depends(get_db),
) -> dict:
    hours = max(1, min(int(since_hours), 24 * 30))
    since = datetime.utcnow() - timedelta(hours=hours)
    error_limit = max(1, min(int(limit_errors), 100))

    usage_rows = (
        db.query(OpenAIUsageLog)
        .filter(OpenAIUsageLog.created_at >= since)
        .order_by(OpenAIUsageLog.created_at.desc())
        .all()
    )
    summary = {
        "since_hours": hours,
        "total_calls": 0,
        "success_calls": 0,
        "failed_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "avg_latency_ms": 0.0,
        "rate_limit_429": 0,
        "timeout_errors": 0,
        "cache_hits": 0,
        "cache_misses": 0,
    }
    operation_stats: dict[str, dict[str, object]] = {}
    model_stats: dict[str, dict[str, object]] = {}
    cache_stats: dict[str, dict[str, object]] = {}
    recent_errors: list[dict[str, object]] = []
    latency_values: list[int] = []

    for row in usage_rows:
        operation = str(row.operation or "unknown")
        model = str(row.model or "unknown")
        error_type = str(row.error_type or "")
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        latency = int(row.latency_ms or 0)
        prompt_tokens = int(row.prompt_tokens or 0)
        completion_tokens = int(row.completion_tokens or 0)
        total_tokens = int(row.total_tokens or 0)
        estimated_cost = float(row.estimated_cost_usd or 0.0)
        is_cache_event = operation in {"cache_hit", "cache_miss"}

        summary["total_calls"] += 1
        if row.success:
            summary["success_calls"] += 1
        else:
            summary["failed_calls"] += 1
        summary["prompt_tokens"] += prompt_tokens
        summary["completion_tokens"] += completion_tokens
        summary["total_tokens"] += total_tokens
        summary["estimated_cost_usd"] += estimated_cost
        if latency > 0:
            latency_values.append(latency)

        error_low = error_type.lower()
        if "429" in error_low:
            summary["rate_limit_429"] += 1
        if "timeout" in error_low:
            summary["timeout_errors"] += 1
        if operation == "cache_hit":
            summary["cache_hits"] += 1
        elif operation == "cache_miss":
            summary["cache_misses"] += 1

        op_stat = operation_stats.setdefault(
            operation,
            {
                "operation": operation,
                "calls": 0,
                "success": 0,
                "failed": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "avg_latency_ms": 0.0,
                "_latencies": [],
            },
        )
        op_stat["calls"] = int(op_stat["calls"]) + 1
        if row.success:
            op_stat["success"] = int(op_stat["success"]) + 1
        else:
            op_stat["failed"] = int(op_stat["failed"]) + 1
        op_stat["prompt_tokens"] = int(op_stat["prompt_tokens"]) + prompt_tokens
        op_stat["completion_tokens"] = int(op_stat["completion_tokens"]) + completion_tokens
        op_stat["total_tokens"] = int(op_stat["total_tokens"]) + total_tokens
        op_stat["estimated_cost_usd"] = float(op_stat["estimated_cost_usd"]) + estimated_cost
        if latency > 0:
            cast_lat = op_stat.get("_latencies")
            if isinstance(cast_lat, list):
                cast_lat.append(latency)

        if not is_cache_event:
            model_stat = model_stats.setdefault(
                model,
                {
                    "model": model,
                    "calls": 0,
                    "success": 0,
                    "failed": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                },
            )
            model_stat["calls"] = int(model_stat["calls"]) + 1
            if row.success:
                model_stat["success"] = int(model_stat["success"]) + 1
            else:
                model_stat["failed"] = int(model_stat["failed"]) + 1
            model_stat["prompt_tokens"] = int(model_stat["prompt_tokens"]) + prompt_tokens
            model_stat["completion_tokens"] = int(model_stat["completion_tokens"]) + completion_tokens
            model_stat["total_tokens"] = int(model_stat["total_tokens"]) + total_tokens
            model_stat["estimated_cost_usd"] = float(model_stat["estimated_cost_usd"]) + estimated_cost

        if is_cache_event:
            cache_type = str(metadata.get("cache_type") or "unknown")
            cache_stat = cache_stats.setdefault(
                cache_type,
                {"cache_type": cache_type, "hits": 0, "misses": 0, "entries": 0},
            )
            if operation == "cache_hit":
                cache_stat["hits"] = int(cache_stat["hits"]) + 1
            else:
                cache_stat["misses"] = int(cache_stat["misses"]) + 1

        if (not row.success) and len(recent_errors) < error_limit:
            recent_errors.append(
                {
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "operation": operation,
                    "endpoint": row.endpoint,
                    "model": model,
                    "error_type": row.error_type,
                    "latency_ms": row.latency_ms,
                    "metadata": metadata,
                }
            )

    if latency_values:
        summary["avg_latency_ms"] = round(sum(latency_values) / len(latency_values), 2)
    summary["estimated_cost_usd"] = round(float(summary["estimated_cost_usd"]), 6)

    for op_stat in operation_stats.values():
        latencies = op_stat.pop("_latencies", [])
        if isinstance(latencies, list) and latencies:
            op_stat["avg_latency_ms"] = round(sum(latencies) / len(latencies), 2)
        op_stat["estimated_cost_usd"] = round(float(op_stat["estimated_cost_usd"]), 6)

    cache_entry_rows = (
        db.query(OpenAIPayloadCache.cache_type, func.count(OpenAIPayloadCache.id))
        .group_by(OpenAIPayloadCache.cache_type)
        .all()
    )
    for cache_type, count in cache_entry_rows:
        cache_stat = cache_stats.setdefault(
            str(cache_type or "unknown"),
            {"cache_type": str(cache_type or "unknown"), "hits": 0, "misses": 0, "entries": 0},
        )
        cache_stat["entries"] = int(count or 0)

    offer_query = db.query(OfferAnalysisResult)
    if tenant_id is not None:
        offer_query = offer_query.filter(OfferAnalysisResult.tenant_id == tenant_id)
    if campaign_id is not None:
        offer_query = offer_query.filter(OfferAnalysisResult.campaign_id == campaign_id)
    offer_rows = offer_query.all()
    materialized = {
        "rows": len(offer_rows),
        "campaigns": len({int(row.campaign_id) for row in offer_rows if row.campaign_id is not None}),
        "invoices_items": len({int(row.invoice_item_id) for row in offer_rows if row.invoice_item_id is not None}),
        "linked_tech_sheets": len({int(row.tech_sheet_id) for row in offer_rows if row.tech_sheet_id is not None}),
        "avg_global_conformity": round(
            (
                sum(float(row.tech_sheet_similarity_display_score or 0.0) for row in offer_rows)
                / len([row for row in offer_rows if row.tech_sheet_similarity_display_score is not None])
            ),
            4,
        )
        if any(row.tech_sheet_similarity_display_score is not None for row in offer_rows)
        else None,
    }

    return {
        "summary": summary,
        "operations": sorted(operation_stats.values(), key=lambda row: int(row["calls"]), reverse=True),
        "models": sorted(model_stats.values(), key=lambda row: int(row["calls"]), reverse=True),
        "cache": {
            "types": sorted(cache_stats.values(), key=lambda row: str(row["cache_type"])),
        },
        "materialized_results": materialized,
        "recent_errors": recent_errors,
    }


@router.get("/email-queue/stats", response_model=EmailQueueStats)
def email_queue_stats(tenant_id: int | None = None, db: Session = Depends(get_db)) -> EmailQueueStats:
    counts_query = db.query(EmailJob.status, func.count(EmailJob.id))
    if tenant_id is not None:
        counts_query = counts_query.filter(EmailJob.tenant_id == tenant_id)
    counts = dict(counts_query.group_by(EmailJob.status).all())

    suppressed_query = db.query(SuppressedEmail)
    if tenant_id is not None:
        suppressed_query = suppressed_query.filter(SuppressedEmail.tenant_id == tenant_id)

    return EmailQueueStats(
        queued=counts.get(EmailJobStatus.QUEUED, 0),
        sending=counts.get(EmailJobStatus.SENDING, 0),
        sent=counts.get(EmailJobStatus.SENT, 0),
        failed=counts.get(EmailJobStatus.FAILED, 0),
        suppressed=suppressed_query.count(),
    )


@router.get("/gmail-sync/status", response_model=SchedulerStatus)
def gmail_sync_status() -> SchedulerStatus:
    return _query_task_status()


@router.post("/gmail-sync/enable", response_model=SchedulerStatus)
def gmail_sync_enable() -> SchedulerStatus:
    return _set_task_enabled(True)


@router.post("/gmail-sync/disable", response_model=SchedulerStatus)
def gmail_sync_disable() -> SchedulerStatus:
    return _set_task_enabled(False)


@router.post("/maintenance/cleanup", response_model=dict)
def maintenance_cleanup(
    dry_run: bool = True,
    tenant_id: int | None = None,
    logs_retention_days: int = 30,
    outbox_retention_days: int = 30,
    email_jobs_retention_days: int = 90,
    include_orphan_attachments: bool = True,
    db: Session = Depends(get_db),
) -> dict:
    logs_days = max(1, min(int(logs_retention_days), 3650))
    outbox_days = max(1, min(int(outbox_retention_days), 3650))
    jobs_days = max(1, min(int(email_jobs_retention_days), 3650))

    now = datetime.utcnow()
    logs_cutoff = now - timedelta(days=logs_days)
    outbox_cutoff = now - timedelta(days=outbox_days)
    jobs_cutoff = now - timedelta(days=jobs_days)

    report: dict[str, object] = {
        "dry_run": bool(dry_run),
        "tenant_id": tenant_id,
        "retention_days": {
            "logs": logs_days,
            "outbox": outbox_days,
            "email_jobs": jobs_days,
        },
        "kept_critical_data": [
            "suppliers",
            "products",
            "campaigns",
            "campaign_items",
            "campaign_recipients",
            "inbound_invoices",
            "invoice_extraction_items",
            "invoice_item_characteristic_matches",
            "tech_sheets",
            "tech_sheet_links",
            "supplier_invoice_formats",
            "embedding_cache",
            "settings",
            "user_settings",
        ],
    }

    # 1) Old logs
    old_logs = _collect_old_files(config.LOG_DIR, logs_cutoff)
    logs_bytes = _sum_file_sizes(old_logs)
    logs_deleted, logs_failed = (0, 0)
    if not dry_run:
        logs_deleted, logs_failed = _delete_files(old_logs)
    report["logs"] = {
        "root": str(config.LOG_DIR),
        "candidates": len(old_logs),
        "candidate_bytes": logs_bytes,
        "deleted": logs_deleted,
        "failed": logs_failed,
    }

    # 2) Old outbox files
    old_outbox = _collect_old_files(config.OUTBOX_DIR, outbox_cutoff)
    outbox_bytes = _sum_file_sizes(old_outbox)
    outbox_deleted, outbox_failed = (0, 0)
    if not dry_run:
        outbox_deleted, outbox_failed = _delete_files(old_outbox)
    report["outbox"] = {
        "root": str(config.OUTBOX_DIR),
        "candidates": len(old_outbox),
        "candidate_bytes": outbox_bytes,
        "deleted": outbox_deleted,
        "failed": outbox_failed,
    }

    # 3) Old sent/failed email jobs
    jobs_query = db.query(EmailJob).filter(
        EmailJob.status.in_([EmailJobStatus.SENT, EmailJobStatus.FAILED]),
        EmailJob.created_at < jobs_cutoff,
    )
    if tenant_id is not None:
        jobs_query = jobs_query.filter(EmailJob.tenant_id == tenant_id)
    jobs_candidates = jobs_query.count()
    jobs_deleted = 0
    if not dry_run and jobs_candidates > 0:
        jobs_deleted = jobs_query.delete(synchronize_session=False)
        db.commit()
    report["email_jobs"] = {
        "status_scope": ["SENT", "FAILED"],
        "before": jobs_candidates,
        "deleted": jobs_deleted,
    }

    # 4) DB references pointing to missing files (visibility only)
    def _missing_rows(model, label: str) -> int:
        query = db.query(model.file_path)
        if tenant_id is not None and hasattr(model, "tenant_id"):
            query = query.filter(model.tenant_id == tenant_id)
        rows = query.all()
        missing = 0
        for (file_path,) in rows:
            if not file_path:
                continue
            try:
                if not Path(str(file_path)).exists():
                    missing += 1
            except Exception:
                missing += 1
        report[label] = {"missing_file_references": missing, "rows_checked": len(rows)}
        return missing

    _missing_rows(CampaignAttachment, "campaign_attachments_refs")
    _missing_rows(InboundInvoice, "inbound_invoices_refs")
    _missing_rows(TechSheet, "tech_sheets_refs")

    # 5) Orphan files under campaign attachments root
    if include_orphan_attachments:
        root = config.CAMPAIGN_ATTACHMENTS_DIR
        referenced: set[str] = set()
        for model in (CampaignAttachment, InboundInvoice, TechSheet):
            query = db.query(model.file_path)
            if tenant_id is not None and hasattr(model, "tenant_id"):
                query = query.filter(model.tenant_id == tenant_id)
            for (file_path,) in query.all():
                if not file_path:
                    continue
                path_obj = Path(str(file_path))
                if _is_under_root(path_obj, root):
                    referenced.add(_normalize_file_key(path_obj))

        orphan_paths: list[Path] = []
        if root.exists() and root.is_dir():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if _normalize_file_key(path) not in referenced:
                    orphan_paths.append(path)

        orphan_bytes = _sum_file_sizes(orphan_paths)
        orphans_deleted, orphans_failed = (0, 0)
        if not dry_run and orphan_paths:
            orphans_deleted, orphans_failed = _delete_files(orphan_paths)
        report["orphan_attachments"] = {
            "root": str(root),
            "candidates": len(orphan_paths),
            "candidate_bytes": orphan_bytes,
            "deleted": orphans_deleted,
            "failed": orphans_failed,
        }
    else:
        report["orphan_attachments"] = {"skipped": True}

    return report

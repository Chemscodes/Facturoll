import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

BASE_DIR = Path(__file__).resolve().parents[1]
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "gmail_token.json"
OUTPUT_DIR = BASE_DIR / "invoices"
UNMATCHED_DIR = OUTPUT_DIR / "_UNMATCHED"
UNMATCHED_INDEX_PATH = BASE_DIR / "reports" / "gmail_unmatched_index.jsonl"
DESKTOP_DIR = BASE_DIR.parent / "desktop"

ENV_FILE = BASE_DIR / ".env"


def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()
_credentials_path_override = (os.getenv("GMAIL_CREDENTIALS_PATH") or "").strip()
if _credentials_path_override:
    CREDENTIALS_FILE = Path(_credentials_path_override).expanduser()
sys.path.append(str(BASE_DIR))

from app.db import init_db, SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    CampaignItem,
    GmailAccount,
    InboundInvoice,
    Product,
    Supplier,
    TechSheet,
    User,
)
from app import config  # noqa: E402
from app.services.document_ai import extract_full_text  # noqa: E402
from app.services.invoice_extraction import (  # noqa: E402
    apply_invoice_fallback_for_low_gpt_score,
    extract_line_items,
    match_products_hybrid,
    match_products_with_embeddings,
    rebuild_characteristics_pair_cache,
    _extract_pdf_text,
    _detect_signature,
)
from app.services.tech_sheet_extraction import extract_tech_sheet, parse_characteristics  # noqa: E402
from app.settings_service import get_user_embedding_thresholds  # noqa: E402
from app.routes.invoice_extractions import (  # noqa: E402
    _auto_link_tech_sheets,
    _extract_invoice_attachment_like_components,
    _try_warm_results_cache,
)


_EXCEL_MIME_TYPES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
}
_WORD_MIME_TYPES = {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-word.document.macroenabled.12",
}
_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
}


def _refresh_credentials(creds: Credentials) -> Credentials:
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("Missing or invalid Gmail refresh token.")
    return creds


def _get_legacy_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    creds = _refresh_credentials(creds)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def _get_service_from_token_json(token_json: str):
    if not token_json:
        raise RuntimeError("Empty Gmail token payload.")
    try:
        payload = json.loads(token_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid Gmail token JSON: {exc}") from exc
    creds = Credentials.from_authorized_user_info(payload, SCOPES)
    creds = _refresh_credentials(creds)
    return build("gmail", "v1", credentials=creds), creds.to_json()


def _decode_header(value: str) -> str:
    return value or ""


def _extract_campaign_id(subject: str) -> int | None:
    text = subject or ""
    patterns = (
        r"\[CAMP-(\d+)\]",
        r"\bCAMP-(\d+)\b",
        r"\bCAMP\s+(\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _extract_campaign_id_from_thread(service, thread_id: str | None) -> int | None:
    thread_key = str(thread_id or "").strip()
    if not thread_key:
        return None
    try:
        thread = service.users().threads().get(userId="me", id=thread_key, format="metadata").execute()
    except Exception:
        return None
    for message in thread.get("messages", []):
        headers = message.get("payload", {}).get("headers", []) or []
        subject = ""
        for header in headers:
            if str(header.get("name") or "").lower() == "subject":
                subject = _decode_header(str(header.get("value") or ""))
                break
        campaign_id = _extract_campaign_id(subject)
        if campaign_id is not None:
            return campaign_id
    return None


def _norm_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _resolve_single_open_campaign_for_sender(
    db,
    *,
    sender_email: str,
    tenant_id: int | None,
):
    email = _norm_email(sender_email)
    if not email:
        return None
    query = (
        db.query(Campaign)
        .join(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
        .join(Supplier, Supplier.id == CampaignRecipient.supplier_id)
        .filter(Supplier.email == email)
        .filter(Campaign.status.in_([CampaignStatus.SENDING, CampaignStatus.SENT]))
    )
    if tenant_id is not None:
        query = query.filter(Campaign.tenant_id == tenant_id)
    rows = (
        query.order_by(Campaign.created_at.desc(), Campaign.id.desc())
        .distinct()
        .limit(2)
        .all()
    )
    if len(rows) == 1:
        return rows[0]
    return None


def _load_unmatched_seen_keys() -> set[str]:
    if not UNMATCHED_INDEX_PATH.exists():
        return set()
    keys: set[str] = set()
    try:
        for line in UNMATCHED_INDEX_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            key = str(payload.get("attachment_key") or "").strip()
            if key:
                keys.add(key)
    except Exception:
        return set()
    return keys


def _append_unmatched_index_record(record: dict[str, object]) -> None:
    UNMATCHED_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with UNMATCHED_INDEX_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


_MEDICAL_DEVICE_TERMS = {
    "autoclave",
    "sterilisateur",
    "thermo soudeuse",
    "soudeuse",
    "nebuliseur",
    "otoscope",
    "laryngoscope",
    "aspirateur",
    "stethoscope",
    "tensiometre",
    "oxymetre",
    "fauteuil",
    "gueridon",
    "paravent",
}

_MEDICAL_CONSUMABLE_TERMS = {
    "seringue",
    "aiguille",
    "compresse",
    "gants",
    "masque",
    "sonde",
    "catheter",
    "canule",
    "perfusion",
    "tubulure",
    "pansement",
    "sparadrap",
    "bistouri",
    "stent",
    "consommable",
    "usage unique",
    "sterile",
}


_INVOICE_LIKE_FILENAME_RE = re.compile(
    r"\b("
    r"facture|invoice|proforma|pro[\s_-]*forma|devis|quotation|"
    r"bon[\s_-]*de[\s_-]*commande|purchase[\s_-]*order|fp[\s._-]*n"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_invoice(text: str) -> bool:
    if not text:
        return False
    signature = _detect_signature(text)
    if signature:
        return True
    low = text.lower()
    invoice_terms = [
        "facture",
        "proforma",
        "invoice",
        "designation",
        "quantite",
        "qte",
        "pu",
        "prix unitaire",
        "montant",
        "total",
        "tva",
        "ttc",
        "ht",
    ]
    hits = sum(1 for term in invoice_terms if term in low)
    has_table_header = "designation" in low and ("qte" in low or "quantite" in low)
    return has_table_header or hits >= 4


def _looks_like_tech_sheet(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    markers = [
        "fiche technique",
        "caracteristiques techniques",
        "caractéristiques techniques",
        "specifications",
        "spécifications",
        "datasheet",
        "brochure",
    ]
    has_marker = any(marker in low for marker in markers)
    numeric_lines = sum(1 for ln in text.splitlines() if any(ch.isdigit() for ch in ln))
    parsed = parse_characteristics(text)
    kv = parsed.get("kv", {}) if isinstance(parsed, dict) else {}
    kv_count = len(kv) if isinstance(kv, dict) else 0
    score = float((parsed or {}).get("score", 0.0) or 0.0)
    med_hits = sum(1 for t in _MEDICAL_DEVICE_TERMS if t in low) + sum(
        1 for t in _MEDICAL_CONSUMABLE_TERMS if t in low
    )

    if has_marker and numeric_lines >= 3:
        return True
    if kv_count >= 3 and score >= 0.08:
        return True
    if kv_count >= 2 and score >= 0.15:
        return True
    if med_hits >= 4 and (numeric_lines >= 2 or kv_count >= 2):
        return True
    if med_hits >= 6:
        return True
    return False


def _classify_pdf_text(text: str) -> str | None:
    if _looks_like_invoice(text):
        return "invoice"
    if _looks_like_tech_sheet(text):
        return "tech_sheet"
    return None


def _is_invoice_like_tech_result(
    *,
    file_name: str,
    raw_text: str | None,
    characteristics: object | None,
    parse_engine: str | None,
) -> bool:
    engine = (parse_engine or "").strip().lower()
    if engine.endswith("_invoice") or engine == "invoice":
        return True

    if _looks_like_invoice(str(raw_text or "")):
        return True

    if isinstance(characteristics, dict):
        doc_type_guess = str(characteristics.get("doc_type_guess") or "").strip().lower()
        if doc_type_guess == "invoice":
            return True
        kv = characteristics.get("kv")
        if isinstance(kv, dict):
            invoice_like_keys = {"designation", "qte", "quantite", "pu", "montant", "total", "tva", "ttc", "ht"}
            low_keys = {str(k).strip().lower() for k in kv.keys()}
            if len(low_keys.intersection(invoice_like_keys)) >= 3:
                return True

    if _INVOICE_LIKE_FILENAME_RE.search((file_name or "").strip().lower()):
        return True

    return False




def _iter_parts(part: dict):
    if not part:
        return
    if "parts" in part:
        for child in part.get("parts", []):
            yield from _iter_parts(child)
    yield part


def _cleanup_desktop_duplicate(file_name: str, size_bytes: int) -> None:
    if not DESKTOP_DIR.exists():
        return
    candidate = DESKTOP_DIR / file_name
    if not candidate.exists():
        return
    try:
        stat = candidate.stat()
    except OSError:
        return
    # Only remove if size matches and file is recent (last 2 hours)
    if stat.st_size != size_bytes:
        return
    now_ts = datetime.now().timestamp()
    if now_ts - stat.st_mtime > 2 * 3600:
        return
    try:
        candidate.unlink()
    except OSError:
        return


def _env_positive_int(key: str, default: int) -> int:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"Invalid {key}={raw!r}; using default {default}.")
        return default
    if value <= 0:
        print(f"Invalid {key}={raw!r}; must be > 0. Using default {default}.")
        return default
    return value


def _env_optional_positive_int(key: str) -> int | None:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        print(f"Invalid {key}={raw!r}; ignoring filter.")
        return None
    if value <= 0:
        print(f"Invalid {key}={raw!r}; ignoring filter.")
        return None
    return value


def _env_bool(key: str, default: bool = False) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _to_unix_utc(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return int(value.timestamp())


def _default_fetch_query() -> str:
    # Optional full query override from env.
    custom_query = (os.getenv("GMAIL_FETCH_QUERY") or "").strip()
    if custom_query:
        return custom_query

    # By default, fetch recent invoice-like attachments (PDF + XLSX/XLSM + Word).
    lookback_days = _env_positive_int("GMAIL_FETCH_LOOKBACK_DAYS", 2)
    return (
        f"has:attachment "
        f"(filename:pdf OR filename:xls OR filename:xlsx OR filename:xlsm OR filename:doc OR filename:docx OR filename:docm) "
        f"newer_than:{lookback_days}d"
    )


def _default_image_query() -> str:
    custom_query = (os.getenv("GMAIL_FETCH_IMAGE_QUERY") or "").strip()
    if custom_query:
        return custom_query
    lookback_days = _env_positive_int("GMAIL_FETCH_LOOKBACK_DAYS", 2)
    return (
        "has:attachment "
        "(filename:jpg OR filename:jpeg OR filename:png OR filename:webp) "
        f"newer_than:{lookback_days}d"
    )


def _is_excel_attachment(filename: str, mime_type: str) -> bool:
    low = filename.lower()
    if low.endswith(".xls") or low.endswith(".xlsx") or low.endswith(".xlsm"):
        return True
    return mime_type in _EXCEL_MIME_TYPES


def _is_word_attachment(filename: str, mime_type: str) -> bool:
    low = filename.lower()
    if low.endswith(".doc") or low.endswith(".docx") or low.endswith(".docm"):
        return True
    return (mime_type or "").strip().lower() in _WORD_MIME_TYPES


def _is_image_attachment(filename: str, mime_type: str) -> bool:
    low = filename.lower()
    if low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".png") or low.endswith(".webp"):
        return True
    return (mime_type or "").strip().lower() in _IMAGE_MIME_TYPES


def _image_attachment_target_pdf_name(filename: str) -> str:
    base = Path(filename or "invoice_image.jpg").name
    stem = Path(base).stem or "invoice_image"
    return f"{stem}.pdf"


def _word_attachment_target_pdf_name(filename: str) -> str:
    base = Path(filename or "invoice_document.docx").name
    stem = Path(base).stem or "invoice_document"
    return f"{stem}.pdf"


def _convert_image_to_pdf(image_path: Path) -> Path | None:
    try:
        from PIL import Image  # type: ignore
    except Exception as exc:
        print(f"Image->PDF conversion unavailable (Pillow missing): {exc}")
        return None

    pdf_path = image_path.with_suffix(".pdf")
    try:
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                rgb = img.convert("RGB")
            else:
                rgb = img if img.mode == "RGB" else img.convert("RGB")
            rgb.save(pdf_path, "PDF", resolution=300.0)
    except Exception as exc:
        print(f"Image->PDF conversion failed for {image_path}: {exc}")
        return None
    return pdf_path if pdf_path.exists() else None


def _convert_word_to_pdf(document_path: Path) -> Path | None:
    configured_bin = (os.getenv("LIBREOFFICE_BIN") or "").strip()
    soffice_bin = configured_bin or shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice_bin:
        print("Word->PDF conversion unavailable (LibreOffice/soffice missing)")
        return None

    pdf_path = document_path.with_suffix(".pdf")
    try:
        result = subprocess.run(
            [
                soffice_bin,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(document_path.parent),
                str(document_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:
        print(f"Word->PDF conversion failed for {document_path}: {exc}")
        return None

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or f"exit={result.returncode}"
        print(f"Word->PDF conversion failed for {document_path}: {detail}")
        return None
    if not pdf_path.exists():
        print(f"Word->PDF conversion failed for {document_path}: missing output PDF")
        return None
    return pdf_path


def _resolve_pipeline_user(
    db,
    campaign: Campaign,
    preferred_user_id: int | None = None,
) -> User | None:
    if preferred_user_id is not None:
        user = (
            db.query(User)
            .filter(
                User.id == preferred_user_id,
                User.tenant_id == campaign.tenant_id,
            )
            .first()
        )
        if user is not None:
            return user

    if campaign.owner_user_id is not None:
        owner = (
            db.query(User)
            .filter(
                User.id == campaign.owner_user_id,
                User.tenant_id == campaign.tenant_id,
            )
            .first()
        )
        if owner is not None:
            return owner

    admin_user = (
        db.query(User)
        .filter(User.tenant_id == campaign.tenant_id, User.is_admin.is_(True))
        .order_by(User.id.asc())
        .first()
    )
    if admin_user is not None:
        return admin_user

    return (
        db.query(User)
        .filter(User.tenant_id == campaign.tenant_id)
        .order_by(User.id.asc())
        .first()
    )


def _run_invoice_pipeline(
    *,
    invoice: InboundInvoice,
    db,
    current_user: User | None,
) -> list:
    items = extract_line_items(invoice, db)
    if not items:
        # Keep parse metadata/signature even when no lines are extracted.
        db.commit()
        return []

    product_rows = (
        db.query(Product)
        .join(CampaignItem, CampaignItem.product_id == Product.id)
        .filter(
            CampaignItem.campaign_id == invoice.campaign_id,
            CampaignItem.tenant_id == invoice.tenant_id,
            Product.tenant_id == invoice.tenant_id,
        )
        .all()
    )

    if current_user is not None:
        thresholds = get_user_embedding_thresholds(
            db, current_user.tenant_id, current_user.id
        )
        match_products_hybrid(
            items,
            product_rows,
            threshold=0.70,
            embed_threshold=thresholds.confirm_threshold,
            embed_min_threshold=thresholds.verify_threshold,
            characteristics_match_threshold=thresholds.characteristics_threshold,
            db=db,
        )
    else:
        # Safety fallback when no user context is available.
        product_names = [p.name for p in product_rows]
        match_products_with_embeddings(items, product_names)
        thresholds = None

    fallback_report = apply_invoice_fallback_for_low_gpt_score(invoice, items)
    if int(fallback_report.get("applied") or 0) > 0:
        if current_user is not None:
            if thresholds is None:
                thresholds = get_user_embedding_thresholds(
                    db, current_user.tenant_id, current_user.id
                )
            match_products_hybrid(
                items,
                product_rows,
                threshold=0.70,
                embed_threshold=thresholds.confirm_threshold,
                embed_min_threshold=thresholds.verify_threshold,
                characteristics_match_threshold=thresholds.characteristics_threshold,
                db=db,
            )
        else:
            product_names = [p.name for p in product_rows]
            match_products_with_embeddings(items, product_names)

    if bool(getattr(config, "OPENAI_INVOICE_COMPONENTS_PRECOMPUTE_ON_EXTRACT", True)):
        for item in items:
            try:
                _extract_invoice_attachment_like_components(item)
            except Exception as exc:
                print(
                    "Invoice components precompute failed "
                    f"(tenant={invoice.tenant_id}, invoice={invoice.id}, item={item.product_name}): {exc}"
                )

    for item in items:
        db.add(item)
    db.commit()

    if current_user is not None:
        item_ids = [it.id for it in items if it.id is not None]
        if item_ids and thresholds is not None:
            rebuild_characteristics_pair_cache(
                db=db,
                tenant_id=current_user.tenant_id,
                campaign_id=invoice.campaign_id,
                threshold=float(thresholds.characteristics_threshold),
                item_ids=item_ids,
            )
            db.commit()

        _auto_link_tech_sheets(
            db=db,
            current_user=current_user,
            campaign_id=invoice.campaign_id,
            invoice_id=invoice.id,
            sender_email=invoice.sender_email,
        )
        _try_warm_results_cache(db, current_user, invoice.campaign_id)

    return items


def _fetch_pdfs_for_service(
    service,
    db,
    query: str,
    max_results: int,
    tenant_id: int | None = None,
    preferred_user_id: int | None = None,
    include_images: bool = False,
) -> list[dict]:
    results = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    messages = results.get("messages", [])

    saved = []
    seen_attachments: set[tuple[str, str]] = set()
    unmatched_seen_keys = _load_unmatched_seen_keys()
    for msg in messages:
        msg_id = msg["id"]
        data = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
        sender_raw = _decode_header(headers.get("from", ""))
        sender_name, sender_email = parseaddr(sender_raw)
        sender_identity = sender_email or sender_raw or "unknown"
        subject = _decode_header(headers.get("subject", ""))
        date = _decode_header(headers.get("date", ""))
        thread_id = str(data.get("threadId") or "").strip() or None
        campaign_id = _extract_campaign_id(subject)
        if campaign_id is None and thread_id:
            campaign_id = _extract_campaign_id_from_thread(service, thread_id)
        campaign = None
        explicit_campaign_reference = campaign_id is not None
        if campaign_id:
            campaign_query = db.query(Campaign).filter(Campaign.id == campaign_id)
            if tenant_id is not None:
                campaign_query = campaign_query.filter(Campaign.tenant_id == tenant_id)
            campaign = campaign_query.first()
            if campaign is None:
                campaign_id = None
            elif campaign.status == CampaignStatus.CLOSED:
                # Do not auto-ingest attachments into a closed campaign even if the
                # subject/thread still carries its [CAMP-id] marker.
                campaign = None
        if campaign is None and not explicit_campaign_reference:
            campaign = _resolve_single_open_campaign_for_sender(
                db,
                sender_email=sender_email or sender_identity,
                tenant_id=tenant_id,
            )
            if campaign is not None:
                campaign_id = int(campaign.id)

        payload = data.get("payload", {})
        for part in _iter_parts(payload):
            filename = part.get("filename", "")
            mime_type = part.get("mimeType", "")
            body = part.get("body", {})
            is_pdf = filename.lower().endswith(".pdf") or mime_type == "application/pdf"
            is_excel = _is_excel_attachment(filename, mime_type)
            is_word = _is_word_attachment(filename, mime_type)
            is_image = include_images and _is_image_attachment(filename, mime_type)
            if not is_pdf and not is_excel and not is_word and not is_image:
                continue

            default_ext = ".pdf" if is_pdf else (".xlsx" if is_excel else (".docx" if is_word else ".jpg"))
            safe_name = filename or f"invoice_{msg_id}{default_ext}"
            if is_image:
                row_file_name = _image_attachment_target_pdf_name(safe_name)
            elif is_word:
                row_file_name = _word_attachment_target_pdf_name(safe_name)
            else:
                row_file_name = safe_name
            attach_id = body.get("attachmentId")
            if not attach_id:
                continue
            attachment_key = (msg_id, str(attach_id))
            if attachment_key in seen_attachments:
                continue
            seen_attachments.add(attachment_key)
            unmatched_attachment_key = f"{msg_id}:{attach_id}"

            if campaign is None or not campaign_id:
                if unmatched_attachment_key in unmatched_seen_keys:
                    continue
                attach = service.users().messages().attachments().get(
                    userId="me", messageId=msg_id, id=attach_id
                ).execute()
                file_data = base64.urlsafe_b64decode(attach["data"].encode("utf-8"))
                safe_name = filename or f"invoice_{msg_id}{default_ext}"
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                unmatched_dir = UNMATCHED_DIR / ts[:8]
                unmatched_dir.mkdir(parents=True, exist_ok=True)
                out_path = unmatched_dir / f"{ts}_{safe_name}"
                out_path.write_bytes(file_data)
                if is_word:
                    converted_pdf_path = _convert_word_to_pdf(out_path)
                    if converted_pdf_path is None:
                        try:
                            out_path.unlink()
                        except OSError:
                            pass
                        print(f"Rejected Word attachment (cannot convert to PDF): {safe_name}")
                        continue
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                    out_path = converted_pdf_path
                record = {
                    "attachment_key": unmatched_attachment_key,
                    "message_id": msg_id,
                    "thread_id": thread_id,
                    "tenant_id": tenant_id,
                    "sender": sender_raw,
                    "sender_email": sender_email or None,
                    "subject": subject,
                    "date": date,
                    "file": str(out_path),
                    "reason": (
                        "closed_campaign_reference"
                        if explicit_campaign_reference
                        else "unmatched_campaign"
                    ),
                }
                _append_unmatched_index_record(record)
                unmatched_seen_keys.add(unmatched_attachment_key)
                saved.append(record)
                continue

            invoice_exists = (
                db.query(InboundInvoice)
                .filter(
                    InboundInvoice.tenant_id == campaign.tenant_id,
                    InboundInvoice.campaign_id == campaign_id,
                    InboundInvoice.message_id == msg_id,
                    InboundInvoice.file_name == row_file_name,
                )
                .first()
            )
            if invoice_exists:
                continue

            attach = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=attach_id
            ).execute()
            file_data = base64.urlsafe_b64decode(attach["data"].encode("utf-8"))
            file_hash = hashlib.sha256(file_data).hexdigest()
            _cleanup_desktop_duplicate(safe_name, len(file_data))

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            campaign_dir = OUTPUT_DIR / f"CAMP-{campaign_id}"
            campaign_dir.mkdir(parents=True, exist_ok=True)
            out_path = campaign_dir / f"{ts}_{safe_name}"
            out_path.write_bytes(file_data)

            source_was_image = bool(is_image)
            source_was_word = bool(is_word)
            if source_was_image:
                converted_pdf_path = _convert_image_to_pdf(out_path)
                if converted_pdf_path is None:
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                    print(f"Rejected image attachment (cannot convert to PDF): {safe_name}")
                    continue
                try:
                    out_path.unlink()
                except OSError:
                    pass
                out_path = converted_pdf_path
            elif source_was_word:
                converted_pdf_path = _convert_word_to_pdf(out_path)
                if converted_pdf_path is None:
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                    print(f"Rejected Word attachment (cannot convert to PDF): {safe_name}")
                    continue
                try:
                    out_path.unlink()
                except OSError:
                    pass
                out_path = converted_pdf_path

            # Excel attachments are treated as invoices directly.
            doc_type = "invoice"
            if (is_pdf or source_was_word) and not source_was_image:
                tech_sheet_hash_exists = (
                    db.query(TechSheet.id)
                    .filter(
                        TechSheet.tenant_id == campaign.tenant_id,
                        TechSheet.campaign_id == campaign_id,
                        TechSheet.file_hash == file_hash,
                    )
                    .first()
                )
                if tech_sheet_hash_exists:
                    continue

                # Detect invoice vs tech sheet.
                # First pass: free local text extraction.
                text = _extract_pdf_text(str(out_path)) or ""
                doc_type = _classify_pdf_text(text)

                # Second pass (only when local text is weak): OCR text for classification.
                if doc_type is None and len(text.strip()) < config.TECH_SHEET_LOCAL_MIN_CHARS:
                    try:
                        ocr_text = extract_full_text(str(out_path)) or ""
                    except Exception:
                        ocr_text = ""
                    if ocr_text:
                        doc_type = _classify_pdf_text(ocr_text)

                if doc_type is None:
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                    print(f"Rejected + deleted (unknown PDF type): {out_path}")
                    continue

            if doc_type == "invoice":
                invoice = InboundInvoice(
                    tenant_id=campaign.tenant_id,
                    campaign_id=campaign_id,
                    sender_email=sender_identity,
                    sender_name=sender_name or "",
                    subject=subject,
                    message_id=msg_id,
                    file_name=row_file_name,
                    file_path=str(out_path),
                )
                db.add(invoice)
                db.commit()
                db.refresh(invoice)

                # Auto-extract line items for the invoice
                try:
                    current_user = _resolve_pipeline_user(
                        db,
                        campaign,
                        preferred_user_id=preferred_user_id,
                    )
                    items = _run_invoice_pipeline(
                        invoice=invoice,
                        db=db,
                        current_user=current_user,
                    )
                    print(
                        "Invoice pipeline done "
                        f"(invoice_id={invoice.id}, campaign_id={campaign_id}, items={len(items)}, "
                        f"user_ctx={'yes' if current_user is not None else 'no'})"
                    )
                except Exception as exc:
                    db.rollback()
                    print(f"Invoice extraction failed for {out_path}: {exc}")
            else:
                # Treat as tech sheet: free local text first, fallback OCR
                try:
                    raw_text, characteristics, score, engine = extract_tech_sheet(str(out_path))
                    if bool(getattr(config, "TECH_SHEET_SKIP_INVOICE_LIKE", True)) and _is_invoice_like_tech_result(
                        file_name=safe_name,
                        raw_text=raw_text,
                        characteristics=characteristics,
                        parse_engine=engine,
                    ):
                        # Safety guard: if tech extraction says invoice-like, route it to invoice flow.
                        invoice = InboundInvoice(
                            tenant_id=campaign.tenant_id,
                            campaign_id=campaign_id,
                            sender_email=sender_identity,
                            sender_name=sender_name or "",
                            subject=subject,
                            message_id=msg_id,
                            file_name=row_file_name,
                            file_path=str(out_path),
                        )
                        db.add(invoice)
                        db.commit()
                        db.refresh(invoice)
                        try:
                            current_user = _resolve_pipeline_user(
                                db,
                                campaign,
                                preferred_user_id=preferred_user_id,
                            )
                            items = _run_invoice_pipeline(
                                invoice=invoice,
                                db=db,
                                current_user=current_user,
                            )
                            print(
                                "Invoice fallback pipeline done "
                                f"(invoice_id={invoice.id}, campaign_id={campaign_id}, items={len(items)}, "
                                f"user_ctx={'yes' if current_user is not None else 'no'})"
                            )
                        except Exception as exc:
                            db.rollback()
                            print(f"Invoice fallback extraction failed for {out_path}: {exc}")
                        continue
                    sheet = TechSheet(
                        tenant_id=campaign.tenant_id,
                        campaign_id=campaign_id,
                        sender_email=sender_identity,
                        file_name=row_file_name,
                        file_path=str(out_path),
                        file_hash=file_hash,
                        parse_engine=engine,
                        parse_score=score,
                        raw_text=raw_text,
                        characteristics=characteristics,
                    )
                    db.add(sheet)
                    db.commit()
                except Exception as exc:
                    print(f"Tech sheet extraction failed for {out_path}: {exc}")

            saved.append(
                {
                    "message_id": msg_id,
                    "sender": sender_raw,
                    "subject": subject,
                    "date": date,
                    "file": str(out_path),
                }
            )
    return saved


def fetch_pdfs(query: str | None = None, max_results: int | None = None):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    db = SessionLocal()

    effective_query = (query or "").strip() or _default_fetch_query()
    effective_max_results = (
        max_results
        if isinstance(max_results, int) and max_results > 0
        else _env_positive_int("GMAIL_FETCH_MAX_RESULTS", 10)
    )
    include_images = _env_bool("GMAIL_FETCH_INCLUDE_IMAGES", False)
    image_query = _default_image_query() if include_images else ""
    selected_user_id = _env_optional_positive_int("GMAIL_FETCH_USER_ID")
    print(f"Gmail fetch query: {effective_query} (maxResults={effective_max_results})")
    if include_images:
        print(f"Gmail image query enabled: {image_query} (maxResults={effective_max_results})")
    if selected_user_id is not None:
        print(f"Gmail account filter: user_id={selected_user_id}")

    saved: list[dict] = []
    accounts_query = db.query(GmailAccount).filter(GmailAccount.enabled.is_(True))
    if selected_user_id is not None:
        accounts_query = accounts_query.filter(GmailAccount.user_id == selected_user_id)
    accounts = accounts_query.order_by(GmailAccount.user_id.asc()).all()

    if accounts:
        print(f"Enabled Gmail accounts found: {len(accounts)}")
        for account in accounts:
            try:
                service, refreshed_token_json = _get_service_from_token_json(account.token_json)
                if refreshed_token_json != account.token_json:
                    account.token_json = refreshed_token_json
                account_query = effective_query
                last_sync_ts = _to_unix_utc(account.last_sync_at)
                account_saved: list[dict] = []
                query_jobs: list[tuple[str, bool]] = [(effective_query, False)]
                if include_images and image_query:
                    query_jobs.append((image_query, True))

                for raw_query, is_image_query in query_jobs:
                    account_query = raw_query
                    if last_sync_ts is not None:
                        # Small overlap to tolerate clock drift and delayed delivery.
                        account_query = f"{raw_query} after:{max(0, last_sync_ts - 120)}"
                    account_saved.extend(
                        _fetch_pdfs_for_service(
                            service=service,
                            db=db,
                            query=account_query,
                            max_results=effective_max_results,
                            tenant_id=account.tenant_id,
                            preferred_user_id=account.user_id,
                            include_images=is_image_query,
                        )
                    )
                account.last_sync_at = datetime.now(UTC).replace(tzinfo=None)
                account.last_error = None
                db.commit()
                saved.extend(account_saved)
                print(
                    f"Gmail account {account.google_email} (user={account.user_id}, tenant={account.tenant_id}) "
                    f"query={account_query!r} imported {len(account_saved)} file(s)."
                )
            except Exception as exc:
                db.rollback()
                account.last_error = str(exc)
                db.commit()
                print(
                    f"Gmail account {account.google_email} (user={account.user_id}, tenant={account.tenant_id}) "
                    f"sync failed: {exc}"
                )
    else:
        if selected_user_id is not None:
            print("No active Gmail account found for selected user. Skipping legacy fallback.")
            db.close()
            return []
        print("No active Gmail accounts in DB. Falling back to legacy gmail_token.json flow.")
        service = _get_legacy_service()
        saved = []
        query_jobs: list[tuple[str, bool]] = [(effective_query, False)]
        if include_images and image_query:
            query_jobs.append((image_query, True))
        for raw_query, is_image_query in query_jobs:
            saved.extend(
                _fetch_pdfs_for_service(
                    service=service,
                    db=db,
                    query=raw_query,
                    max_results=effective_max_results,
                    tenant_id=None,
                    preferred_user_id=None,
                    include_images=is_image_query,
                )
            )

    db.close()
    return saved


if __name__ == "__main__":
    items = fetch_pdfs()
    for item in items:
        print(item)

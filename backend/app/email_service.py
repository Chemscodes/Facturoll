import logging
import mimetypes
import time
from base64 import b64encode
from email.message import EmailMessage
from pathlib import Path
from typing import List

import httpx
import smtplib
from jinja2 import Environment, FileSystemLoader

from app import config
from app.models import Campaign, CampaignItem, Supplier
from app.schemas import Settings

logger = logging.getLogger("app")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
JINJA_ENV = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def _render_placeholders(
    text: str | None,
    campaign: Campaign,
    supplier: Supplier,
    reply_url: str,
) -> str:
    if not text:
        return ""
    mapping = {
        "supplier_name": supplier.name or "",
        "supplier_email": supplier.email or "",
        "campaign_title": campaign.title or "",
        "campaign_subject": campaign.subject or "",
        "campaign_code": f"CAMP-{campaign.id}",
        "reply_url": reply_url,
    }
    rendered = text
    for key, value in mapping.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def render_campaign_email(
    campaign: Campaign,
    supplier: Supplier,
    items: List[CampaignItem],
    reply_url: str,
) -> str:
    template = JINJA_ENV.get_template("email.html")
    message_html = _render_placeholders(campaign.message_html, campaign, supplier, reply_url)
    return template.render(
        campaign=campaign,
        supplier=supplier,
        items=items,
        reply_url=reply_url,
        campaign_code=f"CAMP-{campaign.id}",
        message_html=message_html,
    )


def render_campaign_subject(
    subject: str,
    campaign: Campaign,
    supplier: Supplier,
    reply_url: str,
) -> str:
    rendered = _render_placeholders(subject, campaign, supplier, reply_url)
    code = f"[CAMP-{campaign.id}]"
    if code in rendered:
        return rendered
    return f"{code} {rendered}".strip()


def _build_message(
    subject: str,
    to_email: str,
    from_email: str,
    reply_to: str,
    html_body: str,
    attachments: List[dict] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = to_email
    msg["From"] = from_email
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content("Please use an HTML capable email client to view this message.")
    msg.add_alternative(html_body, subtype="html")
    for item in attachments or []:
        file_path = Path(str(item.get("file_path", "")))
        if not file_path.exists() or not file_path.is_file():
            continue
        file_name = str(item.get("file_name") or file_path.name)
        content_type, _ = mimetypes.guess_type(file_name)
        if content_type:
            maintype, subtype = content_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        payload = file_path.read_bytes()
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=file_name)
    return msg


def _write_outbox(msg: EmailMessage, outbox_dir: Path, filename: str) -> None:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    path = outbox_dir / filename
    path.write_text(msg.as_string(), encoding="utf-8")


def _build_sendgrid_attachments(attachments: List[dict] | None) -> List[dict]:
    payload: List[dict] = []
    for item in attachments or []:
        file_path = Path(str(item.get("file_path", "")))
        if not file_path.exists() or not file_path.is_file():
            continue
        file_name = str(item.get("file_name") or file_path.name)
        content_type, _ = mimetypes.guess_type(file_name)
        encoded = b64encode(file_path.read_bytes()).decode("ascii")
        attachment: dict = {
            "content": encoded,
            "filename": file_name,
            "disposition": "attachment",
        }
        if content_type:
            attachment["type"] = content_type
        payload.append(attachment)
    return payload


def _send_via_sendgrid(
    subject: str,
    to_email: str,
    html_body: str,
    settings: Settings,
    attachments: List[dict] | None = None,
) -> None:
    api_key = settings.sendgrid_api_key.strip()
    if not api_key:
        raise RuntimeError("SendGrid API key is not configured")
    if not settings.from_email.strip():
        raise RuntimeError("from_email is not configured")

    payload: dict = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": settings.from_email},
        "subject": subject,
        "content": [
            {
                "type": "text/plain",
                "value": "Please use an HTML capable email client to view this message.",
            },
            {"type": "text/html", "value": html_body},
        ],
    }
    if settings.reply_to.strip():
        payload["reply_to"] = {"email": settings.reply_to.strip()}
    sg_attachments = _build_sendgrid_attachments(attachments)
    if sg_attachments:
        payload["attachments"] = sg_attachments

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    for attempt in range(1, 4):
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(SENDGRID_API_URL, json=payload, headers=headers)
            if response.status_code in (200, 202):
                return
            body = response.text[:500]
            logger.warning(
                "SendGrid attempt %s failed with status %s: %s",
                attempt,
                response.status_code,
                body,
            )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(1)
                continue
            raise RuntimeError(f"SendGrid send failed ({response.status_code}): {body}")
        except httpx.RequestError as exc:
            logger.warning("SendGrid attempt %s failed: %s", attempt, exc)
            if attempt >= 3:
                raise RuntimeError(f"SendGrid request failed: {exc}") from exc
            time.sleep(1)


def send_email(
    subject: str,
    to_email: str,
    html_body: str,
    settings: Settings,
    attachments: List[dict] | None = None,
) -> None:
    msg = _build_message(
        subject,
        to_email,
        settings.from_email,
        settings.reply_to,
        html_body,
        attachments=attachments,
    )

    if settings.mode_mock:
        filename = f"mock_{int(time.time())}_{to_email.replace('@', '_')}.eml"
        _write_outbox(msg, config.OUTBOX_DIR, filename)
        logger.info("Mock email stored: %s", filename)
        return

    provider = (settings.email_provider or "smtp").strip().lower()
    if provider == "sendgrid":
        _send_via_sendgrid(subject, to_email, html_body, settings, attachments=attachments)
        return

    if not settings.smtp_host:
        raise RuntimeError("SMTP host is not configured")

    for attempt in range(1, 4):
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
                if settings.use_tls:
                    server.starttls()
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
            return
        except smtplib.SMTPException as exc:
            logger.warning("SMTP attempt %s failed: %s", attempt, exc)
            if attempt >= 3:
                raise
            time.sleep(1)

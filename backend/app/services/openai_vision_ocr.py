from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from app import config
from app.services.openai_chat import call_chat_completion_json_with_retry


def _mime_from_name(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or "image/png"


def _build_data_url(image_bytes: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _extract_pdf_page_images(file_path: str, max_pages: int) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    try:
        reader = PdfReader(file_path)
    except Exception:
        return out
    pages = reader.pages[: max(1, int(max_pages))]
    for idx, page in enumerate(pages, start=1):
        try:
            images = list(page.images)
        except Exception:
            images = []
        if not images:
            continue
        # Keep largest image as page surrogate (works well for scanned PDFs).
        best = max(images, key=lambda img: len(getattr(img, "data", b"") or b""))
        name = str(getattr(best, "name", "") or f"page_{idx}.png")
        data = bytes(getattr(best, "data", b"") or b"")
        if data:
            out.append((name, data))
    return out


def _extract_input_images(file_path: str, max_pages: int) -> list[tuple[str, bytes]]:
    path = Path(file_path)
    if not path.exists():
        return []
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return [(path.name, path.read_bytes())]
    if suffix == ".pdf":
        return _extract_pdf_page_images(str(path), max_pages=max_pages)
    return []


def extract_text_from_file(
    *,
    file_path: str,
    operation: str,
    max_pages: int | None = None,
) -> str:
    if not config.OPENAI_API_KEY:
        return ""

    max_pages = max(1, int(max_pages or config.OPENAI_OCR_VISION_MAX_PAGES or 8))
    image_inputs = _extract_input_images(file_path, max_pages=max_pages)
    if not image_inputs:
        return ""

    model = str(config.OPENAI_OCR_VISION_MODEL or "").strip() or "gpt-4o"
    fallback_model = str(config.OPENAI_OCR_VISION_FALLBACK_MODEL or "").strip() or None
    timeout_s = max(30, int(config.OPENAI_OCR_VISION_TIMEOUT_SECONDS or 120))
    retry_attempts = max(1, int(config.OPENAI_OCR_VISION_RETRY_ATTEMPTS or 3))
    retry_backoff_s = max(0.2, float(config.OPENAI_OCR_VISION_RETRY_BACKOFF_SECONDS or 1.0))

    system_prompt = (
        "Extract OCR text faithfully from the provided document images. "
        "Preserve natural reading order and line breaks where possible. "
        "Do not translate, summarize, or invent."
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
    }

    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "OCR the full content of all images and return plain text.",
        }
    ]
    for name, data in image_inputs:
        mime = _mime_from_name(name)
        user_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _build_data_url(data, mime),
                    "detail": "high",
                },
            }
        )

    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "vision_ocr_text",
                "strict": True,
                "schema": schema,
            },
        },
    }

    parsed, _used_model = call_chat_completion_json_with_retry(
        operation=operation,
        payload=payload,
        primary_model=model,
        fallback_model=fallback_model,
        timeout_s=timeout_s,
        retry_attempts=retry_attempts,
        retry_backoff_s=retry_backoff_s,
        metadata={"file_path": str(file_path), "images": len(image_inputs)},
    )
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("text") or "").strip()


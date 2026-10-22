import os
import tempfile
from pathlib import Path

from google.cloud import documentai
from pypdf import PdfReader, PdfWriter

from app import config


def _get_client() -> documentai.DocumentProcessorServiceClient:
    credentials_path = config.GOOGLE_CREDENTIALS_PATH
    if credentials_path:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
    return documentai.DocumentProcessorServiceClient()


def _process_with_processor(file_path: str, processor_id: str) -> documentai.Document:
    project_id = config.GOOGLE_PROJECT_ID
    location = config.GOOGLE_LOCATION
    if not project_id or not processor_id:
        raise ValueError("Google Document AI config is missing")

    file = Path(file_path)
    if not file.exists():
        raise FileNotFoundError(file_path)

    client = _get_client()
    name = client.processor_path(project_id, location, processor_id)
    with file.open("rb") as f:
        raw_document = documentai.RawDocument(content=f.read(), mime_type="application/pdf")

    result = client.process_document(
        request=documentai.ProcessRequest(name=name, raw_document=raw_document)
    )
    return result.document


def _pdf_page_count(file_path: str) -> int:
    try:
        return len(PdfReader(file_path).pages)
    except Exception:
        return 1


def _looks_like_incomplete_multi_page_text(file_path: str, text: str) -> bool:
    page_count = _pdf_page_count(file_path)
    if page_count <= 1:
        return False
    clean_len = len((text or "").strip())
    # If OCR text is too short for a multi-page document, re-run page by page.
    min_expected = max(1200, page_count * 700)
    return clean_len < min_expected


def _extract_full_text_page_by_page(file_path: str, processor_id: str) -> str:
    try:
        reader = PdfReader(file_path)
    except Exception:
        return ""
    if len(reader.pages) <= 1:
        return ""

    chunks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="docai_pages_") as tmpdir:
        tmp_path = Path(tmpdir)
        for idx, page in enumerate(reader.pages, start=1):
            writer = PdfWriter()
            writer.add_page(page)
            page_file = tmp_path / f"page_{idx}.pdf"
            with page_file.open("wb") as f:
                writer.write(f)

            try:
                doc = _process_with_processor(str(page_file), processor_id)
            except Exception:
                continue

            page_text = (doc.text or "").strip()
            if page_text:
                chunks.append(f"[PAGE {idx}]\n{page_text}")

    return "\n\n".join(chunks).strip()


def process_invoice(file_path: str) -> documentai.Document:
    processor_id = config.GOOGLE_PROCESSOR_ID
    return _process_with_processor(file_path, processor_id)


def extract_full_text(file_path: str) -> str:
    # For non-invoice OCR use a dedicated OCR processor when configured.
    processor_id = config.GOOGLE_OCR_PROCESSOR_ID or config.GOOGLE_PROCESSOR_ID
    doc = _process_with_processor(file_path, processor_id)
    text = doc.text or ""
    if _looks_like_incomplete_multi_page_text(file_path, text):
        fallback_text = _extract_full_text_page_by_page(file_path, processor_id)
        if len(fallback_text) > len(text):
            return fallback_text
    return text

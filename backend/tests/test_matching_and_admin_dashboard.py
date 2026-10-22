import os

os.environ["APP_DB_URL"] = "sqlite:///:memory:"
os.environ["APP_ALLOW_SQLITE"] = "1"

from fastapi.testclient import TestClient

from app.models import ExtractionStatus, InvoiceExtractionItem
from app.services.invoice_extraction import (
    _apply_item_match_payload,
    _serialize_item_match_payload,
)
from main import app


def test_item_match_payload_roundtrip_preserves_matching_state():
    item = InvoiceExtractionItem(
        tenant_id=1,
        invoice_id=1,
        campaign_id=1,
        product_name="AUTOCLAVE 24 L",
        status=ExtractionStatus.CONFIRMED,
        notes="gpt_match=AUTOCLAVE gpt_score=0.84",
        similarity_score=0.84,
        similarity_threshold=0.30,
        similarity_points={"matched_points": 3, "total_points": 4},
    )
    payload = _serialize_item_match_payload(item)

    restored = InvoiceExtractionItem(
        tenant_id=1,
        invoice_id=1,
        campaign_id=1,
        product_name="AUTOCLAVE 24 L",
        status=ExtractionStatus.TO_VERIFY,
    )
    assert _apply_item_match_payload(restored, payload) is True
    assert restored.status == ExtractionStatus.CONFIRMED
    assert restored.notes == "gpt_match=AUTOCLAVE gpt_score=0.84"
    assert restored.similarity_score == 0.84
    assert restored.similarity_points == {"matched_points": 3, "total_points": 4}


def test_admin_technical_dashboard_endpoint_returns_expected_sections():
    with TestClient(app) as client:
        login = client.post(
            "/auth/login",
            json={"email": "admin@local", "password": "admin123"},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        response = client.get("/admin/technical/dashboard?since_hours=24", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert "summary" in data
        assert "operations" in data
        assert "models" in data
        assert "cache" in data
        assert "materialized_results" in data
        assert "recent_errors" in data

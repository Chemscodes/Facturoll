import os

os.environ["APP_DB_URL"] = "sqlite:///:memory:"
os.environ["APP_ALLOW_SQLITE"] = "1"

from app.models import ExtractionStatus
from app.routes.campaigns import _serialize_results_payload
from app.routes.invoice_extractions import (
    _compute_tech_sheet_similarity_display,
    _enforce_binary_component_coverage,
    _normalize_invoice_components_payload,
    _weighted_similarity_from_components,
)
from app.routes.tech_sheets import _item_group_key, _sheet_group_key
from app.schemas import OfferEntry, ProductOffers
from app.services.campaign_attachment_extraction import _normalize_characteristics_payload
from app.services.tech_sheet_extraction import _kv_to_product_characteristics
from app.models import InboundInvoice, InvoiceExtractionItem, TechSheet
from app.services.invoice_extraction import extract_line_items


def _offer(
    *,
    supplier_id: int,
    supplier_name: str,
    score: float | None,
    gpt_score: float | None,
) -> OfferEntry:
    return OfferEntry(
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        currency="DZD",
        unit_price=100.0,
        gpt_match_rate=gpt_score,
        tech_sheet_similarity_display_score=score,
    )


def test_attachment_characteristics_normalization_merges_and_dedupes():
    out = _normalize_characteristics_payload(
        {
            "Capacite": ["24 litres", "24 litres"],
            "Classe": ["B"],
        },
        {},
    )
    assert out == [
        {"key": "capacite", "values": ["24 litres"]},
        {"key": "classe", "values": ["B"]},
    ]


def test_invoice_components_normalization_maps_aliases_and_flattens():
    out = _normalize_invoice_components_payload(
        {
            "caracteristiques": [{"key": "Capacite", "value": "24 L"}],
            "exigences": ["Classe B", "Classe B"],
            "accessoires_structures": [{"item": "Panier"}],
            "description": ["Autoclave de paillasse"],
        }
    )
    assert out["characteristics"] == ["capacite: 24 L"]
    assert out["requirements"] == ["Classe B"]
    assert out["accessories"] == ["Panier"]
    assert out["description"] == "Autoclave de paillasse"


def test_tech_sheet_kv_to_characteristics_keeps_attachment_like_shape():
    out = _kv_to_product_characteristics(
        {
            "Capacite": "24 L",
            "Classe": ["B"],
        }
    )
    assert out == [
        {"key": "capacite", "values": ["24 L"]},
        {"key": "classe", "values": ["B"]},
    ]


def test_weighted_similarity_ignores_empty_zero_zero_blocks():
    score = _weighted_similarity_from_components(
        {
            "characteristics": {"score": 1.0, "total": 1},
            "requirements": {"score": 0.0, "total": 0},
            "accessories": {"score": 0.0, "total": 0},
            "description": {"score": None, "total": 0},
        }
    )
    assert score == 1.0


def test_component_coverage_uses_matched_over_total_ratio():
    components = {
        "characteristics": {"matched": 9, "total": 10, "coverage": 0.8123},
        "requirements": {"matched": 3, "total": 5, "coverage": 0.9999},
        "accessories": {"matched": 0, "total": 0, "coverage": 0.5},
    }
    _enforce_binary_component_coverage(components)
    assert components["characteristics"]["coverage"] == 0.9
    assert components["requirements"]["coverage"] == 0.6
    assert components["accessories"]["coverage"] is None


def test_display_score_uses_matched_total_and_blends_gpt_30_percent():
    breakdown = {
        "components": {
            "characteristics": {"matched": 9, "total": 10, "coverage": 0.1111, "score": 0.1111},
            "requirements": {"matched": 3, "total": 5, "coverage": 0.9999, "score": 0.9999},
            "accessories": {"matched": 0, "total": 0, "coverage": 0.9, "score": 0.9},
            "description": {"score": 0.5, "matched": 0, "total": 1},
        }
    }
    score, status = _compute_tech_sheet_similarity_display(
        raw_score=0.02,
        raw_status="faible",
        link_score=None,
        breakdown=breakdown,
        gpt_match_score=0.2,
    )
    # tech_ratio = (0.45*0.9 + 0.25*0.6 + 0.1*0.5) / (0.45+0.25+0.1) = 0.75625
    # final = 0.70*tech_ratio + 0.30*0.2 = 0.589375 -> 0.5894
    assert score == 0.5894
    assert status == "faible"


def test_display_score_reduces_penalty_for_missing_req_acc_evidence():
    breakdown = {
        "offered_counts": {
            "characteristics": 12,
            "requirements": 0,
            "accessories": 0,
            "description": 1,
        },
        "components": {
            "characteristics": {"matched": 8, "total": 10, "coverage": 0.8, "score": 0.8},
            "requirements": {"matched": 0, "total": 10, "coverage": 0.0, "score": 0.0},
            "accessories": {"matched": 0, "total": 10, "coverage": 0.0, "score": 0.0},
            "description": {"score": 0.6, "matched": 1, "total": 1},
        },
    }
    score, status = _compute_tech_sheet_similarity_display(
        raw_score=0.10,
        raw_status="faible",
        link_score=None,
        breakdown=breakdown,
        gpt_match_score=0.50,
    )
    # tech (with missing-evidence attenuation): 0.5936
    # final display = 0.70*0.5936 + 0.30*0.50 = 0.5655
    assert score == 0.5655
    assert status == "faible"


def test_results_payload_filters_offers_below_gpt_threshold():
    rows = [
        ProductOffers(
            product_id=1,
            product_name="AUTOCLAVE",
            offers=[
                _offer(supplier_id=1, supplier_name="A", score=0.31, gpt_score=0.72),
                _offer(supplier_id=2, supplier_name="B", score=0.91, gpt_score=0.31),
            ],
            best_offer=_offer(supplier_id=1, supplier_name="A", score=0.31, gpt_score=0.72),
        ),
        ProductOffers(
            product_id=2,
            product_name="COMPRESSEUR",
            offers=[_offer(supplier_id=3, supplier_name="C", score=0.99, gpt_score=0.20)],
            best_offer=_offer(supplier_id=3, supplier_name="C", score=0.99, gpt_score=0.20),
        ),
    ]

    payload = _serialize_results_payload(
        rows,
        useful_only=True,
        min_match_score=0.50,
    )

    assert len(payload) == 1
    assert payload[0]["product_name"] == "AUTOCLAVE"
    assert len(payload[0]["offers"]) == 1
    assert payload[0]["offers"][0]["supplier_name"] == "A"


def test_tech_sheet_link_group_keys_require_sender_email():
    invoice = InboundInvoice(tenant_id=1, sender_email=None)
    item = InvoiceExtractionItem(tenant_id=1, campaign_id=1, invoice_id=1, product_name="AUTOCLAVE")
    sheet = TechSheet(tenant_id=1, campaign_id=1, sender_email=None, file_name="fiche.pdf")

    assert _item_group_key(item, invoice) is None
    assert _sheet_group_key(sheet) is None

    invoice.sender_email = "supplier@example.com"
    sheet.sender_email = "supplier@example.com"
    assert _item_group_key(item, invoice) == "email:supplier@example.com"
    assert _sheet_group_key(sheet) == "email:supplier@example.com"


def test_extract_line_items_supports_xlsx_invoice_rows(tmp_path):
    from openpyxl import Workbook

    xlsx_path = tmp_path / "invoice.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "listing"
    ws.append(["", "produit", "Ref", "prix HT", "3 M"])
    ws.append(["", "ACB-Albumine", "2*100T", 1201.82, 961.456])
    ws.append(["", "ACB-Bilirubine", "2*100 ML 65 T", 2007.15, 1605.72])
    wb.save(xlsx_path)

    invoice = InboundInvoice(
        id=1,
        tenant_id=1,
        campaign_id=1,
        file_name="invoice.xlsx",
        file_path=str(xlsx_path),
        sender_email="supplier@example.com",
    )
    items = extract_line_items(invoice, db=None)

    assert len(items) == 2
    assert all(row.source_engine == "xlsx_parser" for row in items)
    assert all((row.unit_price or 0) > 0 for row in items)
    # Rightmost price-like column ("3 M") should be used as effective unit price.
    assert items[0].unit_price == 961.456
    assert invoice.parse_engine == "xlsx_parser"

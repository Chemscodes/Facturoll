from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db import SessionLocal
from app.models import Campaign, User
from app.routes.campaigns import _offers_by_product


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except Exception:
        return None


def _close(a: float, b: float, rel_tol: float = 1e-6) -> bool:
    denom = max(abs(a), abs(b), 1.0)
    return abs(a - b) / denom <= rel_tol


def _extract_weight_g(text: str) -> float | None:
    if not text:
        return None
    # Weight only when explicit unit token exists (mg/g/kg)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(mg|gr|g|kg)\b", text, re.IGNORECASE)
    if not m:
        return None
    value = _to_float(m.group(1))
    unit = _norm(m.group(2))
    if value is None:
        return None
    if unit == "kg":
        return value * 1000.0
    if unit == "mg":
        return value / 1000.0
    return value


def _extract_volume_ml(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|cl|l)\b", text, re.IGNORECASE)
    if not m:
        return None
    value = _to_float(m.group(1))
    unit = _norm(m.group(2))
    if value is None:
        return None
    if unit == "l":
        return value * 1000.0
    if unit == "cl":
        return value * 10.0
    return value


def _extract_packaging_count(text: str) -> int | None:
    if not text:
        return None
    value = _norm(text)

    # Example: 2 boites de 50
    m = re.search(
        r"\b(\d{1,4})\s*(?:boites?|box|packs?|paquets?|cartons?|sachets?)\s*(?:de)?\s*(\d{1,5})\b",
        value,
    )
    if m:
        try:
            return int(m.group(1)) * int(m.group(2))
        except Exception:
            pass

    # Examples: bt100, b/100, boite 100, pack 50
    m = re.search(r"\b(?:bt|bte|b|boite|box|pack|paquet|carton|sachet)\s*/?\s*(\d{1,5})\b", value)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass

    return None


def _extract_dimensions(text: str) -> tuple[list[float], str | None] | None:
    if not text:
        return None
    value = _norm(text).replace(",", ".")
    # Grab first vector-like pattern: 5x5 or 10*20*30, optional one unit token
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*[*x]\s*(\d+(?:\.\d+)?)(?:\s*[*x]\s*(\d+(?:\.\d+)?))?\s*(mm|cm|m)?\b",
        value,
    )
    if not m:
        return None
    dims: list[float] = []
    for idx in (1, 2, 3):
        if m.group(idx):
            n = _to_float(m.group(idx))
            if n is not None:
                dims.append(n)
    if not dims:
        return None
    unit = _norm(m.group(4))
    if unit == "m":
        dims = [d * 1000.0 for d in dims]  # canonical mm
        return dims, "mm"
    if unit == "cm":
        dims = [d * 10.0 for d in dims]  # canonical mm
        return dims, "mm"
    if unit == "mm":
        return dims, "mm"
    return dims, None


def _extract_commercial_form(text: str) -> str | None:
    value = _norm(text)
    if not value:
        return None
    forms = [
        "rouleau",
        "flacon",
        "boite",
        "box",
        "pack",
        "sachet",
        "unite",
        "piece",
        "pcs",
    ]
    for token in forms:
        if re.search(rf"\b{re.escape(token)}\b", value):
            return token
    return None


@dataclass
class SpecSignals:
    weight_g: float | None
    volume_ml: float | None
    dimensions: tuple[list[float], str | None] | None
    packaging_count: int | None
    commercial_form: str | None


def _build_signals(text: str | None) -> SpecSignals:
    line = str(text or "")
    return SpecSignals(
        weight_g=_extract_weight_g(line),
        volume_ml=_extract_volume_ml(line),
        dimensions=_extract_dimensions(line),
        packaging_count=_extract_packaging_count(line),
        commercial_form=_extract_commercial_form(line),
    )


def _status_same_diff_convertible(
    offered: float | None,
    requested: float | None,
    *,
    convertible_family: bool,
) -> str:
    if offered is None and requested is None:
        return "not_applicable"
    if offered is None or requested is None:
        return "missing_one_side"
    if _close(float(offered), float(requested)):
        return "same"
    if convertible_family:
        return "convertible"
    return "different"


def _status_packaging(offered: int | None, requested: int | None) -> str:
    if offered is None and requested is None:
        return "not_applicable"
    if offered is None or requested is None:
        return "missing_one_side"
    if int(offered) == int(requested):
        return "same"
    return "convertible"


def _status_dimensions(
    offered: tuple[list[float], str | None] | None,
    requested: tuple[list[float], str | None] | None,
) -> str:
    if offered is None and requested is None:
        return "not_applicable"
    if offered is None or requested is None:
        return "missing_one_side"
    offered_dims, offered_unit = offered
    requested_dims, requested_unit = requested
    if len(offered_dims) != len(requested_dims):
        return "different"
    # If units absent on both sides, compare raw values directly.
    # If one side has explicit unit and the other does not, keep as uncertain.
    if bool(offered_unit) != bool(requested_unit):
        return "missing_one_side"
    for left, right in zip(offered_dims, requested_dims):
        if not _close(float(left), float(right)):
            return "different"
    return "same"


def _status_form(offered: str | None, requested: str | None) -> str:
    if offered is None and requested is None:
        return "not_applicable"
    if offered is None or requested is None:
        return "unknown"
    if offered == requested:
        return "same"
    return "different"


def _build_decision_payload(
    *,
    strict_score: float | None,
    strict_threshold: float | None,
    strict_decision: str | None,
    requested_name: str,
    offered_name: str,
    match_badges: list[str] | None,
) -> dict[str, Any]:
    threshold = float(strict_threshold or 0.0)
    score = float(strict_score) if strict_score is not None else None
    family_match = bool(strict_decision == "matched" and score is not None and score >= threshold)
    family_reason = "same_product_family" if family_match else "different_or_uncertain_family"

    requested_signals = _build_signals(requested_name)
    offered_signals = _build_signals(offered_name)

    weight_status = _status_same_diff_convertible(
        offered_signals.weight_g, requested_signals.weight_g, convertible_family=False
    )
    volume_status = _status_same_diff_convertible(
        offered_signals.volume_ml, requested_signals.volume_ml, convertible_family=True
    )
    dimension_status = _status_dimensions(offered_signals.dimensions, requested_signals.dimensions)
    packaging_status = _status_packaging(
        offered_signals.packaging_count, requested_signals.packaging_count
    )
    commercial_form_status = _status_form(
        offered_signals.commercial_form, requested_signals.commercial_form
    )

    statuses = {
        "weight_status": weight_status,
        "volume_status": volume_status,
        "dimension_status": dimension_status,
        "packaging_status": packaging_status,
        "commercial_form_status": commercial_form_status,
    }

    convertible_components = []
    if volume_status == "convertible":
        convertible_components.append("volume")
    if packaging_status == "convertible":
        convertible_components.append("packaging")

    has_blocking_diff = any(
        statuses[k] == "different" for k in ("weight_status", "dimension_status", "commercial_form_status")
    )
    has_uncertain = any(
        statuses[k] in {"missing_one_side", "unknown"}
        for k in ("weight_status", "volume_status", "dimension_status", "packaging_status", "commercial_form_status")
    )
    all_same_or_na = all(v in {"same", "not_applicable"} for v in statuses.values())

    conversion_allowed = bool(family_match and not has_blocking_diff and convertible_components)
    conversion_mode = "none"
    conversion_factor = None
    conversion_basis = None
    if conversion_allowed:
        conversion_mode = "mixed" if len(convertible_components) > 1 else convertible_components[0]
        # Derive a stable factor if exactly one convertible dimension is fully known.
        if conversion_mode == "volume":
            if offered_signals.volume_ml and requested_signals.volume_ml and offered_signals.volume_ml > 0:
                conversion_factor = requested_signals.volume_ml / offered_signals.volume_ml
                conversion_basis = "volume_ratio_ml"
        elif conversion_mode == "packaging":
            if offered_signals.packaging_count and requested_signals.packaging_count and offered_signals.packaging_count > 0:
                conversion_factor = requested_signals.packaging_count / offered_signals.packaging_count
                conversion_basis = "packaging_count_ratio"

    if not family_match:
        final_decision = "NO_MATCH"
        decision_reason = "family_mismatch"
    elif all_same_or_na:
        final_decision = "EXACT_MATCH"
        decision_reason = "all_specs_same_or_not_applicable"
    elif has_blocking_diff:
        final_decision = "MATCH_SAME_PRODUCT_DIFFERENT_SPEC"
        decision_reason = "blocking_spec_difference"
    elif conversion_allowed:
        final_decision = "MATCH_WITH_CONVERSION"
        decision_reason = f"convertible_{conversion_mode}"
    elif has_uncertain:
        final_decision = "MATCH_UNCERTAIN"
        decision_reason = "missing_or_unknown_spec_info"
    else:
        final_decision = "MATCH_SAME_PRODUCT_DIFFERENT_SPEC"
        decision_reason = "non_convertible_spec_difference"

    alerts: list[str] = []
    if final_decision == "EXACT_MATCH":
        alerts.append("EXACT")
    elif final_decision == "MATCH_WITH_CONVERSION":
        alerts.append("CONVERSION")
    elif final_decision == "MATCH_SAME_PRODUCT_DIFFERENT_SPEC":
        alerts.append("SPEC_DIFF")
    elif final_decision == "MATCH_UNCERTAIN":
        alerts.append("UNCERTAIN_SPEC")
    elif final_decision == "NO_MATCH":
        alerts.append("NO_MATCH")

    existing_badges = set(match_badges or [])
    if "SANS_UNITE" in existing_badges:
        alerts.append("NO_UNIT")
    if "UNITE_DEVINEE" in existing_badges:
        alerts.append("UNIT_INFERRED")
    if "TVA_GLOBALE" in existing_badges:
        alerts.append("GLOBAL_VAT")
    if "QTY_CORRIGEE" in existing_badges:
        alerts.append("QTY_FIXED")
    if "PRIX_CORRIGE" in existing_badges:
        alerts.append("PRICE_FIXED")

    out = {
        "name_match_score": score,
        "name_match_threshold": threshold if threshold > 0 else None,
        "name_match_decision": strict_decision,
        "family_match": family_match,
        "family_reason": family_reason,
        "spec_comparison": statuses,
        "conversion": {
            "allowed": conversion_allowed,
            "mode": conversion_mode,
            "factor": round(float(conversion_factor), 6) if conversion_factor is not None else None,
            "basis": conversion_basis,
        },
        "final_decision": final_decision,
        "decision_reason": decision_reason,
        "alerts": alerts,
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run new strict comparison logic without DB writes.")
    parser.add_argument("--campaign-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--only-matched", action="store_true", default=False)
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        camp = db.query(Campaign).filter(Campaign.id == int(args.campaign_id)).first()
        if not camp:
            print("CAMPAIGN_NOT_FOUND")
            return 1
        user = db.query(User).filter(User.tenant_id == camp.tenant_id).order_by(User.id.asc()).first()
        if not user:
            print("USER_NOT_FOUND")
            return 1

        rows = _offers_by_product(int(args.campaign_id), db, user, limit=int(args.limit), conform_only=False)
        output: list[dict[str, Any]] = []

        print(
            "item_id | strict_score | decision | requested | offered | final_decision | "
            "weight | volume | dimension | packaging | form | conversion_mode | factor | alerts"
        )
        print("-" * 280)

        for product_row in rows:
            requested_name = str(product_row.product_name or "").strip()
            for offer in product_row.offers:
                item_id = getattr(offer, "invoice_item_id", None)
                if item_id is None:
                    continue
                strict_decision = str(getattr(offer, "strict_match_decision", "") or "").strip() or None
                if args.only_matched and strict_decision != "matched":
                    continue
                offered_name = str(getattr(offer, "extracted_name", "") or "").strip()
                strict_score = getattr(offer, "strict_match_score", None)
                strict_threshold = getattr(offer, "strict_match_threshold", None)
                match_badges = getattr(offer, "match_badges", None)

                decision_payload = _build_decision_payload(
                    strict_score=strict_score,
                    strict_threshold=strict_threshold,
                    strict_decision=strict_decision,
                    requested_name=requested_name,
                    offered_name=offered_name,
                    match_badges=match_badges if isinstance(match_badges, list) else [],
                )

                spec = decision_payload["spec_comparison"]
                conv = decision_payload["conversion"]
                print(
                    f"{item_id} | {strict_score} | {strict_decision} | {requested_name} | {offered_name} | "
                    f"{decision_payload['final_decision']} | {spec['weight_status']} | {spec['volume_status']} | "
                    f"{spec['dimension_status']} | {spec['packaging_status']} | {spec['commercial_form_status']} | "
                    f"{conv['mode']} | {conv['factor']} | {decision_payload['alerts']}"
                )

                output.append(
                    {
                        "invoice_item_id": item_id,
                        "requested_name": requested_name,
                        "offered_name": offered_name,
                        "strict_match_score": strict_score,
                        "strict_match_threshold": strict_threshold,
                        "strict_match_decision": strict_decision,
                        "existing_match_badges": match_badges,
                        "new_logic": decision_payload,
                    }
                )

        out_path = args.out.strip() or f"/tmp/strict_new_logic_campaign_{int(args.campaign_id)}.json"
        Path(out_path).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print("-" * 280)
        print(f"TOTAL_ROWS={len(output)}")
        print(f"JSON_SAVED={out_path}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

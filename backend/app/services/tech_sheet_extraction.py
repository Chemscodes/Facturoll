from __future__ import annotations

import re
import unicodedata
import json
import time
from pathlib import Path
from typing import Any

import httpx

from app.services.document_ai import extract_full_text
from app import config
from app.services.openai_embeddings import cosine_similarity, fetch_embeddings, get_cached_embeddings
from app.services.openai_observability import record_openai_api_call
from app.db import SessionLocal
from app.models import EmbeddingCache


_UNIT_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>kw|w|v|kg|g|mg|l|litre|litres|mm|cm|m|bar|bars|hz|Â°c|c|a)\b",
    re.IGNORECASE,
)
_RANGE_UNIT_RE = re.compile(
    r"(?P<from>\d+(?:[.,]\d+)?)\s*(?:[-â€“]|/)\s*(?P<to>\d+(?:[.,]\d+)?)\s*(?P<unit>kw|w|v|kg|g|mg|l|litre|litres|mm|cm|m|bar|bars|hz|Â°c|c|a)\b",
    re.IGNORECASE,
)
_DIM_RE = re.compile(r"(\d+\s*[xÃ—]\s*\d+(?:\s*[xÃ—]\s*\d+)?)\s*(mm|cm|m)\b", re.IGNORECASE)
_CLASS_RE = re.compile(r"\bclasse\s*([A-Z])\b", re.IGNORECASE)
_GAUGE_RE = re.compile(r"\b(?P<num>\d{1,2}(?:[.,]\d+)?)\s*g\b", re.IGNORECASE)
_FR_CH_RE = re.compile(r"\b(?P<num>\d{1,3}(?:[.,]\d+)?)\s*(?P<unit>fr|ch)\b", re.IGNORECASE)
_PACK_RE = re.compile(
    r"(?<!\d\s)\b(?:x|Ã—)\s*(?P<xqty>\d{1,5})(?!\s*[xÃ—*]\s*\d)\b|"
    r"\b(?:boite|boÃ®te|sachet|blister|pack|lot|carton)\s*(?:de)?\s*(?P<pqty>\d{1,5})\b",
    re.IGNORECASE,
)
_REF_RE = re.compile(
    r"\b(?:ref(?:erence)?|reference|code(?:\s+article|\s+produit)?|sku|catalogue)\b\s*[:#\-]?\s*"
    r"(?P<val>[A-Za-z0-9][A-Za-z0-9_\-./]{1,80})\b",
    re.IGNORECASE,
)
_NORM_TOKEN_RE = re.compile(
    r"\b(?:iso\s*\d{3,5}(?:-\d+)?|en\s*\d{3,5}(?:-\d+)?|ce\s*\d{2,5}|ped\s*20\d{2}/\d{2}/[a-z]{2})\b",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"\b\d{1,3}(?:[.,]\d+)?\s*%")
_TIME_RE = re.compile(r"\b\d{1,4}(?:[.,]\d+)?\s*(?:s|min|mn|h)\b", re.IGNORECASE)
_CYCLE_COUNT_RE = re.compile(r"\b\d{1,3}\s*cycles?\b", re.IGNORECASE)
_CHARGE_RE = re.compile(
    r"\b(?:charge|charge standard|payload)\s*(?:max)?\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(kg|g|l|litre|litres)\b",
    re.IGNORECASE,
)
_INVOICE_LINE_KEYWORDS = {
    "date",
    "facture",
    "facture n",
    "proforma",
    "nif",
    "nis",
    "rc",
    "rib",
    "tel",
    "tél",
    "tva",
    "ttc",
    "ht",
    "total",
    "prix unitaire",
    "quantite",
    "qte",
    "client",
    "fournisseur",
    "adresse",
}
_TECH_EMBED_PROMPTS = {
    "capacite": "capacite volume litre ml cuve volume utile",
    "classe": "classe b classe n classe s class medicale",
    "puissance": "puissance watt kw consommation electrique",
    "tension": "tension voltage alimentation electrique v",
    "frequence": "frequence hz alimentation 50 60 hz",
    "pression": "pression bar pression maximale",
    "temperature": "temperature plage thermique celsius",
    "dimensions": "dimensions taille longueur largeur hauteur mm cm",
    "poids": "poids kilogramme gramme",
    "portes": "portes simple porte double porte",
    "marque": "marque brand fabricant",
    "type": "type modele model",
    "origine": "origine pays de fabrication made in",
    "normes": "normes et conformite iso en ce ped certification",
    "cycle": "cycle de sterilisation nombre de cycles programmes",
    "temps_cycle": "duree temps de cycle en minute secondes",
    "charge": "charge maximale capacite de charge en kg",
    "usage": "usage destination application dispositif medical",
    "matiere": "materiau acier inox plastique latex silicone",
    "sterilite": "sterile non sterile autoclavable eo gamma usage unique",
    "conditionnement": "conditionnement boite sachet blister pack lot carton",
    "reference": "reference ref sku code article code produit",
    "taille": "taille gauge calibre diametre fr ch",
}

_KEY_SYNONYMS = {
    "capacite": ["capacite", "capacitÃ©", "volume utile", "capacitÃ© utile", "volume"],
    "classe": ["classe"],
    "puissance": ["puissance", "energie", "Ã©nergie", "consommation", "power"],
    "tension": ["tension", "voltage", "alimentation"],
    "dimensions": ["dimensions", "dimension", "dim", "dim utile", "dim exterieur", "dim extÃ©rieur"],
    "dimensions_scellement": [
        "dimensions du scellement",
        "dimensions scellement",
        "scellement",
        "soudure",
        "zone de soudure",
        "largeur de soudure",
        "largeur de la zone de soudure",
        "largeur zone de soudure",
        "largeur de la zone",
    ],
    "poids": ["poids", "weight"],
    "pression": ["pression", "bar", "bars"],
    "frequence": ["frequence", "frÃ©quence", "hz"],
    "temperature": ["temperature", "temp", "Â°c"],
    "portes": ["porte", "portes"],
    "marque": ["marque", "brand"],
    "type": ["type", "modele", "modÃ¨le"],
    "origine": ["origine", "pays"],
    "normes": ["normes", "norme", "conformite", "certification", "iso", "en", "ce", "ped"],
    "cycle": ["cycle", "cycles", "programme", "programmes"],
    "temps_cycle": ["temps de cycle", "duree de cycle", "durée de cycle", "cycle rapide", "mn", "minutes"],
    "charge": ["charge", "charge standard", "charge max", "payload"],
    "matiere": [
        "matiere",
        "matiere principale",
        "materiau",
        "material",
        "acier",
        "inox",
        "acier inoxydable",
        "stainless steel",
        "plastique",
        "silicone",
        "latex",
        "pvc",
        "polypropylene",
        "polyethylene",
        "polyurethane",
        "coton",
        "non tisse",
    ],
    "sterilite": [
        "sterile",
        "sterility",
        "sterilite",
        "non sterile",
        "usage unique",
        "single use",
        "jetable",
        "disposable",
        "autoclavable",
        "sterilisation",
        "eo",
        "gamma",
    ],
    "conditionnement": [
        "conditionnement",
        "packaging",
        "boite",
        "sachet",
        "blister",
        "pack",
        "lot",
        "carton",
    ],
    "reference": ["reference", "ref", "code article", "code produit", "catalogue", "sku"],
    "taille": ["taille", "size", "diametre", "gauge", "calibre", "epaisseur"],
    "usage": ["usage", "utilisation", "application", "destination"],
}

_TEXTUAL_KEYS = {"matiere", "sterilite", "conditionnement", "reference", "taille", "usage"}
_GENERIC_TEXT_VALUES = {
    "caracteristiques techniques",
    "specifications techniques",
    "fiche technique",
    "brochure",
}
_GAUGE_CONTEXT = {
    "aiguille",
    "needle",
    "seringue",
    "catheter",
    "cath",
    "canule",
    "sonde",
    "gauge",
}
_MATERIAL_TERMS = [
    "acier inoxydable",
    "stainless steel",
    "inox",
    "acier",
    "plastique",
    "silicone",
    "latex",
    "pvc",
    "polypropylene",
    "polyethylene",
    "polyurethane",
    "coton",
    "non tisse",
]
_STERILITY_TERMS = [
    "non sterile",
    "sterile",
    "usage unique",
    "single use",
    "jetable",
    "disposable",
    "autoclavable",
    "sterilisation",
    "eo",
    "gamma",
]

_FEATURE_TERMS = [
    "carte sd",
    "tracabilite",
    "traçabilite",
    "generateur vapeur",
    "générateur vapeur",
    "double porte",
    "double portes",
    "sans huile",
    "compact",
    "professionnel",
    "professionnelle",
    "avec humidificateur",
    "avec humidificateur",
]

_CUSTOM_REGEX_RULES: list[dict[str, Any]] = []
_TECH_SHEET_LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "doc_type_guess": {
            "type": "string",
            "enum": ["tech_sheet", "invoice", "unknown"],
        },
        "characteristics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["key", "value", "confidence"],
            },
        },
    },
    "required": ["doc_type_guess", "characteristics"],
}


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _norm(text: str) -> str:
    return " ".join(_strip_accents(text).lower().split())


def _canonical_key(raw_key: str) -> str:
    key_norm = _norm(raw_key)
    if "scellement" in key_norm or "soudure" in key_norm:
        return "dimensions_scellement"
    for canonical, synonyms in _KEY_SYNONYMS.items():
        for syn in synonyms:
            syn_norm = _norm(syn)
            if syn_norm == key_norm:
                return canonical
            if len(syn_norm) <= 3:
                if re.search(rf"(?<!\w){re.escape(syn_norm)}(?!\w)", key_norm):
                    return canonical
            elif syn_norm in key_norm:
                return canonical
    return key_norm


def _load_custom_characteristics_rules() -> dict[str, Any]:
    path_value = str(getattr(config, "CUSTOM_CHARACTERISTICS_RULES_PATH", "") or "").strip()
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _register_custom_characteristics_rules() -> None:
    global _CUSTOM_REGEX_RULES
    payload = _load_custom_characteristics_rules()
    if not payload:
        return

    raw_synonyms = payload.get("key_synonyms")
    if isinstance(raw_synonyms, dict):
        for raw_key, raw_values in raw_synonyms.items():
            key = _canonical_key(str(raw_key))
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            if not values:
                continue
            bucket = _KEY_SYNONYMS.setdefault(key, [])
            existing = {_norm(item) for item in bucket}
            for raw in values:
                text = str(raw or "").strip()
                if not text:
                    continue
                token = _norm(text)
                if token in existing:
                    continue
                bucket.append(text)
                existing.add(token)

    raw_features = payload.get("feature_terms")
    if isinstance(raw_features, list):
        existing = {_norm(item) for item in _FEATURE_TERMS}
        for raw in raw_features:
            text = str(raw or "").strip()
            if not text:
                continue
            token = _norm(text)
            if token in existing:
                continue
            _FEATURE_TERMS.append(text)
            existing.add(token)

    raw_prompts = payload.get("embedding_prompts")
    if isinstance(raw_prompts, dict):
        for raw_key, raw_prompt in raw_prompts.items():
            key = _canonical_key(str(raw_key or "").strip())
            prompt = str(raw_prompt or "").strip()
            if not key or not prompt:
                continue
            _TECH_EMBED_PROMPTS[key] = prompt

    compiled_rules: list[dict[str, Any]] = []
    raw_rules = payload.get("regex_rules")
    if isinstance(raw_rules, list):
        for idx, raw_rule in enumerate(raw_rules, start=1):
            if not isinstance(raw_rule, dict):
                continue
            key = _canonical_key(str(raw_rule.get("key") or "").strip())
            pattern = str(raw_rule.get("pattern") or "").strip()
            if not key or not pattern:
                continue
            flags = re.IGNORECASE if raw_rule.get("ignore_case", True) else 0
            try:
                regex = re.compile(pattern, flags)
            except re.error:
                continue
            compiled_rules.append(
                {
                    "name": str(raw_rule.get("name") or f"custom_rule_{idx}"),
                    "key": key,
                    "regex": regex,
                    "value_group": raw_rule.get("value_group"),
                    "value_template": raw_rule.get("value_template"),
                    "use_full_match": bool(raw_rule.get("use_full_match", False)),
                }
            )
    _CUSTOM_REGEX_RULES = compiled_rules


def _custom_rule_value(rule: dict[str, Any], match: re.Match[str]) -> str | None:
    template = rule.get("value_template")
    if isinstance(template, str) and template.strip():
        value = template
        value = value.replace("{0}", match.group(0))
        for i, group in enumerate(match.groups(), start=1):
            value = value.replace(f"{{{i}}}", str(group or ""))
        value = " ".join(value.split())
        return value or None

    group = rule.get("value_group")
    if isinstance(group, int):
        try:
            value = " ".join(str(match.group(group) or "").split())
            return value or None
        except Exception:
            return None
    if isinstance(group, str) and group.strip():
        try:
            value = " ".join(str(match.group(group) or "").split())
            return value or None
        except Exception:
            return None

    if rule.get("use_full_match"):
        value = " ".join(str(match.group(0) or "").split())
        return value or None

    groups = [str(g or "").strip() for g in match.groups() if str(g or "").strip()]
    if groups:
        return " ".join(groups)
    value = " ".join(str(match.group(0) or "").split())
    return value or None


_register_custom_characteristics_rules()


def _store_kv(kv: dict, key: str, value: str) -> None:
    if key in kv:
        existing = kv[key]
        if isinstance(existing, list):
            existing.append(value)
        else:
            kv[key] = [existing, value]
    else:
        kv[key] = value


def _dedupe_kv(kv: dict[str, str | list[str]]) -> None:
    for key, value in list(kv.items()):
        if isinstance(value, list):
            out: list[str] = []
            seen: set[str] = set()
            for raw in value:
                text = str(raw).strip()
                if not text:
                    continue
                token = _norm(text)
                if token in seen:
                    continue
                seen.add(token)
                out.append(text)
            if not out:
                del kv[key]
            elif len(out) == 1:
                kv[key] = out[0]
            else:
                kv[key] = out


def _contains_synonym(text_norm: str, synonym: str) -> bool:
    syn_norm = _norm(synonym)
    if not syn_norm:
        return False
    if len(syn_norm) <= 3:
        return re.search(rf"(?<!\w){re.escape(syn_norm)}(?!\w)", text_norm) is not None
    return syn_norm in text_norm


def _extract_text_features(line: str) -> list[str]:
    line_norm = _norm(line)
    if not line_norm:
        return []
    features: set[str] = set()

    # Capture common free-text feature chunks.
    for match in re.finditer(r"\b(avec|sans)\s+([a-z0-9°/%\- ]{3,80})", line_norm):
        chunk = f"{match.group(1)} {match.group(2)}".strip()
        # keep only short meaningful chunks
        chunk = " ".join(chunk.split()[:6])
        if len(chunk) >= 6:
            features.add(chunk)

    for term in _FEATURE_TERMS:
        if _contains_synonym(line_norm, term):
            features.add(_norm(term))

    return sorted(features)


def _looks_like_invoice_text(text: str) -> bool:
    if not text:
        return False
    low = _norm(text)
    hits = sum(1 for token in _INVOICE_LINE_KEYWORDS if token in low)
    has_table = ("designation" in low or "description" in low) and (
        "qte" in low or "quantite" in low or "prix unitaire" in low
    )
    return has_table or hits >= 5


def _extract_tech_sheet_with_openai_schema(text: str) -> dict[str, Any] | None:
    if not config.OPENAI_API_KEY:
        return None
    cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not cleaned:
        return None

    max_chars = max(
        8000,
        int(getattr(config, "OPENAI_TECH_SHEET_MAX_INPUT_CHARS", 50000) or 50000),
    )
    text_for_model = cleaned[:max_chars]
    model = str(
        getattr(
            config,
            "OPENAI_TECH_SHEET_MODEL",
            getattr(config, "OPENAI_ATTACHMENT_MODEL", "gpt-4.1-mini"),
        )
        or "gpt-4.1-mini"
    )
    timeout_s = max(
        30,
        int(getattr(config, "OPENAI_TECH_SHEET_TIMEOUT_SECONDS", 90) or 90),
    )

    system_prompt = (
        "Tu extrais uniquement des caracteristiques techniques d'un document produit. "
        "Retourne strictement le JSON schema demande. "
        "Ne pas inventer de valeurs. "
        "Si le document ressemble a une facture/devis, renvoie doc_type_guess=invoice."
    )
    user_prompt = (
        "Extrait les caracteristiques techniques sous forme key/value. "
        "Utilise des cles metier courtes (capacite, tension, frequence, puissance, dimensions, "
        "pression, temperature, normes, reference, matiere, sterilite, conditionnement, usage, etc.).\n\n"
        f"Document OCR:\n{text_for_model}"
    )
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "tech_sheet_characteristics",
                "strict": True,
                "schema": _TECH_SHEET_LLM_SCHEMA,
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        record_openai_api_call(
            operation="tech_sheet_llm_structured",
            endpoint="/v1/chat/completions",
            model=model,
            response_json=None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            success=False,
            error_type=type(exc).__name__,
            metadata={"max_input_chars": max_chars},
        )
        return None
    record_openai_api_call(
        operation="tech_sheet_llm_structured",
        endpoint="/v1/chat/completions",
        model=model,
        response_json=data if isinstance(data, dict) else None,
        latency_ms=int((time.perf_counter() - started) * 1000),
        success=True,
        metadata={"max_input_chars": max_chars},
    )

    try:
        message = ((data.get("choices") or [{}])[0].get("message") or {})
        content = message.get("content")
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                text_part = part.get("text")
                if isinstance(text_part, str) and text_part.strip():
                    chunks.append(text_part)
            content = "\n".join(chunks)
        if not isinstance(content, str) or not content.strip():
            return None
        parsed = json.loads(content)
    except Exception:
        return None

    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("characteristics"), list):
        return None
    return parsed


def _llm_characteristics_to_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    kv: dict[str, str | list[str]] = {}
    pairs: list[dict[str, Any]] = []
    confidence_values: list[float] = []

    for raw in parsed.get("characteristics") or []:
        if not isinstance(raw, dict):
            continue
        key_raw = str(raw.get("key") or "").strip()
        value_raw = str(raw.get("value") or "").strip()
        if not key_raw or not value_raw:
            continue
        key = _canonical_key(key_raw)
        _store_kv(kv, key, value_raw)
        conf = 0.0
        try:
            conf = float(raw.get("confidence") or 0.0)
        except Exception:
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        confidence_values.append(conf)
        pairs.append(
            {
                "key": key,
                "value": value_raw,
                "raw_line": "LLM",
                "score": round(conf, 4),
            }
        )

    _dedupe_kv(kv)
    avg_conf = (
        sum(confidence_values) / len(confidence_values)
        if confidence_values
        else 0.0
    )
    doc_type_guess = str(parsed.get("doc_type_guess") or "unknown").strip().lower()
    if doc_type_guess not in {"tech_sheet", "invoice", "unknown"}:
        doc_type_guess = "unknown"
    return {
        "kv": kv,
        "text_features": [],
        "pairs": pairs,
        "signals": sorted(kv.keys()),
        "unknown_numeric_lines": [],
        "score": round(avg_conf, 4),
        "score_regex_only": 0.0,
        "embedding_kv": {},
        "doc_type_guess": doc_type_guess,
    }


def _merge_tech_sheet_payloads(
    heuristic_payload: dict[str, Any],
    llm_payload: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(heuristic_payload)

    h_kv = merged.get("kv") if isinstance(merged.get("kv"), dict) else {}
    l_kv = llm_payload.get("kv") if isinstance(llm_payload.get("kv"), dict) else {}
    combined_kv: dict[str, str | list[str]] = {}

    for key in set(list(h_kv.keys()) + list(l_kv.keys())):
        left = h_kv.get(key)
        right = l_kv.get(key)
        values = _as_list_of_strings(left) + _as_list_of_strings(right)
        dedup = _as_list_of_strings(values)
        if not dedup:
            continue
        combined_kv[key] = dedup[0] if len(dedup) == 1 else dedup

    merged_pairs: list[dict[str, Any]] = []
    seen_pairs: set[str] = set()
    for pair in (heuristic_payload.get("pairs") or []) + (llm_payload.get("pairs") or []):
        if not isinstance(pair, dict):
            continue
        token = _norm(json.dumps(pair, ensure_ascii=False, sort_keys=True))
        if not token or token in seen_pairs:
            continue
        seen_pairs.add(token)
        merged_pairs.append(pair)

    merged_signals = sorted(
        {
            *(_as_list_of_strings(heuristic_payload.get("signals"))),
            *(_as_list_of_strings(llm_payload.get("signals"))),
        }
    )
    merged_unknown = sorted(
        {
            *(_as_list_of_strings(heuristic_payload.get("unknown_numeric_lines"))),
            *(_as_list_of_strings(llm_payload.get("unknown_numeric_lines"))),
        }
    )
    merged_text_features = sorted(
        {
            *(_as_list_of_strings(heuristic_payload.get("text_features"))),
            *(_as_list_of_strings(llm_payload.get("text_features"))),
        }
    )

    h_score = float(heuristic_payload.get("score") or 0.0)
    l_score = float(llm_payload.get("score") or 0.0)
    if merged_signals:
        coverage_score = float(len(combined_kv)) / max(len(merged_signals), 1)
        score = max(h_score, l_score, coverage_score)
    else:
        score = max(h_score, l_score)
    score = max(0.0, min(1.0, score))

    doc_type_guess = str(
        llm_payload.get("doc_type_guess")
        or heuristic_payload.get("doc_type_guess")
        or "unknown"
    ).strip().lower()
    if doc_type_guess not in {"tech_sheet", "invoice", "unknown"}:
        doc_type_guess = "unknown"

    merged["kv"] = combined_kv
    merged["pairs"] = merged_pairs
    merged["signals"] = merged_signals
    merged["unknown_numeric_lines"] = merged_unknown
    merged["text_features"] = merged_text_features
    merged["score"] = round(score, 4)
    merged["doc_type_guess"] = doc_type_guess
    return merged


def _extract_tech_sheet_characteristics(text: str) -> tuple[dict[str, Any], float, str]:
    mode = str(getattr(config, "OPENAI_TECH_SHEET_EXTRACTION_MODE", "heuristic") or "heuristic").lower()
    if mode in {"hybrid", "mix", "heuristic_plus_llm"}:
        heuristic_payload = parse_characteristics(text)
        parsed = _extract_tech_sheet_with_openai_schema(text)
        if parsed:
            llm_payload = _llm_characteristics_to_payload(parsed)
            if llm_payload.get("kv"):
                merged = _merge_tech_sheet_payloads(heuristic_payload, llm_payload)
                score = float(merged.get("score") or 0.0)
                return merged, score, "hybrid"
        score = float(heuristic_payload.get("score") or 0.0)
        return heuristic_payload, score, "heuristic"

    if mode in {"llm", "llm_structured", "ai", "llm_first"}:
        parsed = _extract_tech_sheet_with_openai_schema(text)
        if parsed:
            payload = _llm_characteristics_to_payload(parsed)
            if payload.get("kv"):
                score = float(payload.get("score") or 0.0)
                return payload, score, "openai_llm"
    characteristics = parse_characteristics(text)
    score = float(characteristics.get("score", 0.0))
    return characteristics, score, "heuristic"


def _as_list_of_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif value is None:
        items = []
    else:
        items = [value]
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        text = str(raw or "").strip()
        if not text:
            continue
        token = _norm(text)
        if token in seen:
            continue
        seen.add(token)
        out.append(text)
    return out


def _kv_to_product_characteristics(kv: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw_key, raw_value in kv.items():
        key = _canonical_key(str(raw_key or "").strip())
        if not key:
            continue
        values = _as_list_of_strings(raw_value)
        if not values:
            continue
        out.append({"key": key, "values": values})
    out.sort(key=lambda row: str(row.get("key") or ""))
    return out


def _looks_like_title_line(line: str) -> bool:
    text = " ".join(line.split()).strip()
    if not text:
        return False
    if len(text) < 6:
        return False
    if len(text) > 170:
        return False
    if ":" in text:
        return False
    if re.fullmatch(r"[\d\s.,;:/\-()]+", text):
        return False
    lower = _norm(text)
    if any(token in lower for token in _INVOICE_LINE_KEYWORDS):
        return False
    generic_title_fragments = {
        "classification",
        "caracteristiques",
        "caracteristiques techniques",
        "fonctionnalites",
        "largeur de soudure",
        "type et volume",
        "dimensions",
        "tension",
        "puissance",
        "frequence",
        "pression",
        "temperature",
        "normes",
        "conformite",
    }
    if any(fragment in lower for fragment in generic_title_fragments):
        return False
    # Avoid company address/footer lines.
    if (
        "www." in lower
        or "@" in lower
        or "http" in lower
        or " via " in f" {lower} "
        or " italy" in f" {lower} "
        or " vicenza" in lower
    ):
        return False
    if text.count(",") >= 2:
        return False
    words = text.split()
    if len(words) < 2 or len(words) > 18:
        return False
    return True


def _is_good_name_candidate(candidate: str) -> bool:
    text = candidate.strip()
    if not text:
        return False
    if len(text) < 2 or len(text) > 120:
        return False
    if text.endswith(":"):
        return False
    if re.fullmatch(r"[\d\s.,;:/\-()]+", text):
        return False
    cand_norm = _norm(text)
    generic_fragments = {
        "classification",
        "caracteristiques",
        "caracteristiques techniques",
        "fonctionnalites",
        "type et volume",
        "largeur de soudure",
        "dimensions",
        "cycle",
        "tension",
        "puissance",
    }
    if any(fragment in cand_norm for fragment in generic_fragments):
        return False
    if "largeur" in cand_norm and "soudure" in cand_norm:
        return False
    if re.search(r"\b(?:iso|en|ce|ped|eec|directive)\b", cand_norm):
        return False
    return True


def _contains_measurement_token(text: str) -> bool:
    return re.search(
        r"\b\d+(?:[.,]\d+)?\s*(?:kw|w|v|hz|bar|bars|kg|g|l|litre|litres|mm|cm|m|a)\b",
        text,
        re.IGNORECASE,
    ) is not None


def _first_good_kv_value(kv: dict[str, Any], key: str) -> str | None:
    values = _as_list_of_strings(kv.get(key))
    for value in values:
        candidate = value.strip()
        if not _is_good_name_candidate(candidate):
            continue
        cand_norm = _norm(candidate)
        words = [w for w in candidate.split() if w]
        if key == "marque":
            if len(words) > 4:
                continue
            if any(ch.isdigit() for ch in candidate):
                continue
            if "," in candidate:
                continue
        elif key == "reference":
            if len(words) > 6:
                continue
            if _contains_measurement_token(candidate):
                continue
        elif key == "type":
            if _contains_measurement_token(candidate):
                continue
            if " et " in cand_norm and len(words) <= 5:
                # Often section labels, not product names.
                continue
        return candidate
    return None


def _best_name_from_kv(kv: dict[str, Any]) -> str | None:
    if not isinstance(kv, dict):
        return None

    reference = _first_good_kv_value(kv, "reference")
    marque = _first_good_kv_value(kv, "marque")
    product_type = _first_good_kv_value(kv, "type")

    # Priority order:
    # 1) reference + marque
    # 2) reference only
    # 3) marque + type
    # 4) type only
    # 5) marque only
    if reference and marque:
        combined = f"{reference} - {marque}"
        if _is_good_name_candidate(combined):
            return combined
    if reference:
        return reference
    if marque and product_type:
        combined = f"{product_type} - {marque}"
        if _is_good_name_candidate(combined):
            return combined
    if product_type:
        return product_type
    if marque:
        return marque
    return None


def _detect_product_title_from_text(text: str, file_path: str, kv: dict[str, Any]) -> str:
    by_kv = _best_name_from_kv(kv)
    if by_kv:
        return by_kv[:180]

    stem = Path(file_path).stem.replace("_", " ").strip()
    stem = re.sub(r"\b(?:brochure|fiche\s+technique|ft)\b", " ", stem, flags=re.IGNORECASE)
    stem = " ".join(stem.split()).strip(" -_")
    if _is_good_name_candidate(stem):
        return stem[:180]

    lines = [(" ".join(ln.split())).strip() for ln in text.splitlines() if ln.strip()]
    for line in lines[:40]:
        if _looks_like_title_line(line):
            return line[:180]
    return Path(file_path).stem.replace("_", " ").strip()[:180] or "Produit extrait"


def _extract_unclassified_description_lines(text: str, pairs: list[dict[str, Any]]) -> list[str]:
    consumed: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        raw_line = str(pair.get("raw_line") or "").strip()
        if not raw_line or raw_line in {"LLM", "EMBED", "COMPOSED"}:
            continue
        consumed.add(_norm(raw_line))

    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip()
        if not line:
            continue
        line_norm = _norm(line)
        if not line_norm or line_norm in consumed:
            continue
        if any(token in line_norm for token in _INVOICE_LINE_KEYWORDS):
            continue
        if len(line) < 10:
            continue
        if re.fullmatch(r"[\d\s.,;:/\-()]+", line):
            continue
        if _looks_like_title_line(line):
            continue
        if line_norm in seen:
            continue
        seen.add(line_norm)
        out.append(line)
        if len(out) >= 8:
            break
    return out


def _augment_with_product_view(
    payload: dict[str, Any],
    *,
    file_path: str,
    engine: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    kv = payload.get("kv") if isinstance(payload.get("kv"), dict) else {}
    pairs = payload.get("pairs") if isinstance(payload.get("pairs"), list) else []
    raw_text = str(payload.get("raw_text") or "")
    score = float(payload.get("score") or 0.0)
    doc_type_guess = str(payload.get("doc_type_guess") or "unknown").strip().lower()
    if doc_type_guess not in {"tech_sheet", "invoice", "unknown"}:
        doc_type_guess = "unknown"

    doc_type = "tech_sheet" if doc_type_guess != "invoice" else "invoice"
    products: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []

    if doc_type == "tech_sheet":
        name = _detect_product_title_from_text(raw_text, file_path, kv)
        characteristics = _kv_to_product_characteristics(kv)
        desc_lines = _extract_unclassified_description_lines(raw_text, pairs)
        description = " || ".join(desc_lines)[:1200]
        accessories: list[dict[str, Any]] = []
        exigences: list[dict[str, Any]] = []
        verify_rows: list[dict[str, Any]] = []
        try:
            # Keep the same accessory/exigence structure as attachment extraction.
            from app.services.campaign_attachment_extraction import (  # local import avoids module cycle at import time
                _extract_structured_lists_from_block,
                _normalize_accessoires_payload,
                _normalize_characteristics_payload,
                _normalize_exigences_payload,
                _normalize_verify_payload,
                _merge_object_rows,
                _ensure_quality_review_badge,
            )

            block_lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
            normalized_characteristics = _normalize_characteristics_payload(kv, kv)
            if isinstance(normalized_characteristics, list):
                characteristics = normalized_characteristics
            accessories_raw, exigences_raw, verify_raw = _extract_structured_lists_from_block(block_lines, name)
            accessories = _normalize_accessoires_payload(accessories_raw, exigences_raw)
            exigences = _normalize_exigences_payload(exigences_raw)
            verify_rows = _normalize_verify_payload(verify_raw)
        except Exception:
            accessories = []
            exigences = []
            verify_rows = []

        product = {
            "product_id": "p1",
            "name": name,
            "quantity": None,
            "description": description,
            "characteristics": characteristics,
            "accessoires_structures": accessories,
            "exigences": exigences,
            "a_verifier": verify_rows,
            "quality_score": round(max(0.0, min(1.0, score)), 4),
            "source_engine": engine,
        }
        if score < 0.70:
            low_conf = {
                "text": name,
                "reason": f"Extraction moyenne/faible ({score:.2f}).",
            }
            existing = product.get("a_verifier") or []
            if isinstance(existing, list):
                try:
                    from app.services.campaign_attachment_extraction import _merge_object_rows

                    product["a_verifier"] = _merge_object_rows(existing, [low_conf])
                except Exception:
                    product["a_verifier"] = existing + [low_conf]
            else:
                product["a_verifier"] = [low_conf]

        # Keep the exact quality fields used by attachment extraction.
        try:
            from app.services.campaign_attachment_extraction import _ensure_quality_review_badge

            _ensure_quality_review_badge(product)
        except Exception:
            product["quality_level"] = "high" if score >= 0.78 else ("medium" if score >= 0.52 else "low")
            product["needs_review"] = bool(product.get("a_verifier"))
        products.append(product)

    payload["doc_type"] = doc_type
    payload["engine"] = engine
    payload["file_name"] = Path(file_path).name
    payload["embedding_threshold"] = None
    payload["avg_regex_score"] = round(float(payload.get("score_regex_only") or 0.0), 4)
    payload["items"] = []
    payload["items_count"] = len(products)
    payload["products"] = products
    payload["unassigned"] = unassigned
    return payload


def _is_invoice_kv_key(key: str) -> bool:
    key_norm = _norm(key)
    return any(token in key_norm for token in _INVOICE_LINE_KEYWORDS)


def _enrich_with_embedding_candidates(
    lines: list[str],
    kv: dict[str, str | list[str]],
    threshold: float = 0.55,
) -> dict[str, list[dict]]:
    if not config.OPENAI_API_KEY:
        return {}
    candidates = []
    for ln in lines:
        text = ln.strip()
        if not text:
            continue
        if len(text) > 220:
            continue
        ln_norm = _norm(text)
        if _is_invoice_kv_key(ln_norm):
            continue
        has_numeric = any(ch.isdigit() for ch in text)
        has_norm = _NORM_TOKEN_RE.search(text) is not None
        has_time = _TIME_RE.search(text) is not None
        has_pct = _PERCENT_RE.search(text) is not None
        has_key_hint = any(
            _contains_synonym(ln_norm, syn)
            for syns in _KEY_SYNONYMS.values()
            for syn in syns
        )
        if has_numeric or has_norm or has_time or has_pct or has_key_hint:
            candidates.append(text)
    if not candidates:
        return {}
    seen = set()
    uniq = []
    for c in candidates:
        n = _norm(c)
        if n in seen:
            continue
        seen.add(n)
        uniq.append(c)
    candidates = uniq[:50]

    query_texts = list(_TECH_EMBED_PROMPTS.values())
    try:
        emb = _embed_texts(query_texts + candidates)
    except Exception:
        return {}

    present_keys = {_canonical_key(str(k)) for k in kv.keys()}
    out: dict[str, list[dict]] = {}
    for cand in candidates:
        c_emb = emb.get(cand)
        if c_emb is None:
            continue
        best_key = None
        best_score = -1.0
        for key, prompt in _TECH_EMBED_PROMPTS.items():
            ckey = _canonical_key(str(key))
            if ckey in present_keys:
                continue
            q_emb = emb.get(prompt)
            if q_emb is None:
                continue
            score = cosine_similarity(c_emb, q_emb)
            if score > best_score:
                best_score = score
                best_key = ckey
        if best_key is None or best_score < threshold:
            continue
        out.setdefault(best_key, []).append({"value": cand, "score": round(float(best_score), 4)})
    for key, rows in list(out.items()):
        rows_sorted = sorted(rows, key=lambda x: x.get("score", 0.0), reverse=True)[:2]
        out[key] = rows_sorted
    return out


def _merge_keyword_lines(lines: list[str]) -> list[str]:
    merged: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        line_norm = _norm(line)
        has_digit = any(ch.isdigit() for ch in line)
        has_keyword = any(
            _contains_synonym(line_norm, syn)
            for syns in _KEY_SYNONYMS.values()
            for syn in syns
        )
        if not has_digit and has_keyword and i + 1 < len(lines):
            nxt = lines[i + 1]
            # Do not merge a complete key:value line with the next key:value line.
            # Example to avoid: "MARQUE: FK" + "TYPE: E8".
            if ":" in line:
                _, right = line.split(":", 1)
                if right.strip():
                    merged.append(line)
                    i += 1
                    continue
            if ":" in nxt:
                merged.append(line)
                i += 1
                continue
            if any(ch.isdigit() for ch in nxt):
                merged.append(f"{line} {nxt}".strip())
                i += 2
                continue
        merged.append(line)
        i += 1
    return merged


def parse_characteristics(text: str) -> dict:
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    expanded_lines: list[str] = []
    for line in raw_lines:
        if "|" in line and ":" in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if parts:
                expanded_lines.extend(parts)
                continue
        expanded_lines.append(line)
    lines = _merge_keyword_lines(expanded_lines)
    kv: dict[str, str | list[str]] = {}
    pairs: list[dict] = []
    signals: list[str] = []
    unknown_numeric_lines: list[str] = []
    text_features: set[str] = set()

    for line in lines:
        line_norm = _norm(line)
        inline_hit = False
        if _is_invoice_kv_key(line_norm):
            # Ignore invoice/admin lines in tech-sheet parsing.
            continue
        # Special case: "Tension dâ€™alimentation 200-240 V | 50/60 Hz"
        if ("tension" in line_norm or "alimentation" in line_norm) and ("v" in line_norm and "hz" in line_norm):
            ranges = _RANGE_UNIT_RE.findall(line)
            if ranges:
                for frm, to, unit in ranges:
                    value = f"{frm}-{to} {unit}".strip()
                    if unit.lower() == "v":
                        _store_kv(kv, "tension", value)
                        pairs.append({"key": "tension", "value": value, "raw_line": line})
                    if unit.lower() == "hz":
                        _store_kv(kv, "frequence", value)
                        pairs.append({"key": "frequence", "value": value, "raw_line": line})
                continue
        # Standards / compliance references (ISO/EN/CE/PED...)
        norm_tokens = _NORM_TOKEN_RE.findall(line)
        if norm_tokens:
            cleaned = sorted({tok.upper().replace("  ", " ").strip() for tok in norm_tokens})
            for tok in cleaned:
                _store_kv(kv, "normes", tok)
                pairs.append({"key": "normes", "value": tok, "raw_line": line})
            inline_hit = True
        # Cycle count and cycle time
        cycle_token = _CYCLE_COUNT_RE.search(line)
        if cycle_token:
            value = cycle_token.group(0).strip()
            _store_kv(kv, "cycle", value)
            pairs.append({"key": "cycle", "value": value, "raw_line": line})
            inline_hit = True
        time_tokens = _TIME_RE.findall(line)
        if time_tokens and ("cycle" in line_norm or "rapide" in line_norm):
            for tok in sorted({t.strip() for t in _TIME_RE.findall(line)}):
                _store_kv(kv, "temps_cycle", tok)
                pairs.append({"key": "temps_cycle", "value": tok, "raw_line": line})
            inline_hit = True
        # Charge maximum
        m_charge = _CHARGE_RE.search(line)
        if m_charge:
            value = f"{m_charge.group(1)} {m_charge.group(2)}".strip()
            _store_kv(kv, "charge", value)
            pairs.append({"key": "charge", "value": value, "raw_line": line})
            inline_hit = True
        # Consumables regex rules (non-invoice medical lines).
        m_ref = _REF_RE.search(line)
        if m_ref:
            ref_val = m_ref.group("val").strip()
            if ref_val:
                _store_kv(kv, "reference", ref_val)
                pairs.append({"key": "reference", "value": ref_val, "raw_line": line})
                inline_hit = True

        if any(ctx in line_norm for ctx in _GAUGE_CONTEXT):
            m_g = _GAUGE_RE.search(line)
            if m_g:
                gauge_val = f"{m_g.group('num').replace(',', '.')} G"
                _store_kv(kv, "taille", gauge_val)
                pairs.append({"key": "taille", "value": gauge_val, "raw_line": line})
                inline_hit = True

        m_fr = _FR_CH_RE.search(line)
        if m_fr:
            fr_val = f"{m_fr.group('num').replace(',', '.')} {m_fr.group('unit').upper()}"
            _store_kv(kv, "taille", fr_val)
            pairs.append({"key": "taille", "value": fr_val, "raw_line": line})
            inline_hit = True

        m_pack = _PACK_RE.search(line)
        if m_pack:
            xqty = m_pack.group("xqty")
            pqty = m_pack.group("pqty")
            if xqty:
                pack_val = f"x{xqty}"
                _store_kv(kv, "conditionnement", pack_val)
                pairs.append({"key": "conditionnement", "value": pack_val, "raw_line": line})
                inline_hit = True
            elif pqty:
                pack_val = m_pack.group(0).strip()
                _store_kv(kv, "conditionnement", pack_val)
                pairs.append({"key": "conditionnement", "value": pack_val, "raw_line": line})
                inline_hit = True

        sterility_match = next((t for t in _STERILITY_TERMS if _contains_synonym(line_norm, t)), None)
        if sterility_match:
            _store_kv(kv, "sterilite", sterility_match)
            pairs.append({"key": "sterilite", "value": sterility_match, "raw_line": line})
            inline_hit = True

        material_match = next((t for t in _MATERIAL_TERMS if _contains_synonym(line_norm, t)), None)
        if material_match:
            _store_kv(kv, "matiere", material_match)
            pairs.append({"key": "matiere", "value": material_match, "raw_line": line})
            inline_hit = True

        for feat in _extract_text_features(line):
            text_features.add(feat)

        # Capacity hints often appear inline in invoice item names (e.g. "AUTOCLAVE 24 LITRES ...").
        m_cap = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(l|litre|litres)\b", line, re.IGNORECASE)
        if m_cap and (
            "autoclave" in line_norm or "capacite" in line_norm or "capacit" in line_norm or "volume" in line_norm
        ):
            cap_val = f"{m_cap.group(1).replace(',', '.')} L"
            _store_kv(kv, "capacite", cap_val)
            pairs.append({"key": "capacite", "value": cap_val, "raw_line": line})
            inline_hit = True

        if any(ch.isdigit() for ch in line) or inline_hit:
            signals.append(line)

        if ":" in line:
            left, right = line.split(":", 1)
            key = _canonical_key(left.strip())
            value = right.strip()
            if value and not _is_invoice_kv_key(key):
                _store_kv(kv, key, value)
                pairs.append({"key": key, "value": value, "raw_line": line})
            continue

        matched_key = None
        for canonical, synonyms in _KEY_SYNONYMS.items():
            if any(_contains_synonym(line_norm, syn) for syn in synonyms):
                matched_key = canonical
                break
        if matched_key:
            value = None
            # For "classe", always prioritize explicit class token (A/B/C...) before numeric unit regex.
            m_class = _CLASS_RE.search(line)
            if m_class:
                value = m_class.group(1).upper()
            # "portes" should map to door count/type, not to first numeric unit in the line.
            if value is None and matched_key == "portes":
                if re.search(r"\b(double|deux|2)\s*porte", line_norm):
                    value = "2"
                elif re.search(r"\b(simple|une|un|1)\s*porte", line_norm):
                    value = "1"
            # Numeric regex are relevant for numeric keys, but not for purely textual keys
            # like sterilite/matiere/reference/usage where they can create false values.
            if matched_key not in _TEXTUAL_KEYS and matched_key != "portes" or matched_key == "taille":
                if value is None:
                    m_dim = _DIM_RE.search(line)
                    if m_dim:
                        value = f"{m_dim.group(1)} {m_dim.group(2)}".strip()
                    else:
                        m_range = _RANGE_UNIT_RE.search(line)
                        if m_range:
                            value = f"{m_range.group('from')}-{m_range.group('to')} {m_range.group('unit')}".strip()
                        else:
                            m_unit = _UNIT_RE.search(line)
                            if m_unit:
                                unit = m_unit.group("unit")
                                if matched_key == "capacite":
                                    unit = "L" if _norm(unit) in {"l", "litre", "litres"} else unit
                                value = f"{m_unit.group('value')} {unit}".strip()
            if value is None and matched_key == "taille":
                m_g = _GAUGE_RE.search(line)
                if m_g:
                    value = f"{m_g.group('num').replace(',', '.')} G"
                else:
                    m_fr2 = _FR_CH_RE.search(line)
                    if m_fr2:
                        value = f"{m_fr2.group('num').replace(',', '.')} {m_fr2.group('unit').upper()}"
            if value is None and matched_key == "reference":
                m_ref2 = _REF_RE.search(line)
                if m_ref2:
                    value = m_ref2.group("val").strip()
            if value is None and matched_key == "conditionnement":
                m_pack2 = _PACK_RE.search(line)
                if m_pack2:
                    if m_pack2.group("xqty"):
                        value = f"x{m_pack2.group('xqty')}"
                    elif m_pack2.group("pqty"):
                        value = m_pack2.group(0).strip()
            if value is None and matched_key == "matiere":
                material_match2 = next((t for t in _MATERIAL_TERMS if _contains_synonym(line_norm, t)), None)
                if material_match2:
                    value = material_match2
            if value is None and matched_key == "sterilite":
                sterility_match2 = next((t for t in _STERILITY_TERMS if _contains_synonym(line_norm, t)), None)
                if sterility_match2:
                    value = sterility_match2
            if value is None and matched_key in _TEXTUAL_KEYS:
                candidate = line.strip()
                for syn in _KEY_SYNONYMS.get(matched_key, []):
                    pattern = re.compile(rf"^\s*{re.escape(syn)}\s*[:\-]?\s*", re.IGNORECASE)
                    candidate2 = pattern.sub("", candidate).strip()
                    if candidate2 != candidate:
                        candidate = candidate2
                        break
                candidate_norm = _norm(candidate)
                if (
                    candidate
                    and candidate_norm not in _GENERIC_TEXT_VALUES
                    and any(ch.isalpha() for ch in candidate)
                ):
                    value = candidate
            if value:
                _store_kv(kv, matched_key, value)
                pairs.append({"key": matched_key, "value": value, "raw_line": line})
                inline_hit = True

        for rule in _CUSTOM_REGEX_RULES:
            rule_match = rule["regex"].search(line)
            if not rule_match:
                continue
            value = _custom_rule_value(rule, rule_match)
            if not value:
                continue
            _store_kv(kv, str(rule["key"]), value)
            pairs.append(
                {
                    "key": str(rule["key"]),
                    "value": value,
                    "raw_line": line,
                    "source": str(rule.get("name") or "custom_regex"),
                }
            )
            inline_hit = True

        if any(ch.isdigit() for ch in line) and not inline_hit:
            unknown_numeric_lines.append(line)

    score = 0.0
    if signals:
        score = len(kv) / max(len(signals), 1)

    # Compose dimensions_scellement if we have two numeric values
    dims = kv.get("dimensions_scellement")
    if isinstance(dims, list) and len(dims) >= 2:
        vals = []
        unit = None
        for v in dims:
            num, u = _to_number(str(v))
            if num is None:
                continue
            if unit is None:
                unit = u
            vals.append(str(v))
        if len(vals) >= 2:
            # Try to build "A x B unit" from first two numeric values
            n1, u1 = _to_number(str(vals[0]))
            n2, u2 = _to_number(str(vals[1]))
            if n1 is not None and n2 is not None:
                u = u1 or u2 or ""
                combined = f"{n1:g} x {n2:g} {u}".strip()
                if combined not in dims:
                    kv["dimensions_scellement"] = [combined] + dims
                    pairs.append({"key": "dimensions_scellement", "value": combined, "raw_line": "COMPOSED"})
    _dedupe_kv(kv)
    emb_kv = {}
    # Always attempt embedding completion when we have technical signals.
    # Threshold remains configurable via env/config.
    embed_threshold = max(
        0.20,
        min(1.0, float(getattr(config, "OPENAI_EXTRACTION_EMBEDDING_THRESHOLD", 0.30) or 0.30)),
    )
    if signals:
        emb_kv = _enrich_with_embedding_candidates(lines, kv, threshold=embed_threshold)
    for key, rows in emb_kv.items():
        for row in rows:
            value = str(row.get("value") or "").strip()
            if not value:
                continue
            _store_kv(kv, key, value)
            pairs.append({"key": key, "value": value, "raw_line": "EMBED"})
    _dedupe_kv(kv)
    score_after = score
    if signals:
        score_after = len(kv) / max(len(signals), 1)
    score_after = max(0.0, min(1.0, float(score_after)))
    score = max(0.0, min(1.0, float(score)))

    return {
        "kv": kv,
        "text_features": sorted(text_features),
        "pairs": pairs,
        "signals": signals,
        "unknown_numeric_lines": unknown_numeric_lines,
        "score": round(score_after, 4),
        "score_regex_only": round(score, 4),
        "embedding_kv": emb_kv,
        "doc_type_guess": "invoice" if _looks_like_invoice_text(text) else "tech_sheet",
    }


def extract_tech_sheet(file_path: str) -> tuple[str, dict, float, str]:
    # Try free local text extraction first (works for native PDFs).
    text = _extract_pdf_text_local(file_path)
    if text and len(text.strip()) >= config.TECH_SHEET_LOCAL_MIN_CHARS:
        raw_score = _raw_text_quality(text)
        if raw_score >= config.TECH_SHEET_RAW_MIN_SCORE:
            if _looks_like_invoice_text(text):
                payload = {
                    "kv": {},
                    "pairs": [],
                    "signals": [],
                    "unknown_numeric_lines": [],
                    "score": 0.0,
                    "score_regex_only": 0.0,
                    "embedding_kv": {},
                    "doc_type_guess": "invoice",
                    "raw_text": text,
                }
                payload = _augment_with_product_view(
                    payload,
                    file_path=file_path,
                    engine="local_text_invoice",
                )
                return text, payload, 0.0, "local_text_invoice"
            characteristics, score, mode_engine = _extract_tech_sheet_characteristics(text)
            engine_name = "local_text" if mode_engine == "heuristic" else f"local_text+{mode_engine}"
            characteristics["raw_text"] = text
            characteristics = _augment_with_product_view(
                characteristics,
                file_path=file_path,
                engine=engine_name,
            )
            return text, characteristics, score, engine_name

    # If local text is weak, fallback to OCR.
    if text:
        raw_score = _raw_text_quality(text)
        if raw_score < config.TECH_SHEET_RAW_MIN_SCORE:
            text = None

    # Fallback to Document AI OCR for scanned PDFs.
    text = extract_full_text(file_path)
    if _looks_like_invoice_text(text):
        payload = {
            "kv": {},
            "pairs": [],
            "signals": [],
            "unknown_numeric_lines": [],
            "score": 0.0,
            "score_regex_only": 0.0,
            "embedding_kv": {},
            "doc_type_guess": "invoice",
            "raw_text": text,
        }
        payload = _augment_with_product_view(
            payload,
            file_path=file_path,
            engine="document_ai_ocr_invoice",
        )
        return text, payload, 0.0, "document_ai_ocr_invoice"
    characteristics, score, mode_engine = _extract_tech_sheet_characteristics(text)
    engine_name = "document_ai_ocr" if mode_engine == "heuristic" else f"document_ai_ocr+{mode_engine}"
    characteristics["raw_text"] = text
    characteristics = _augment_with_product_view(
        characteristics,
        file_path=file_path,
        engine=engine_name,
    )
    return text, characteristics, score, engine_name


def _raw_text_quality(text: str) -> float:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    total_chars = sum(len(ln) for ln in lines)
    if total_chars == 0:
        return 0.0
    alnum_chars = sum(sum(ch.isalnum() for ch in ln) for ln in lines)
    line_alnum = sum(1 for ln in lines if any(ch.isalnum() for ch in ln))
    alnum_ratio = alnum_chars / total_chars
    line_alnum_ratio = line_alnum / max(len(lines), 1)
    # Heuristic: prioritize clean alnum text, but reward usable lines
    return (0.7 * alnum_ratio) + (0.3 * line_alnum_ratio)


def _extract_pdf_text_local(file_path: str) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except Exception:
            return None
    try:
        reader = PdfReader(file_path)
    except Exception:
        return None
    texts = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            continue
    content = "\n".join(texts).strip()
    return content or None


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _hash_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embed_texts(texts: list[str]) -> dict[str, list[float]]:
    cached, missing_texts, missing_hashes = get_cached_embeddings(texts)
    if missing_texts:
        # Deduplicate by hash to avoid unique constraint collisions
        unique_map: dict[str, str] = {}
        for t, h in zip(missing_texts, missing_hashes):
            if h not in unique_map:
                unique_map[h] = t
        unique_texts = list(unique_map.values())
        unique_hashes = list(unique_map.keys())

        embeddings = fetch_embeddings(unique_texts)
        db = SessionLocal()
        try:
            from app import config
            model = config.OPENAI_EMBEDDING_MODEL
            for t, h, emb in zip(unique_texts, unique_hashes, embeddings):
                db.add(
                    EmbeddingCache(
                        model=model,
                        text_hash=_hash_text(_normalize_text(t)),
                        text=t,
                        embedding=emb,
                    )
                )
            try:
                db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()
        # merge
        for t, h, emb in zip(unique_texts, unique_hashes, embeddings):
            cached[h] = emb
    # map by original text
    result: dict[str, list[float]] = {}
    for t in texts:
        h = _hash_text(_normalize_text(t))
        emb = cached.get(h)
        if emb is not None:
            result[t] = emb
    return result


def compare_with_raw_text(raw_text: str, requested: list[dict]) -> dict:
    # requested = [{"key": "...", "value": "..."}]
    if not requested:
        return {"score": 0.0, "matches": []}
    # build query strings
    queries = [
        {
            "key": str(r.get("key", "")),
            "requested": str(r.get("value", "")),
            "query": f"{r.get('key', '')} {r.get('value', '')}".strip(),
        }
        for r in requested
    ]
    # candidate lines: keep lines with digits (tech characteristics)
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    req_tokens = {_norm(str(r.get("key", ""))) for r in requested}
    candidates = []
    for ln in lines:
        ln_norm = _norm(ln)
        has_digit = any(ch.isdigit() for ch in ln)
        has_norm = _NORM_TOKEN_RE.search(ln) is not None
        has_token = any(tok and tok in ln_norm for tok in req_tokens)
        if has_digit or has_norm or has_token:
            candidates.append(ln)
    if not candidates:
        return {"score": 0.0, "matches": []}
    # embeddings
    emb_map = _embed_texts([q["query"] for q in queries] + candidates)
    matches = []
    total = 0.0
    for q in queries:
        q_text = q["query"]
        q_emb = emb_map.get(q_text)
        if q_emb is None:
            continue
        best = 0.0
        best_line = None
        for line in candidates:
            line_emb = emb_map.get(line)
            if line_emb is None:
                continue
            score = cosine_similarity(q_emb, line_emb)
            if score > best:
                best = score
                best_line = line
        matches.append(
            {
                "key": q["key"],
                "requested": q["requested"],
                "query": q_text,
                "best_line": best_line,
                "score": round(best, 4),
            }
        )
        total += best
    score = total / max(len(matches), 1)
    return {"score": round(score, 4), "matches": matches}


_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-zA-ZÂ°]+)?")


def _to_number(val: str) -> tuple[float | None, str | None]:
    if not val:
        return None, None
    m = _NUM_RE.search(val.replace(",", "."))
    if not m:
        return None, None
    num = float(m.group(1))
    unit = (m.group(2) or "").lower()
    return num, unit or None


_UNIT_CONV = {
    ("mg", "g"): 0.001,
    ("g", "mg"): 1000.0,
    ("g", "kg"): 0.001,
    ("kg", "g"): 1000.0,
    ("ml", "l"): 0.001,
    ("l", "ml"): 1000.0,
    ("w", "kw"): 0.001,
    ("kw", "w"): 1000.0,
    ("mm", "cm"): 0.1,
    ("cm", "mm"): 10.0,
    ("m", "cm"): 100.0,
    ("cm", "m"): 0.01,
    ("m", "mm"): 1000.0,
    ("mm", "m"): 0.001,
}


def _compare_values(req_val: str, cand_val: str) -> float:
    req_norm = _norm(req_val)
    cand_norm = _norm(cand_val)
    if req_norm in cand_norm:
        return 1.0
    # handle composite dimensions like "12 x 310 mm"
    if ("x" in req_norm or "Ã—" in req_val) and ("x" in cand_norm or "Ã—" in cand_val):
        req_nums = [m.group(1) for m in _NUM_RE.finditer(req_norm)]
        cand_nums = [m.group(1) for m in _NUM_RE.finditer(cand_norm)]
        if len(req_nums) >= 2 and len(cand_nums) >= 2:
            try:
                req_vals = [float(x) for x in req_nums]
                cand_vals = [float(x) for x in cand_nums]
                # compare pairwise (same order)
                ok = 0
                for rv, cv in zip(req_vals, cand_vals):
                    tol = 0.1 * rv if rv != 0 else 0.0
                    if abs(cv - rv) <= tol:
                        ok += 1
                if ok >= 2:
                    return 1.0
            except Exception:
                pass
    # numeric compare
    r_num, r_unit = _to_number(req_norm)
    c_num, c_unit = _to_number(cand_norm)
    if r_num is not None and c_num is not None:
        if r_unit and c_unit and (r_unit, c_unit) in _UNIT_CONV:
            c_num = c_num * _UNIT_CONV[(r_unit, c_unit)]
            c_unit = r_unit
        if r_unit and c_unit and r_unit != c_unit:
            return 0.0
        # 10% tolerance
        tol = 0.1 * r_num if r_num != 0 else 0.0
        if abs(c_num - r_num) <= tol:
            return 1.0
        return max(0.0, 1 - (abs(c_num - r_num) / max(r_num, 1.0)))
    return 0.0


def compare_with_kv(characteristics: dict, requested: list[dict]) -> dict:
    kv = characteristics.get("kv", {}) if characteristics else {}
    if not requested:
        return {"score": 0.0, "matches": []}
    matches = []
    total = 0.0
    considered = 0
    for req in requested:
        key = _canonical_key(req["key"])
        value = req["value"]
        cand = kv.get(key)
        best_score = 0.0
        best_value = None
        if isinstance(cand, list):
            for v in cand:
                score = _compare_values(value, str(v))
                if score > best_score:
                    best_score = score
                    best_value = v
        elif cand is not None:
            best_score = _compare_values(value, str(cand))
            best_value = cand
        if cand is not None:
            considered += 1
        matches.append(
            {
                "key": key,
                "requested": value,
                "found": best_value,
                "score": round(best_score, 4),
            }
        )
        total += best_score
    score = total / max(considered, 1)
    return {"score": round(score, 4), "matches": matches}


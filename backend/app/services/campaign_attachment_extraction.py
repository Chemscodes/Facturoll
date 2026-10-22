from __future__ import annotations

import json
import difflib
import re
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import httpx

from app import config
from app.services.openai_observability import record_openai_api_call
from app.services.document_ai import extract_full_text
from app.services.tech_sheet_extraction import (
    _canonical_key,
    _embed_texts,
    _extract_pdf_text_local,
    _looks_like_invoice_text,
    _norm,
    _raw_text_quality,
    parse_characteristics,
)

_HEADING_PREFIX_RE = re.compile(r"^\s*(?:\d+[\)\.\-]\s*|[-â€¢]\s*)")
_PRODUCT_KEYWORDS = {
    "radio",
    "fauteuil",
    "station",
    "visio",
    "autoclave",
    "sterilisateur",
    "stÃ©rilisateur",
    "soudeuse",
    "laveur",
    "desinfecteur",
    "dÃ©sinfecteur",
    "panier",
    "armoire",
    "compresseur",
    "adoucisseur",
    "nebuliseur",
    "nÃ©buliseur",
    "otoscope",
    "laryngoscope",
    "tabouret",
    "paravent",
    "gueridon",
    "guÃ©ridon",
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
    "champ",
    "stent",
    "kit",
    "consommable",
    "stethoscope",
    "tensiometre",
    "oxymetre",
    "aspirateur",
    "analyseur",
    "hematologie",
    "hÃ©matologie",
    "coagulation",
    "gaz",
    "sang",
    "biochimie",
}

_PRODUCT_OCR_CORRECTIONS: tuple[tuple[str, str], ...] = (
    (r"\bjonogramme\b", "ionogramme"),
    (r"\bjono?gramme\b", "ionogramme"),
    (r"\bh[eé]moglobine\s+glu[eé]e\b", "hémoglobine glyquée"),
    (r"\bhemoglobine\s+gluee\b", "hémoglobine glyquée"),
)

_EMBED_KEY_PROMPTS = {
    "tension": "tension voltage alimentation electrique en volts",
    "frequence": "frequence alimentation en hz",
    "puissance": "puissance consommation en watts ou kw",
    "classe": "classe type classification classe b classe c",
    "capacite": "capacite volume en litres",
    "dimensions": "dimensions longueur largeur hauteur en mm cm m",
    "dimensions_scellement": "dimensions du scellement ou zone de soudure",
    "poids": "poids masse en kg",
    "pression": "pression en bar",
    "temperature": "temperature en celsius",
    "portes": "nombre de portes ouverture",
    "matiere": "matiere materiau acier inox plastique aluminium",
    "couleur": "couleur blanc noir gris bleu rouge",
    "options": "accessoires options inclus avec",
    "sterilite": "sterile non sterile usage unique jetable autoclavable sterilisation",
    "conditionnement": "conditionnement packaging boite sachet blister lot carton",
    "reference": "reference ref code article code produit catalogue sku",
    "taille": "taille diametre calibre gauge epaisseur",
    "usage": "usage indication application destination",
}

_TEXTUAL_SIGNAL_KEYWORDS = {
    "materiau",
    "matiere",
    "acier",
    "inox",
    "plastique",
    "silicone",
    "latex",
    "sterile",
    "non sterile",
    "usage unique",
    "jetable",
    "autoclavable",
    "conditionnement",
    "packaging",
    "blister",
    "boite",
    "sachet",
    "reference",
    "catalogue",
    "sku",
    "diametre",
    "calibre",
    "gauge",
    "epaisseur",
}

_AMBIGUOUS_SIGNAL_UNITS = {
    "mm",
    "cm",
    "m",
    "kg",
    "g",
    "l",
    "litre",
    "litres",
    "w",
    "kw",
    "v",
    "hz",
    "bar",
    "classe",
    "type",
}
_AMBIGUOUS_FRAGMENT_SPLIT_RE = re.compile(r"\s*[|;/]\s*|\s+-\s+")
_AMBIGUOUS_KV_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9 _/\-]{1,28}\s*[:=]\s*[^:=]{1,140}")
_ENUMERATED_PRODUCT_RE = re.compile(r"(?:^|\s)\d{1,2}[)\.-]\s+[A-Za-z]")
_ENUMERATED_BARE_PRODUCT_RE = re.compile(
    r"(?:^|\s)\d{1,2}\s+(?=[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ]{2,})"
)
_BARE_ENUM_HEAD_RE = re.compile(r"^\s*(\d{1,2})\s+(.+)$")
_BARE_ENUM_NON_TITLE_TOKENS = {
    "parametre",
    "parametres",
    "nbre",
    "nombre",
    "jour",
    "jours",
    "personne",
    "personnes",
    "page",
    "pub",
    "telechargez",
}

_LLM_ATTACHMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "doc_type": {
            "type": "string",
            "enum": ["product_list", "requirement_sheet", "tech_sheet", "unknown"],
        },
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "description": {"type": "string"},
                    "characteristics": {"type": "array", "items": {"type": "string"}},
                    "accessories": {"type": "array", "items": {"type": "string"}},
                    "requirements": {"type": "array", "items": {"type": "string"}},
                    "to_verify": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "name",
                    "quantity",
                    "description",
                    "characteristics",
                    "accessories",
                    "requirements",
                    "to_verify",
                    "confidence",
                ],
            },
        },
        "unassigned": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["doc_type", "products", "unassigned"],
}

_LLM_ATTACHMENT_STRICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "packaging": {"type": ["string", "null"]},
                    "dimensions": {"type": ["string", "null"]},
                    "size": {"type": ["string", "null"]},
                    "weight": {"type": ["string", "null"]},
                    "vat_rate": {"type": ["number", "null"]},
                    "confidence": {"type": ["number", "null"]},
                },
                "required": [
                    "name",
                    "quantity",
                    "unit",
                    "packaging",
                    "dimensions",
                    "size",
                    "weight",
                    "vat_rate",
                    "confidence",
                ],
            },
        },
        "unassigned": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["products", "unassigned"],
}

_REQUIREMENT_DOC_HINTS = {
    "demande",
    "demande de prix",
    "fiche de conformite",
    "fiche conformite",
    "cahier des charges",
    "caracteristiques techniques",
    "specifications techniques",
    "produit demande",
    "produits demandes",
    "exigences",
    "conformite",
    "offre technique",
}

_PRODUCT_MATCH_STOPWORDS = {
    "avec",
    "sans",
    "dans",
    "pour",
    "sur",
    "des",
    "les",
    "une",
    "un",
    "du",
    "de",
    "la",
    "le",
    "et",
    "ou",
    "aux",
    "par",
    "plus",
    "moins",
    "donc",
    "cette",
    "cet",
    "ces",
    "ils",
    "elles",
    "elle",
    "il",
    "doit",
    "etre",
    "est",
    "sont",
    "livre",
    "fourni",
    "indique",
    "indiquer",
    "demande",
    "scanne",
    "camscanner",
}

_DETAIL_LINE_HINTS = (
    "doit",
    "livre",
    "fourni",
    "fonction",
    "niveau",
    "capacite",
    "puissance",
    "dimension",
    "vitesse",
    "classe",
    "forme",
    "longueur",
    "largeur",
    "hauteur",
    "pression",
    "temperature",
    "connectivite",
    "compatibilite",
    "certificat",
    "avec",
    "sans",
)

_ACCESSORY_LINE_HINTS = (
    "accessoire",
    "accessoires",
    "kit",
    "compresseur",
    "crachoir",
    "aspiration",
    "pompe",
    "meuble",
    "filtre",
    "canule",
    "tabouret",
    "plateau",
    "pince",
    "seringue",
    "camera",
    "capteur",
    "porte",
    "manche",
    "miroir",
    "sonde",
    "davier",
    "precelle",
    "excavateur",
    "spatule",
    "haricot",
    "tambour",
    "elevateur",
    "curette",
    "amalgame",
    "matrice",
    "aiguille",
    "plaque",
    "fraise",
    "fouloir",
    "brunissoir",
    "instrument",
    "inclus",
    "inclut",
    "fourni",
    "livre avec",
)

_ACCESSORY_REQUIREMENT_TERMS = (
    "accessoire",
    "accessoires",
    "livre avec",
    "fourni avec",
    "instruments suivants",
    "kit",
    "canule",
    "plateau",
    "tabouret",
    "crachoir",
    "pompe",
    "capteur",
    "cordon",
)

_EXIGENCE_LINE_HINTS = (
    "doit",
    "doivent",
    "livre",
    "livree",
    "livré",
    "livrée",
    "fourni",
    "fournie",
    "conforme",
    "conformite",
    "certificat",
    "norme",
    "obligatoire",
    "exige",
    "exigence",
    "fourniture",
    "pose",
    "branchement",
)

_ACCESSORY_SECTION_HINTS = (
    "accessoire",
    "accessoires",
    "instruments suivants",
    "livre avec les accessoires",
    "livre avec les instruments",
    "fourni avec les accessoires",
    "fourni avec les instruments",
)

_ACCESSORY_QTY_PREFIX_RE = re.compile(
    r"^\s*(?:\W\s*)?(\d{1,3})(?:\s+|(?=[A-Za-zÀ-ÖØ-öø-ÿ]))",
)
_ACCESSORY_LEADING_QTY_RE = re.compile(
    r"^\s*(?:\W\s*)?\d{1,3}(?:[.,]\d+)?(?:\s+|(?=[A-Za-zÀ-ÖØ-öø-ÿ]))",
)
_ACCESSORY_MATERIAL_RE = re.compile(
    r"\b(?:inox(?:ydable)?|acier(?:\s+inoxydable)?|aluminium|plastique|silicone|caoutchouc|metal(?:lique)?|verre)\b",
    flags=re.IGNORECASE,
)
_ACCESSORY_DIMENSION_RE = re.compile(
    r"\b\d{1,4}(?:[.,]\d+)?\s*(?:x|×|\*)\s*\d{1,4}(?:[.,]\d+)?(?:\s*(?:x|×|\*)\s*\d{1,4}(?:[.,]\d+)?)?\s*(?:mm|cm|m)?\b",
    flags=re.IGNORECASE,
)
_TECHNICAL_KV_HINTS = (
    "tension",
    "puissance",
    "frequence",
    "fréquence",
    "temperature",
    "température",
    "pression",
    "capacite",
    "capacité",
    "dimension",
    "classe",
    "connectivite",
    "connectivité",
    "affichage",
    "volume",
    "poids",
)

_CHARACTERISTIC_KEY_ALIASES = {
    "dimensions": "dimensions",
    "dimension": "dimensions",
    "dimensions_scellement": "dimensions_scellement",
    "tension": "tension",
    "frequence": "frequence",
    "puissance": "puissance",
    "capacite": "capacite",
    "classe": "classe",
    "classe_sterilisation": "classe_sterilisation",
    "resolution": "resolution",
    "connectivite": "connectivite",
    "connectivite_usb": "connectivite",
    "longueur_cable": "longueur_cable",
    "forme": "forme",
    "poids": "poids",
    "pression": "pression",
    "temperature": "temperature",
    "usage": "usage",
    "materiel": "materiel",
    "matiere": "matiere",
    "conditionnement": "conditionnement",
    "normes": "normes",
}

_ROW_FAMILY_TOKENS_BASE: dict[str, tuple[str, ...]] = {
    "autoclave": (
        "autoclave",
        "sterilis",
        "vapeur",
        "classe b",
        "vide fractionne",
        "chambre",
    ),
    "soudeuse": (
        "soudeuse",
        "gaine",
        "scellement",
        "thermosoudeuse",
        "soudure",
    ),
    "radio": (
        "radio",
        "rvg",
        "visio",
        "graphie",
        "capteur",
        "radiographique",
        "resolution",
    ),
    "fauteuil": (
        "fauteuil",
        "tabouret",
        "meuble",
        "filtre",
        "crachoir",
        "aspiration",
        "canule",
        "cordon",
        "turbine",
        "micromoteur",
        "compresseur",
        "detartreur",
        "détartreur",
    ),
}

_ROW_FAMILY_STOPWORDS = {
    "avec",
    "sans",
    "pour",
    "sur",
    "dans",
    "des",
    "les",
    "une",
    "un",
    "du",
    "de",
    "la",
    "le",
    "et",
    "ou",
    "plus",
    "moins",
    "type",
    "modele",
    "modèle",
    "materiel",
    "matériel",
    "medical",
    "médical",
    "equipement",
    "équipement",
    "appareil",
    "dispositif",
    "solution",
    "systeme",
    "système",
}
_ROW_FAMILY_TOKEN_RE = re.compile(r"[a-z0-9à-öø-ÿ]+")


def _row_family_slug(value: str) -> str:
    slug = _norm(value).replace(" ", "_")
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:80]


def _row_family_tokens_from_text(text: str) -> list[str]:
    norm = _norm(text or "")
    if not norm:
        return []
    parts = _ROW_FAMILY_TOKEN_RE.findall(norm)
    out: list[str] = []
    for token in parts:
        if len(token) < 3:
            continue
        if token in _ROW_FAMILY_STOPWORDS:
            continue
        out.append(token)
    return out


def _load_json_dict_file(path_value: str) -> dict[str, Any]:
    path_text = (path_value or "").strip()
    if not path_text:
        return {}
    try:
        path = Path(path_text).expanduser()
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _load_custom_row_family_tokens() -> dict[str, tuple[str, ...]]:
    payload = _load_json_dict_file(str(getattr(config, "CUSTOM_CHARACTERISTICS_RULES_PATH", "") or ""))
    raw = payload.get("row_family_tokens")
    if not isinstance(raw, dict):
        return {}

    out: dict[str, tuple[str, ...]] = {}
    for family, raw_tokens in raw.items():
        family_key = _row_family_slug(str(family or ""))
        if not family_key:
            continue
        values = raw_tokens if isinstance(raw_tokens, list) else [raw_tokens]
        tokens: list[str] = []
        seen: set[str] = set()
        for value in values:
            token_norm = _norm(str(value or ""))
            if len(token_norm) < 3:
                continue
            if token_norm in _ROW_FAMILY_STOPWORDS:
                continue
            if token_norm in seen:
                continue
            seen.add(token_norm)
            tokens.append(token_norm)
        if tokens:
            out[family_key] = tuple(tokens)
    return out


def _load_medical_lexicon_row_family_tokens() -> dict[str, tuple[str, ...]]:
    enabled = bool(getattr(config, "MEDICAL_LEXICON_ENABLED", True))
    if not enabled:
        return {}

    payload = _load_json_dict_file(str(getattr(config, "MEDICAL_LEXICON_PATH", "") or ""))
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return {}

    entry_rows: list[tuple[str, set[str]]] = []
    token_frequency: dict[str, int] = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("canonical") or "").strip()
        if not canonical:
            continue
        aliases_raw = entry.get("aliases")
        aliases = aliases_raw if isinstance(aliases_raw, list) else []
        sources = [canonical] + [str(alias or "") for alias in aliases]

        token_set: set[str] = set()
        for source in sources:
            tokens = _row_family_tokens_from_text(source)
            token_set.update(tokens)
            # Add bigrams to keep terms more specific than single generic words.
            for i in range(len(tokens) - 1):
                left = tokens[i]
                right = tokens[i + 1]
                if left in _ROW_FAMILY_STOPWORDS or right in _ROW_FAMILY_STOPWORDS:
                    continue
                token_set.add(f"{left} {right}")

        if not token_set:
            continue
        entry_rows.append((canonical, token_set))
        for token in token_set:
            token_frequency[token] = token_frequency.get(token, 0) + 1

    if not entry_rows:
        return {}

    max_freq = max(3, int(round(len(entry_rows) * 0.18)))
    out: dict[str, tuple[str, ...]] = {}

    for canonical, token_set in entry_rows:
        family_key = _row_family_slug(canonical)
        if not family_key:
            continue
        specific = [
            token
            for token in token_set
            if token_frequency.get(token, 0) <= max_freq
        ]
        if not specific:
            specific = sorted(
                token_set,
                key=lambda token: (token_frequency.get(token, 999), -len(token), token),
            )[:8]
        ordered = [token for token in specific if len(token) >= 3]
        if not ordered:
            continue
        out[family_key] = tuple(ordered[:16])
    return out


def _load_medical_lexicon_matchers() -> list[dict[str, Any]]:
    enabled = bool(getattr(config, "MEDICAL_LEXICON_ENABLED", True))
    if not enabled:
        return []

    payload = _load_json_dict_file(str(getattr(config, "MEDICAL_LEXICON_PATH", "") or ""))
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []

    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        canonical = " ".join(str(entry.get("canonical") or "").split()).strip()
        canonical_norm = _norm(canonical)
        if not canonical_norm:
            continue

        aliases_raw = entry.get("aliases")
        aliases = aliases_raw if isinstance(aliases_raw, list) else []
        alias_values = [canonical] + [str(alias or "") for alias in aliases]
        alias_norms: list[str] = []
        seen: set[str] = set()
        token_set: set[str] = set()
        for alias in alias_values:
            alias_norm = _norm(alias)
            if len(alias_norm) < 3:
                continue
            if alias_norm in seen:
                continue
            seen.add(alias_norm)
            alias_norms.append(alias_norm)
            token_set.update(_row_family_tokens_from_text(alias_norm))

        if not alias_norms:
            continue
        out.append(
            {
                "canonical": canonical,
                "canonical_norm": canonical_norm,
                "aliases_norm": tuple(alias_norms),
                "token_set": tuple(sorted(token_set)),
            }
        )
    return out


def _lexicon_similarity(text_norm: str, alias_norm: str) -> float:
    if not text_norm or not alias_norm:
        return 0.0
    if text_norm == alias_norm:
        return 1.0

    seq_score = difflib.SequenceMatcher(None, text_norm, alias_norm).ratio()
    text_tokens = set(_row_family_tokens_from_text(text_norm))
    alias_tokens = set(_row_family_tokens_from_text(alias_norm))
    token_score = 0.0
    if text_tokens and alias_tokens:
        token_score = len(text_tokens & alias_tokens) / max(len(text_tokens), len(alias_tokens))

    phrase_bonus = 0.0
    if alias_norm in text_norm or text_norm in alias_norm:
        shorter = min(len(alias_norm), len(text_norm))
        if shorter >= 8:
            phrase_bonus = 0.12
        elif shorter >= 5:
            phrase_bonus = 0.06

    score = (0.68 * seq_score) + (0.32 * token_score) + phrase_bonus
    return float(max(0.0, min(1.0, score)))


def _match_medical_lexicon_name(name: str) -> dict[str, Any] | None:
    name_norm = _norm(name)
    if len(name_norm) < 3:
        return None
    if not _MEDICAL_LEXICON_MATCHERS:
        return None

    best_match: dict[str, Any] | None = None
    best_score = 0.0
    best_alias = ""

    for entry in _MEDICAL_LEXICON_MATCHERS:
        aliases = entry.get("aliases_norm")
        if not isinstance(aliases, tuple):
            continue
        local_best = 0.0
        local_alias = ""
        for alias_norm in aliases:
            score = _lexicon_similarity(name_norm, str(alias_norm))
            if score > local_best:
                local_best = score
                local_alias = str(alias_norm)
        if local_best > best_score:
            best_score = local_best
            best_match = entry
            best_alias = local_alias

    min_score = float(getattr(config, "MEDICAL_LEXICON_MIN_SCORE", 0.58) or 0.58)
    if best_match is None or best_score < min_score:
        return None

    autocorrect_score = float(getattr(config, "MEDICAL_LEXICON_AUTOCORRECT_SCORE", 0.88) or 0.88)
    canonical = _clean_line(str(best_match.get("canonical") or ""))
    if not canonical:
        return None
    return {
        "canonical": canonical,
        "canonical_norm": _norm(canonical),
        "score": round(best_score, 4),
        "alias_norm": best_alias,
        "auto_correct": bool(best_score >= autocorrect_score),
    }


def _build_llm_medical_lexicon_hints(raw_text: str) -> str:
    if not bool(getattr(config, "OPENAI_ATTACHMENT_LEXICON_HINTS_ENABLED", True)):
        return ""
    if not bool(getattr(config, "MEDICAL_LEXICON_ENABLED", True)):
        return ""
    if not _MEDICAL_LEXICON_MATCHERS:
        return ""

    max_entries = max(
        0,
        int(getattr(config, "OPENAI_ATTACHMENT_LEXICON_HINTS_MAX_ENTRIES", 40) or 40),
    )
    if max_entries <= 0:
        return ""
    max_aliases = max(
        1,
        int(getattr(config, "OPENAI_ATTACHMENT_LEXICON_HINTS_MAX_ALIASES", 2) or 2),
    )
    max_chars = max(
        300,
        int(getattr(config, "OPENAI_ATTACHMENT_LEXICON_HINTS_MAX_CHARS", 3500) or 3500),
    )

    text_tokens = set(_row_family_tokens_from_text(raw_text))
    if not text_tokens:
        return ""

    scored: list[tuple[int, str, tuple[str, ...]]] = []
    for entry in _MEDICAL_LEXICON_MATCHERS:
        canonical = _clean_line(str(entry.get("canonical") or ""))
        if not canonical:
            continue
        token_set = set(entry.get("token_set") or ())
        overlap = len(text_tokens & token_set)
        if overlap <= 0:
            continue
        aliases_norm = entry.get("aliases_norm")
        aliases_tuple = aliases_norm if isinstance(aliases_norm, tuple) else ()
        scored.append((overlap, canonical, aliases_tuple))

    if not scored:
        return ""

    scored.sort(key=lambda row: (-row[0], -len(row[1]), row[1]))
    lines: list[str] = []
    for overlap, canonical, aliases_tuple in scored[:max_entries]:
        canonical_norm = _norm(canonical)
        alias_candidates = [
            alias
            for alias in aliases_tuple
            if isinstance(alias, str) and alias and alias != canonical_norm
        ]
        alias_preview = ", ".join(alias_candidates[:max_aliases])
        if alias_preview:
            lines.append(f"- {canonical} | aliases: {alias_preview} | overlap={overlap}")
        else:
            lines.append(f"- {canonical} | overlap={overlap}")

    if not lines:
        return ""

    out = (
        "Lexique medical d'aide (indices, non bloquant):\n"
        + "\n".join(lines)
    ).strip()
    if len(out) <= max_chars:
        return out
    return out[: max_chars - 3].rstrip() + "..."


def _merge_row_family_maps(*maps: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    merged: dict[str, tuple[str, ...]] = {}
    for mapping in maps:
        for family, raw_tokens in mapping.items():
            family_key = _row_family_slug(str(family or ""))
            if not family_key:
                continue
            prev = list(merged.get(family_key, ()))
            seen: set[str] = set(prev)
            for token in raw_tokens:
                token_norm = _norm(str(token or ""))
                if len(token_norm) < 3:
                    continue
                if token_norm in _ROW_FAMILY_STOPWORDS:
                    continue
                if token_norm in seen:
                    continue
                seen.add(token_norm)
                prev.append(token_norm)
            if prev:
                merged[family_key] = tuple(prev[:28])
    return merged


def _normalize_row_family_map(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for family, raw_tokens in raw.items():
        family_key = _row_family_slug(str(family or ""))
        if not family_key:
            continue
        values = raw_tokens if isinstance(raw_tokens, list) else [raw_tokens]
        tokens: list[str] = []
        seen: set[str] = set()
        for value in values:
            token = _norm(str(value or ""))
            if len(token) < 3:
                continue
            if token in _ROW_FAMILY_STOPWORDS:
                continue
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        if tokens:
            out[family_key] = tuple(tokens)
    return out


def _save_json_dict_file(path_value: str, payload: dict[str, Any]) -> bool:
    path_text = (path_value or "").strip()
    if not path_text:
        return False
    try:
        path = Path(path_text).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


def refresh_row_family_tokens() -> dict[str, tuple[str, ...]]:
    global _ROW_FAMILY_TOKENS, _MEDICAL_LEXICON_MATCHERS
    _ROW_FAMILY_TOKENS = _merge_row_family_maps(
        _ROW_FAMILY_TOKENS_BASE,
        _load_medical_lexicon_row_family_tokens(),
        _load_custom_row_family_tokens(),
    )
    _MEDICAL_LEXICON_MATCHERS = _load_medical_lexicon_matchers()
    return _ROW_FAMILY_TOKENS


def _collect_product_learning_tokens(product: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    name = _clean_line(str(product.get("name") or product.get("product_name") or ""))
    if name:
        texts.append(name)

    description = _clean_line(str(product.get("description") or ""))
    if description:
        texts.append(description)

    for row in product.get("characteristics") or []:
        if not isinstance(row, dict):
            continue
        key = _clean_line(str(row.get("key") or ""))
        values = row.get("values")
        text_values = values if isinstance(values, list) else [values]
        for value in text_values:
            chunk = _clean_line(" ".join(part for part in [key, str(value or "")] if part))
            if chunk:
                texts.append(chunk)

    for row in product.get("accessoires_structures") or []:
        if isinstance(row, dict):
            for key in ("item", "details", "material", "group"):
                chunk = _clean_line(str(row.get(key) or ""))
                if chunk:
                    texts.append(chunk)
        else:
            chunk = _clean_line(str(row or ""))
            if chunk:
                texts.append(chunk)

    for row in product.get("exigences") or []:
        if isinstance(row, dict):
            chunk = _clean_line(str(row.get("text") or ""))
        else:
            chunk = _clean_line(str(row or ""))
        if chunk:
            texts.append(chunk)

    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        base_tokens = _row_family_tokens_from_text(text)
        for token in base_tokens:
            if token in seen:
                continue
            seen.add(token)
            out.append(token)
        for idx in range(len(base_tokens) - 1):
            bigram = f"{base_tokens[idx]} {base_tokens[idx + 1]}"
            if bigram in seen:
                continue
            seen.add(bigram)
            out.append(bigram)
    return out[:24]


def _merge_learned_row_family_tokens(learned: dict[str, tuple[str, ...]]) -> int:
    path = str(getattr(config, "CUSTOM_CHARACTERISTICS_RULES_PATH", "") or "")
    payload = _load_json_dict_file(path)
    if not payload:
        payload = {}

    existing_map = _normalize_row_family_map(payload.get("row_family_tokens"))
    merged_map = _merge_row_family_maps(existing_map, learned)

    changed = 0
    for family, tokens in merged_map.items():
        prev = existing_map.get(family, ())
        if tuple(prev) != tuple(tokens):
            changed += 1

    payload["row_family_tokens"] = {
        family: list(tokens)
        for family, tokens in sorted(merged_map.items())
    }
    saved = _save_json_dict_file(path, payload)
    return changed if saved else 0


def _append_anchor_linking_examples(
    examples: list[dict[str, Any]],
    *,
    campaign_id: int | None,
    attachment_id: int | None,
    source: str,
) -> int:
    if not examples:
        return 0
    path = str(getattr(config, "ANCHOR_LINKING_EXAMPLES_PATH", "") or "")
    payload = _load_json_dict_file(path)
    if not payload:
        payload = {"version": "1.0", "examples": []}

    history = payload.get("examples")
    if not isinstance(history, list):
        history = []

    history.append(
        {
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source": source,
            "campaign_id": campaign_id,
            "attachment_id": attachment_id,
            "examples": examples,
        }
    )
    payload["version"] = str(payload.get("version") or "1.0")
    payload["examples"] = history[-500:]
    saved = _save_json_dict_file(path, payload)
    return len(examples) if saved else 0


def learn_anchor_linking_from_products(
    products: list[dict[str, Any]],
    *,
    campaign_id: int | None = None,
    attachment_id: int | None = None,
    source: str = "manual_correction",
) -> dict[str, Any]:
    if not isinstance(products, list) or not products:
        return {
            "learned_families": 0,
            "examples_saved": 0,
            "family_tokens_total": len(_ROW_FAMILY_TOKENS),
        }

    learned: dict[str, tuple[str, ...]] = {}
    example_rows: list[dict[str, Any]] = []

    for raw in products:
        if not isinstance(raw, dict):
            continue
        name = _clean_line(str(raw.get("name") or raw.get("product_name") or ""))
        if not name:
            continue
        family = _row_family_slug(name)
        if not family:
            continue
        tokens = _collect_product_learning_tokens(raw)
        if not tokens:
            continue

        merged = _merge_row_family_maps(
            {family: tuple(tokens)},
            {family: learned.get(family, ())},
        )
        learned[family] = merged.get(family, tuple(tokens))
        example_rows.append(
            {
                "family": family,
                "name": name,
                "tokens": list(learned[family])[:20],
            }
        )

    if not learned:
        return {
            "learned_families": 0,
            "examples_saved": 0,
            "family_tokens_total": len(_ROW_FAMILY_TOKENS),
        }

    updated_families = _merge_learned_row_family_tokens(learned)
    examples_saved = _append_anchor_linking_examples(
        example_rows,
        campaign_id=campaign_id,
        attachment_id=attachment_id,
        source=source,
    )
    refresh_row_family_tokens()
    return {
        "learned_families": int(updated_families),
        "examples_saved": int(examples_saved),
        "family_tokens_total": int(len(_ROW_FAMILY_TOKENS)),
        "families": sorted(learned.keys()),
    }


_ROW_FAMILY_TOKENS: dict[str, tuple[str, ...]] = {}
_MEDICAL_LEXICON_MATCHERS: list[dict[str, Any]] = []
refresh_row_family_tokens()

_NON_PRODUCT_TITLE_PREFIXES = {
    "reservoir",
    "réservoir",
    "utilisable",
    "equip",
    "équip",
    "construction",
    "puissance",
    "taille",
    "chasse",
    "grille",
    "grillée",
    "livre",
    "se raccorde",
    "fils",
    "no ",
    "i/ip",
    "conditions environnementales",
    "scanne",
    "scanné",
    "une paire",
}

_DOC_TYPE_HINT_VALUES = {
    "auto",
    "product_list",
    "requirement_sheet",
    "tech_sheet",
    "lot_table",
    "catalog_list",
    "accessories_appendix",
    "strict_produit",
    "mixed",
}
_LAYOUT_HINT_VALUES = {"auto", "table", "paragraph", "mixed"}
_STRICT_COUNT_MODE_VALUES = {"auto", "strict", "soft", "off"}

_DOC_TYPE_HINT_TO_EFFECTIVE_DOC_TYPE = {
    "product_list": "product_list",
    "lot_table": "product_list",
    "catalog_list": "product_list",
    "strict_produit": "product_list",
    "requirement_sheet": "requirement_sheet",
    "accessories_appendix": "requirement_sheet",
    "tech_sheet": "tech_sheet",
}
_DOC_TYPE_HINT_TO_LAYOUT = {
    "lot_table": "table",
    "catalog_list": "mixed",
    "accessories_appendix": "mixed",
    "mixed": "mixed",
}

_DEFAULT_HINT_BEHAVIOR: dict[str, Any] = {
    "force_invoice_like": None,  # None=auto, True=force on, False=force off
    "force_product_list_signal": None,  # None=auto, True=on, False=off
    "allow_flexible_mode": True,
    "prefer_list_when_available": False,
    "prefer_mixed_merge_with_strict": True,
    "strict_requirement_filter": False,
    "aggressive_accessory_capture": False,
}
_HINT_BEHAVIOR_OVERRIDES: dict[str, dict[str, Any]] = {
    "product_list": {
        "force_product_list_signal": True,
        "prefer_list_when_available": True,
    },
    "strict_produit": {
        "force_product_list_signal": True,
        "allow_flexible_mode": False,
        "prefer_list_when_available": True,
        "prefer_mixed_merge_with_strict": False,
        "strict_requirement_filter": True,
    },
    "lot_table": {
        "force_invoice_like": True,
        "force_product_list_signal": True,
        "allow_flexible_mode": False,
        "prefer_list_when_available": True,
        "prefer_mixed_merge_with_strict": False,
    },
    "catalog_list": {
        "force_invoice_like": False,
        "force_product_list_signal": True,
        "allow_flexible_mode": True,
        "prefer_list_when_available": True,
        "prefer_mixed_merge_with_strict": False,
    },
    "requirement_sheet": {
        "force_invoice_like": False,
        "force_product_list_signal": False,
        "allow_flexible_mode": False,
        "strict_requirement_filter": True,
        "prefer_mixed_merge_with_strict": False,
    },
    "accessories_appendix": {
        "force_invoice_like": False,
        "force_product_list_signal": False,
        "allow_flexible_mode": False,
        "strict_requirement_filter": True,
        "aggressive_accessory_capture": True,
        "prefer_mixed_merge_with_strict": False,
    },
    "tech_sheet": {
        "force_invoice_like": False,
        "force_product_list_signal": False,
        "allow_flexible_mode": False,
        "prefer_mixed_merge_with_strict": False,
    },
    "mixed": {
        "allow_flexible_mode": True,
        "prefer_mixed_merge_with_strict": True,
    },
}


def _normalize_doc_type_hint(value: Any) -> str:
    raw = _norm(str(value or "auto"))
    aliases = {
        "auto": "auto",
        "liste": "product_list",
        "list": "product_list",
        "product_list": "product_list",
        "products": "product_list",
        "strict_produit": "strict_produit",
        "strict_produits": "strict_produit",
        "strict_product": "strict_produit",
        "strict_products": "strict_produit",
        "requirement_sheet": "requirement_sheet",
        "requirement": "requirement_sheet",
        "cahier_des_charges": "requirement_sheet",
        "tech_sheet": "tech_sheet",
        "fiche_technique": "tech_sheet",
        "tech": "tech_sheet",
        "lot_table": "lot_table",
        "lot": "lot_table",
        "lotlist": "lot_table",
        "catalog_list": "catalog_list",
        "catalogue": "catalog_list",
        "catalog": "catalog_list",
        "accessories_appendix": "accessories_appendix",
        "annexe_accessoires": "accessories_appendix",
        "appendix_accessories": "accessories_appendix",
        "mixed": "mixed",
        "mixte": "mixed",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in _DOC_TYPE_HINT_VALUES else "auto"


def _effective_doc_type_hint(doc_type_hint: str) -> str | None:
    return _DOC_TYPE_HINT_TO_EFFECTIVE_DOC_TYPE.get(doc_type_hint)


def _effective_layout_from_doc_type_hint(doc_type_hint: str) -> str | None:
    return _DOC_TYPE_HINT_TO_LAYOUT.get(doc_type_hint)


def _hint_behavior_profile(doc_type_hint: str) -> dict[str, Any]:
    profile = dict(_DEFAULT_HINT_BEHAVIOR)
    profile.update(_HINT_BEHAVIOR_OVERRIDES.get(doc_type_hint, {}))
    return profile


def _normalize_layout_hint(value: Any) -> str:
    raw = _norm(str(value or "auto"))
    aliases = {
        "auto": "auto",
        "table": "table",
        "tabular": "table",
        "tableau": "table",
        "paragraph": "paragraph",
        "paragraphe": "paragraph",
        "text": "paragraph",
        "mixed": "mixed",
        "mixte": "mixed",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in _LAYOUT_HINT_VALUES else "auto"


def _normalize_strict_count_mode(value: Any) -> str:
    raw = _norm(str(value or "auto"))
    aliases = {
        "auto": "auto",
        "strict": "strict",
        "hard": "strict",
        "soft": "soft",
        "souple": "soft",
        "off": "off",
        "disabled": "off",
        "disable": "off",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in _STRICT_COUNT_MODE_VALUES else "auto"


def _apply_require_specs_for_acceptance(payload: dict[str, Any]) -> dict[str, Any]:
    products = payload.get("products")
    if not isinstance(products, list):
        return payload

    for row in products:
        if not isinstance(row, dict):
            continue
        has_structured = bool(
            (row.get("characteristics") or [])
            or (row.get("accessoires_structures") or [])
            or (row.get("exigences") or [])
        )
        description = _clean_line(str(row.get("description") or ""))
        has_description = len(description.split()) >= 4
        if has_structured or has_description:
            continue
        name = _clean_line(str(row.get("name") or "Produit"))
        row["a_verifier"] = _merge_object_rows(
            row.get("a_verifier") or [],
            [
                {
                    "text": name,
                    "reason": "Mode require_specs_for_acceptance: specs insuffisantes pour validation automatique.",
                }
            ],
        )
        row["needs_review"] = True
        _ensure_quality_review_badge(row)

    payload["products"] = products
    return payload


def _convert_strict_products_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    products_out: list[dict[str, Any]] = []
    for raw in parsed.get("products") or []:
        if not isinstance(raw, dict):
            continue
        name = _clean_line(str(raw.get("name") or ""))
        if not name:
            continue
        quantity = _normalize_quantity_value(raw.get("quantity"))
        characteristics: list[str] = []
        packaging = _clean_line(str(raw.get("packaging") or ""))
        if packaging:
            characteristics.append(f"conditionnement: {packaging}")
        unit = _clean_line(str(raw.get("unit") or ""))
        if unit:
            characteristics.append(f"unite: {unit}")
        dimensions = _clean_line(str(raw.get("dimensions") or ""))
        if dimensions:
            characteristics.append(f"dimensions: {dimensions}")
        size = _clean_line(str(raw.get("size") or ""))
        if size:
            characteristics.append(f"taille: {size}")
        weight = _clean_line(str(raw.get("weight") or ""))
        if weight:
            characteristics.append(f"poids: {weight}")
        vat_rate = raw.get("vat_rate")
        if isinstance(vat_rate, (int, float)):
            characteristics.append(f"tva: {float(vat_rate):g}%")
        confidence = raw.get("confidence")
        conf_value: float | None = None
        if isinstance(confidence, (int, float)):
            conf_value = float(confidence)
        products_out.append(
            {
                "name": name,
                "quantity": quantity,
                "description": "",
                "characteristics": characteristics,
                "accessories": [],
                "requirements": [],
                "to_verify": [],
                "confidence": conf_value if conf_value is not None else 0.7,
            }
        )
    return {
        "doc_type": "product_list",
        "products": products_out,
        "unassigned": list(parsed.get("unassigned") or []),
    }


_STRICT_MEASURE_UNIT_ALIASES = {
    "mm": "mm",
    "cm": "cm",
    "m": "m",
    "mg": "mg",
    "g": "g",
    "gr": "g",
    "gram": "g",
    "gramme": "g",
    "grammes": "g",
    "kg": "kg",
    "ml": "ml",
    "cl": "cl",
    "l": "l",
    "litre": "l",
    "litres": "l",
    "liter": "l",
    "liters": "l",
}

_STRICT_COMMERCIAL_UNITS = {
    "u",
    "un",
    "unit",
    "unite",
    "unites",
    "piece",
    "pieces",
    "pc",
    "pcs",
    "rouleau",
    "rouleaux",
    "flacon",
    "flacons",
    "boite",
    "boites",
    "box",
    "pack",
    "sachet",
    "sachets",
    "bt",
    "bte",
}

_STRICT_WEIGHT_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(mg|g|gr|kg)\b", re.IGNORECASE)
_STRICT_PACKAGING_RE = re.compile(
    r"\b(?:bt|bte|b|p|boite|boites|box|pack|paquet|carton|sachet)\s*(?:de)?\s*[/.-]?\s*\d+\b",
    re.IGNORECASE,
)
_STRICT_MEASURE_UNIT_TOKEN_RE = re.compile(
    r"\b(?:\d+(?:[.,]\d+)?)\s*(mm|cm|m|mg|g|gr|kg|ml|cl|l)\b",
    re.IGNORECASE,
)


def _normalize_strict_unit_value(value: Any) -> tuple[str | None, str | None]:
    raw = _clean_line(str(value or ""))
    if not raw:
        return None, None
    token_match = _STRICT_MEASURE_UNIT_TOKEN_RE.search(raw)
    if token_match:
        token_with_value = _norm(token_match.group(1)).replace(" ", "")
        canonical = _STRICT_MEASURE_UNIT_ALIASES.get(token_with_value)
        if canonical:
            return canonical, None
    token = _norm(raw).replace(" ", "")
    if not token:
        return None, None
    canonical = _STRICT_MEASURE_UNIT_ALIASES.get(token)
    if canonical:
        return canonical, None
    if token in _STRICT_COMMERCIAL_UNITS:
        return None, raw
    return None, raw


def _extract_weight_from_text(text: str | None) -> str | None:
    value = _clean_line(str(text or ""))
    if not value:
        return None
    match = _STRICT_WEIGHT_RE.search(value)
    if not match:
        return None
    num = _clean_line(match.group(1)).replace(",", ".")
    unit_raw = _norm(match.group(2)).replace(" ", "")
    unit = _STRICT_MEASURE_UNIT_ALIASES.get(unit_raw)
    if not num or not unit:
        return None
    return f"{num} {unit}"


def _extract_measure_unit_from_text(text: str | None) -> str | None:
    value = _clean_line(str(text or ""))
    if not value:
        return None
    match = _STRICT_MEASURE_UNIT_TOKEN_RE.search(value)
    if not match:
        return None
    token = _norm(match.group(1)).replace(" ", "")
    return _STRICT_MEASURE_UNIT_ALIASES.get(token)


def _extract_weight_unit(text: str | None) -> str | None:
    value = _clean_line(str(text or ""))
    if not value:
        return None
    match = _STRICT_WEIGHT_RE.search(value)
    if not match:
        return None
    token = _norm(match.group(2)).replace(" ", "")
    return _STRICT_MEASURE_UNIT_ALIASES.get(token)


def _extract_packaging_from_text(text: str | None) -> str | None:
    value = _clean_line(str(text or ""))
    if not value:
        return None
    match = _STRICT_PACKAGING_RE.search(value)
    if not match:
        return None
    return _clean_line(match.group(0))


def _sanitize_strict_product_row(raw: dict[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    name = _clean_line(str(row.get("name") or ""))
    packaging = _clean_line(str(row.get("packaging") or ""))
    dimensions = _clean_line(str(row.get("dimensions") or ""))
    size = _clean_line(str(row.get("size") or ""))
    unit_raw = row.get("unit")
    unit_norm, moved_packaging = _normalize_strict_unit_value(unit_raw)
    if unit_norm is not None:
        row["unit"] = unit_norm
    else:
        row["unit"] = None
        if not packaging and moved_packaging:
            packaging = moved_packaging

    # Recover packaging and weight from explicit product text when missing.
    if not packaging:
        packaging = _extract_packaging_from_text(name)
    row["packaging"] = packaging or None

    weight = _clean_line(str(row.get("weight") or ""))
    if not weight:
        weight = _extract_weight_from_text(name) or ""
    row["weight"] = weight or None

    # Fill unit from the best explicit source when missing:
    # explicit weight unit -> dimensions/size unit -> product text unit.
    if not row.get("unit"):
        unit_from_weight = _extract_weight_unit(row.get("weight"))
        unit_from_dims = _extract_measure_unit_from_text(dimensions) or _extract_measure_unit_from_text(size)
        unit_from_name = _extract_measure_unit_from_text(name)
        row["unit"] = unit_from_weight or unit_from_dims or unit_from_name

    return row


def _sanitize_strict_products_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    out = dict(parsed)
    rows = parsed.get("products")
    if not isinstance(rows, list):
        out["products"] = []
        return out
    clean_rows: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        clean_rows.append(_sanitize_strict_product_row(raw))
    out["products"] = clean_rows
    return out


def _to_float(value: str) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return None


def _parse_quantity_range(text: str) -> float | None:
    raw = (text or "").strip().lower()
    if not raw:
        return None
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:-|–|—|to|a|à|au)\s*(\d+(?:[.,]\d+)?)",
        raw,
    )
    if m:
        a = _to_float(m.group(1))
        b = _to_float(m.group(2))
        if a is not None and b is not None:
            return min(a, b)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)", raw)
    if m:
        a = _to_float(m.group(1))
        b = _to_float(m.group(2))
        if a is not None and b is not None:
            return min(a, b)
    if "min" in raw and "max" in raw:
        nums = re.findall(r"\d+(?:[.,]\d+)?", raw)
        if len(nums) >= 2:
            a = _to_float(nums[0])
            b = _to_float(nums[1])
            if a is not None and b is not None:
                return min(a, b)
    return None


def _extract_requested_quantity(
    block: list[str],
    regex_kv: dict[str, Any],
    requested_chars: dict[str, list[str]],
) -> float | None:
    maps: list[dict[str, Any]] = []
    if isinstance(regex_kv, dict):
        maps.append(regex_kv)
    if isinstance(requested_chars, dict):
        maps.append(requested_chars)

    for mapping in maps:
        for key, raw in mapping.items():
            key_norm = _norm(str(key or ""))
            if not any(t in key_norm for t in ("quantite", "quantit", "qte", "qty", "nombre", "nbr", "nb")):
                continue
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                text = str(value or "").strip()
                ranged = _parse_quantity_range(text)
                if ranged is not None:
                    return ranged
                m = re.search(r"\d+(?:[.,]\d+)?", text)
                if not m:
                    continue
                parsed = _to_float(m.group(0))
                if parsed is not None:
                    return parsed

    for raw_line in block:
        line = _clean_line(raw_line)
        if not line:
            continue
        low = _norm(line)

        ranged = _parse_quantity_range(low)
        if ranged is not None:
            return ranged

        m_labeled = re.search(
            r"\b(?:qte|qt[eé]|qty|nombre|nbr|nb|quantit[^\s:]*)(?:\s+\S+){0,3}\s*[:\-]?\s*(\d+(?:[.,]\d+)?)",
            low,
            re.IGNORECASE,
        )
        if m_labeled:
            parsed = _to_float(m_labeled.group(1))
            if parsed is not None:
                return parsed

        if re.search(r"\b(mm|cm|m|kg|g|l|litre|litres|w|kw|v|hz|bar)\b", low):
            continue
        m_pack = re.search(r"(?<!\d\s)\b(?:x|×)\s*(\d{1,6})(?!\s*[x×*]\s*\d)\b", low)
        if m_pack:
            parsed = _to_float(m_pack.group(1))
            if parsed is not None:
                return parsed

    return None


def _strip_quantity_characteristics(mapping: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (mapping or {}).items():
        key_norm = _norm(str(key or ""))
        if any(token in key_norm for token in ("quantite", "quantit", "qte", "qty", "nombre", "nbr", "nb")):
            continue
        out[key] = value
    return out


def _clean_line(line: str) -> str:
    return " ".join(line.replace("\t", " ").split()).strip()


def _to_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for raw in values:
        if isinstance(raw, dict):
            text = _clean_line(
                str(
                    raw.get("text")
                    or raw.get("value")
                    or raw.get("name")
                    or raw.get("label")
                    or ""
                )
            )
        else:
            text = _clean_line(str(raw or ""))
        if text:
            out.append(text)
    return out


def _merge_text_lists(base: Any, incoming: Any, *, limit: int = 120) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in _to_text_list(base) + _to_text_list(incoming):
        token = _norm(raw)
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(raw[:420])
        if len(out) >= limit:
            break
    return out


def _looks_like_requirement_line(text: str) -> bool:
    line = _clean_line(text)
    if not line:
        return False
    norm = _norm(line)
    return any(hint in norm for hint in _EXIGENCE_LINE_HINTS)


def _is_noisy_ocr_line(text: str) -> bool:
    line = _clean_line(text)
    if not line:
        return False
    noisy_tokens = ("Ã", "Â", "â", "�", "Ð", "Ñ", "??", ">>>", ";;", ",,")
    if any(tok in line for tok in noisy_tokens):
        return True
    allowed_punct = set(" .,:;()[]{}+-*/%°<>_=|/'\"\\")
    odd = sum(1 for ch in line if not (ch.isalpha() or ch.isdigit() or ch in allowed_punct))
    ratio = odd / max(len(line), 1)
    return ratio >= 0.18


def _accessory_structural_score(text: str) -> float:
    line = _clean_line(text)
    if not line:
        return 0.0
    norm = _norm(line)
    words = [w for w in line.split() if w]
    score = 0.0

    if re.match(r"^\s*[•\-]\s*\d{1,3}\b", line):
        score += 2.4
    if _ACCESSORY_LEADING_QTY_RE.match(line):
        score += 2.0
    if _ACCESSORY_MATERIAL_RE.search(norm):
        score += 0.9
    if _ACCESSORY_DIMENSION_RE.search(line):
        score += 0.6
    if re.search(r"\b(?:avec|sans|pour)\b", norm):
        score += 0.35
    if any(hint in norm for hint in _ACCESSORY_LINE_HINTS):
        score += 0.8
    if 2 <= len(words) <= 22:
        score += 0.25

    if _is_qty_header_line(line):
        score -= 2.0
    if _looks_like_requirement_line(line):
        score -= 1.2
    if _is_product_heading(line):
        score -= 1.0
    if "a indiquer" in norm or "à indiquer" in norm:
        score -= 0.5

    if ":" in line and not _ACCESSORY_LEADING_QTY_RE.match(line):
        left = _norm(line.split(":", 1)[0])
        if any(token in left for token in _TECHNICAL_KV_HINTS):
            score -= 1.1

    return score


def _looks_like_accessory_section_header(text: str) -> bool:
    line = _clean_line(text)
    if not line:
        return False
    norm = _norm(line)
    if _looks_like_requirement_line(line):
        return False
    if any(h in norm for h in _ACCESSORY_SECTION_HINTS):
        return True
    if line.endswith(":") and _accessory_structural_score(line[:-1]) >= 1.0:
        return True
    return False


def _extract_structured_lists_from_block(
    block_lines: list[str],
    product_title: str,
    *,
    aggressive_mode: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    accessories: list[str] = []
    exigences: list[str] = []
    to_verify: list[str] = []

    accessory_mode = False
    title_norm = _norm(product_title)

    for raw in block_lines[1:260]:
        line = _clean_line(raw)
        if not line:
            continue
        norm = _norm(line)
        if norm == title_norm:
            continue
        if "camscanner" in norm or norm.startswith("scann"):
            continue
        if _is_qty_header_line(line):
            continue
        if _is_product_heading(line):
            continue
        if len(line) < 3:
            continue

        looks_accessory_line = _looks_like_accessory_line(line)
        has_section_hint = _looks_like_accessory_section_header(line)
        has_requirement = _looks_like_requirement_line(line)
        looks_qty_accessory = bool(
            re.match(r"^\s*(?:[•\-]\s*)?\d{1,3}(?:[.,]\d+)?\s*[A-Za-zÀ-ÖØ-öø-ÿ]", line)
        )
        looks_technical_kv = ":" in line and not has_requirement and not looks_accessory_line

        if has_section_hint:
            accessory_mode = True
            if line.endswith(":") and not has_requirement:
                accessories = _merge_text_lists(accessories, [line], limit=180)
            if has_requirement:
                exigences = _merge_text_lists(exigences, [line], limit=140)
            continue

        if has_requirement:
            exigences = _merge_text_lists(exigences, [line], limit=140)
            if any(token in norm for token in ("accessoire", "instrument", "livre avec", "fourni avec")):
                accessory_mode = True
            continue

        if aggressive_mode and any(token in norm for token in _ACCESSORY_REQUIREMENT_TERMS):
            accessory_mode = True
            accessories = _merge_text_lists(accessories, [line], limit=180)
            continue

        if looks_qty_accessory or (accessory_mode and (looks_accessory_line or not looks_technical_kv)):
            accessories = _merge_text_lists(accessories, [line], limit=180)
            continue

        if looks_accessory_line and not looks_technical_kv:
            accessories = _merge_text_lists(accessories, [line], limit=180)
            continue

        if aggressive_mode and accessory_mode and not looks_technical_kv:
            accessories = _merge_text_lists(accessories, [line], limit=180)
            continue

        if _looks_like_ambiguous_fragment(line) and len(line.split()) >= 6 and _is_noisy_ocr_line(line):
            to_verify = _merge_text_lists(to_verify, [line], limit=120)

    return accessories, exigences, to_verify


def _looks_like_ambiguous_fragment(fragment: str) -> bool:
    frag = _clean_line(fragment)
    if len(frag) < 6:
        return False
    norm = _norm(frag)
    if _AMBIGUOUS_KV_RE.search(frag):
        return True
    if any(tok in norm for tok in _TEXTUAL_SIGNAL_KEYWORDS):
        return True
    if re.search(r"\bclasse\s+[a-z0-9]+\b", norm):
        return True
    has_number = bool(re.search(r"\d", frag))
    has_unit = any(re.search(rf"\b{re.escape(unit)}\b", norm) for unit in _AMBIGUOUS_SIGNAL_UNITS)
    return has_number and has_unit


def _extract_ambiguous_fragments(raw_text: str, product_name: str) -> list[str]:
    clean_raw = _clean_line(raw_text)
    if not clean_raw:
        return []

    product_norm = _norm(product_name)
    fragments = _AMBIGUOUS_FRAGMENT_SPLIT_RE.split(clean_raw)
    if len(fragments) <= 1:
        fragments = [clean_raw]

    out: list[str] = []
    seen: set[str] = set()
    for fragment in fragments:
        frag = _clean_line(fragment)
        if not frag:
            continue
        # Only flag ambiguous fragments when OCR quality is actually noisy.
        if not _is_noisy_ocr_line(frag):
            continue
        frag_norm = _norm(frag)
        if not frag_norm or frag_norm == product_norm:
            continue
        if not _looks_like_ambiguous_fragment(frag):
            continue
        if frag_norm in seen:
            continue
        seen.add(frag_norm)
        out.append(frag[:180])
        if len(out) >= 4:
            break
    return out


def _estimate_name_confidence(product_name: str, doc_type: str) -> float:
    norm = _norm(product_name)
    if not norm:
        return 0.0
    words = [w for w in norm.split() if w]
    has_keyword = any(k in norm for k in _PRODUCT_KEYWORDS)
    if has_keyword and len(words) >= 2:
        return 0.9
    if len(words) >= 3:
        return 0.75 if doc_type == "product_list" else 0.70
    if len(words) == 2:
        return 0.62 if doc_type == "product_list" else 0.58
    return 0.45


def _count_product_keyword_hits(text_norm: str) -> int:
    return sum(1 for key in _PRODUCT_KEYWORDS if key in text_norm)


def _sanitize_product_title(text: str) -> str:
    title = _clean_line(_HEADING_PREFIX_RE.sub("", text))
    if not title:
        return ""
    title = re.sub(
        r"\b(?:qte|qt[eé]|qty|nombre|nbr|nb|quantit[^\s:]*)(?:\s+\S+){0,3}\s*[:\-]?\s*\d+(?:[.,]\d+)?",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*(?:unite|unites|unit[eé]|unit[eé]s|piece|pieces|pcs)\b",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s*[-|;,.:]\s*$", "", title)
    return _clean_line(title)


def _normalize_product_display_name(name: str) -> str:
    clean = _sanitize_product_title(str(name or ""))
    if not clean:
        return ""
    normalized = clean
    for pattern, replacement in _PRODUCT_OCR_CORRECTIONS:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    # Prefer normalized title but avoid returning too short/noisy output.
    if len(normalized) < 4:
        return clean
    return normalized[:240]


def _product_name_key(name: str) -> str:
    clean = _normalize_product_display_name(str(name or ""))
    if not clean:
        return ""
    # Normalize indexed variants: "01 Product", "1/ Product", "1) Product", etc.
    clean = re.sub(r"^\s*\d{1,2}\s+", "", clean)
    clean = re.sub(r"^\s*\d{1,2}[)\./-]\s*", "", clean)
    clean = _clean_line(_HEADING_PREFIX_RE.sub("", clean))
    return _norm(clean)


def _explode_compound_product_line(line: str) -> list[str]:
    clean = _clean_line(line)
    if not clean:
        return []

    norm = _norm(clean)
    hits = _count_product_keyword_hits(norm)
    has_enum = bool(_ENUMERATED_PRODUCT_RE.search(clean))
    has_bare_enum = False
    if not has_enum:
        # OCR often drops punctuation in indexed rows: "01 PRODUCT ... 02 PRODUCT ...".
        # Detect only when we clearly have at least two indexed title starts.
        has_bare_enum = len(list(_ENUMERATED_BARE_PRODUCT_RE.finditer(clean))) >= 2
    has_separator = ";" in clean or " - " in clean or ". " in clean
    should_split = has_enum or has_bare_enum or hits >= 2 or (len(clean) >= 120 and has_separator)
    if not should_split:
        return [clean]

    chunks: list[str] = [clean]

    if has_enum:
        exploded: list[str] = []
        for chunk in chunks:
            parts = re.split(r"(?=(?:^|\s)\d{1,2}[)\.-]\s+[A-Za-z])", chunk)
            for part in parts:
                part_clean = _clean_line(part)
                if part_clean:
                    exploded.append(part_clean)
        chunks = exploded or chunks
    elif has_bare_enum:
        exploded = []
        for chunk in chunks:
            parts = re.split(
                r"(?=(?:^|\s)\d{1,2}\s+(?=[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ]{2,}))",
                chunk,
            )
            for part in parts:
                part_clean = _clean_line(part)
                if part_clean:
                    exploded.append(part_clean)
        # Keep only plausible indexed titles as split anchors; merge noisy
        # fragments (e.g. "28 PARAMETRES") into the previous chunk.
        if exploded:
            merged: list[str] = []
            for part_clean in exploded:
                m = _BARE_ENUM_HEAD_RE.match(part_clean)
                if m is None:
                    if merged:
                        merged[-1] = _clean_line(f"{merged[-1]} {part_clean}")
                    else:
                        merged.append(part_clean)
                    continue

                tail = _clean_line(m.group(2))
                tail_norm = _norm(tail)
                tail_words = [w for w in tail.split() if w]
                first_norm = _norm(tail_words[0]) if tail_words else ""
                has_keyword = any(k in tail_norm for k in _PRODUCT_KEYWORDS)
                is_non_title = first_norm in _BARE_ENUM_NON_TITLE_TOKENS
                looks_anchor = bool(
                    tail_words
                    and not is_non_title
                    and (has_keyword or (len(tail_words) >= 2 and len(tail) >= 10))
                )
                if looks_anchor:
                    merged.append(part_clean)
                elif merged:
                    merged[-1] = _clean_line(f"{merged[-1]} {part_clean}")
                else:
                    merged.append(part_clean)
            chunks = merged
        else:
            chunks = chunks

    exploded = []
    for chunk in chunks:
        if ";" in chunk:
            parts = [_clean_line(p) for p in chunk.split(";") if _clean_line(p)]
            exploded.extend(parts if parts else [chunk])
        else:
            exploded.append(chunk)
    chunks = exploded

    exploded = []
    for chunk in chunks:
        chunk_norm = _norm(chunk)
        chunk_hits = _count_product_keyword_hits(chunk_norm)
        if chunk_hits >= 2 and ". " in chunk:
            parts = [_clean_line(p) for p in re.split(r"\.\s+(?=[A-Z])", chunk) if _clean_line(p)]
            exploded.extend(parts if len(parts) > 1 else [chunk])
        else:
            exploded.append(chunk)
    chunks = exploded

    exploded = []
    for chunk in chunks:
        chunk_norm = _norm(chunk)
        chunk_hits = _count_product_keyword_hits(chunk_norm)
        if chunk_hits >= 2 and " - " in chunk and len(chunk) >= 90:
            parts = [_clean_line(p) for p in chunk.split(" - ") if _clean_line(p)]
            exploded.extend(parts if len(parts) > 1 else [chunk])
        else:
            exploded.append(chunk)
    chunks = exploded

    out: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        c = _clean_line(chunk)
        if len(c) < 3:
            continue
        key = _norm(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c[:500])
        if len(out) >= 30:
            break
    return out or [clean]


def _explode_compound_product_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    idx = 0
    while idx < len(lines):
        line = _clean_line(lines[idx])
        if not line:
            idx += 1
            continue

        merged_line = line
        # OCR often wraps indexed titles on the next line:
        # "01 ANALYSEUR ..." + "MULTIPARAMETRIQUE ...".
        if _INDEXED_TITLE_RE.match(line) and idx + 1 < len(lines):
            nxt = _clean_line(lines[idx + 1])
            if (
                nxt
                and _INDEXED_TITLE_RE.match(nxt) is None
                and not _is_qty_header_line(nxt)
                and _parse_simple_qty_line(nxt) is None
            ):
                nxt_norm = _norm(nxt)
                continuation_like = bool(
                    len(nxt.split()) <= 8
                    and not nxt_norm.startswith("lot")
                    and not nxt_norm.startswith("designation")
                )
                if continuation_like:
                    merged_line = _clean_line(f"{line} {nxt}")
                    idx += 1

        chunks = _explode_compound_product_line(merged_line)
        if chunks:
            out.extend(chunks)
        idx += 1
    return out


def _decorate_preview_item(
    item: dict[str, Any],
    *,
    raw_text: str,
    raw_product_name: str | None = None,
    description_confidence: float | None = None,
) -> dict[str, Any]:
    row = dict(item)
    doc_type = str(row.get("doc_type") or "")
    product_name = _clean_line(str(row.get("product_name") or ""))
    base_description = _clean_line(str(row.get("description") or ""))
    clean_raw = _clean_line(raw_text)
    clean_raw_name = _clean_line(raw_product_name or product_name)

    ambiguous = _extract_ambiguous_fragments(clean_raw, product_name)
    if ambiguous:
        marker = "Infos ambigues (faible confiance): " + " | ".join(ambiguous)
        if base_description:
            if marker not in base_description:
                base_description = f"{base_description} | {marker}"
        else:
            base_description = marker

    name_conf = round(_estimate_name_confidence(product_name, doc_type), 2)
    desc_conf = description_confidence
    if desc_conf is None:
        if not base_description:
            desc_conf = 0.0
        elif ambiguous:
            desc_conf = 0.55 if name_conf >= 0.75 else 0.35
        else:
            desc_conf = 0.85
    elif ambiguous:
        desc_conf = min(float(desc_conf), 0.55 if name_conf >= 0.75 else 0.35)

    row["product_name"] = product_name[:240]
    row["description"] = base_description[:640]
    row["raw_text"] = clean_raw[:1800]
    row["raw_product_name"] = clean_raw_name[:320]
    row["ambiguous_fragments"] = ambiguous
    row["description_confidence"] = round(max(0.0, min(1.0, float(desc_conf))), 2)
    row["name_confidence"] = name_conf
    row["accessoires_structures"] = _merge_text_lists([], row.get("accessoires_structures"))
    row["exigences"] = _merge_text_lists([], row.get("exigences"))
    row["a_verifier"] = _merge_text_lists([], row.get("a_verifier"))
    if ambiguous:
        row["a_verifier"] = _merge_text_lists(
            row.get("a_verifier"),
            [f"Ambigu: {frag}" for frag in ambiguous],
        )
    row["needs_review"] = bool(row["name_confidence"] < 0.45 or (ambiguous and row["name_confidence"] < 0.70))
    return row


def _is_product_heading(line: str) -> bool:
    ln = _clean_line(line)
    if len(ln) < 6 or len(ln) > 120:
        return False

    letters = [ch for ch in ln if ch.isalpha()]
    if len(letters) < 4:
        return False
    upper_ratio = (sum(1 for ch in letters if ch.isupper()) / len(letters)) if letters else 0.0
    words = [w for w in ln.split() if w]
    if len(words) < 2 or len(words) > 14:
        return False

    norm = _norm(ln)
    has_keyword = any(k in norm for k in _PRODUCT_KEYWORDS)

    if ":" in ln:
        left = _clean_line(ln.split(":", 1)[0])
        left_norm = _norm(left)
        technical_left_prefixes = (
            "cadence",
            "temperature",
            "pression",
            "capacite",
            "dimensions",
            "connectivite",
            "conditionnement",
            "usage",
            "principe",
            "classe",
            "type",
            "forme",
            "longueur",
            "largeur",
            "hauteur",
            "couleur",
            "materiau",
            "matiere",
        )
        if not has_keyword:
            return False
        if len(left.split()) < 2:
            return False
        if any(left_norm.startswith(prefix) for prefix in technical_left_prefixes):
            return False

    if norm.startswith("caracteristiques") or norm.startswith("fiche technique"):
        return False
    if norm.startswith("specification"):
        return False
    if norm.startswith("dimensions") or norm.startswith("largeur"):
        return False

    # Avoid treating short technical/value lines as product names.
    if len(words) <= 2 and not has_keyword:
        return False
    if any(tok in norm for tok in ["ip x", "f10", "t4 a", "hz", "volt", "watt"]) and not has_keyword:
        return False

    # Sentence-case product headings (common in scanned requirement sheets).
    if has_keyword:
        description_verb_markers = (
            "permet ",
            "permettant",
            "dispose",
            "offrant",
            "fonctionnement",
            "doit permettre",
        )
        if any(marker in norm for marker in description_verb_markers):
            # Keep concise technical titles with ":" but reject narrative description lines.
            if not (":" in ln and len(words) <= 9):
                return False

        starts_like_sentence = bool(words and words[0][:1].isupper())
        looks_requirement_clause = (
            norm.startswith("l'analyseur")
            or norm.startswith("le ")
            or norm.startswith("la ")
            or norm.startswith("les ")
        ) and any(v in norm for v in ("doit", "dispose", "permet", "sont", "est"))
        if starts_like_sentence and not looks_requirement_clause:
            first_word_norm = _norm(words[0]) if words else ""
            first_two_norm = _norm(" ".join(words[:2])) if words else ""
            starts_with_keyword = any(
                first_word_norm.startswith(k) or first_two_norm.startswith(k)
                for k in _PRODUCT_KEYWORDS
            )
            if starts_with_keyword and len(words) <= 11 and not ln.endswith("."):
                return True
            if len(words) <= 14 and upper_ratio >= 0.18:
                return True

    # Accept numbered/bulleted headings or strongly title-like uppercase lines.
    if _HEADING_PREFIX_RE.match(ln) and (upper_ratio >= 0.5 or has_keyword):
        return True
    return upper_ratio >= 0.55 and (len(words) >= 3 or has_keyword)


def _split_product_blocks(lines: list[str]) -> list[list[str]]:
    heading_indices: list[int] = []
    for idx, line in enumerate(lines):
        is_heading = _is_product_heading(line) or _is_short_contextual_product_heading(lines, idx)
        if not is_heading:
            continue
        if heading_indices and idx - heading_indices[-1] <= 2:
            # Keep first heading when OCR duplicates nearby title/detail variants.
            continue
        heading_indices.append(idx)

    if not heading_indices:
        return [lines] if lines else []

    blocks: list[list[str]] = []
    for pos, start in enumerate(heading_indices):
        end = heading_indices[pos + 1] if pos + 1 < len(heading_indices) else len(lines)
        block = lines[start:end]
        if block:
            blocks.append(block)
    return blocks


def _is_title_priority_block(block_lines: list[str]) -> bool:
    if not block_lines:
        return False
    title = _clean_line(block_lines[0])
    if not title or ":" in title:
        return False
    words = [w for w in title.split() if w]
    if len(words) < 2 or len(words) > 5:
        return False
    if len(block_lines) < 3:
        return False
    second = _clean_line(block_lines[1])
    third = _clean_line(block_lines[2])
    if not second or not third:
        return False
    if not re.fullmatch(r"\d{1,3}", second):
        return False
    third_norm = _norm(third)
    if ":" in third:
        return True
    if any(k in third_norm for k in _PRODUCT_KEYWORDS):
        return True
    if re.search(r"\b(?:na\+|k\+|cl-?|ph|po2|pco2|mmol|mg/?dl|°c|hz|v|w)\b", third_norm):
        return True
    return False


def _is_short_contextual_product_heading(lines: list[str], idx: int) -> bool:
    if idx < 0 or idx >= len(lines):
        return False
    line = _clean_line(lines[idx])
    if not line:
        return False
    if _is_product_heading(line):
        return False
    if ":" in line:
        return False
    if _is_qty_header_line(line) or _looks_like_requirement_line(line):
        return False
    if re.search(r"\d", line):
        return False

    words = [w for w in line.split() if w]
    if len(words) < 2 or len(words) > 5:
        return False
    if len(line) < 8 or len(line) > 72:
        return False

    norm = _norm(line)
    if norm in {"lot", "art", "page", "pub", "to"}:
        return False
    if any(norm.startswith(prefix) for prefix in _NON_PRODUCT_TITLE_PREFIXES):
        return False

    starts_title_case = bool(words and words[0][:1].isupper())
    if not starts_title_case:
        return False

    prev_line = _clean_line(lines[idx - 1]) if idx > 0 else ""
    next_line = _clean_line(lines[idx + 1]) if idx + 1 < len(lines) else ""
    next_slice = [_clean_line(ln) for ln in lines[idx + 1 : idx + 4] if _clean_line(ln)]
    if not next_slice:
        return False
    next_joined = " ".join(next_slice)
    next_norm = _norm(next_joined)

    score = 0
    prev_is_index = bool(prev_line and re.fullmatch(r"\d{1,3}", prev_line))
    next_is_index = bool(next_line and re.fullmatch(r"\d{1,3}", next_line))
    if prev_is_index:
        score += 1
    if next_is_index:
        score += 1
    if ":" in next_joined:
        score += 1
    if any(k in next_norm for k in _PRODUCT_KEYWORDS):
        score += 1
    if re.search(r"\b(?:na\+|k\+|cl-?|ph|po2|pco2|mmol|mg/?dl|°c|hz|v|w)\b", next_norm):
        score += 1
    if any(tok in next_norm for tok in ("parametre", "parametres", "type d'echantillon", "livre avec")):
        score += 1

    # Require local index context for short titles to avoid splitting on generic clauses.
    if not (prev_is_index or next_is_index):
        return False
    return score >= 3


def _looks_like_collapsed_ocr(raw_text: str, lines: list[str]) -> bool:
    clean_text = _clean_line(raw_text)
    if not clean_text:
        return False
    if len(lines) > 2:
        return False
    if len(clean_text) < 500:
        return False
    punctuation_hits = clean_text.count(":") + clean_text.count(";") + clean_text.count(".")
    numeric_hits = len(re.findall(r"\b\d{1,4}(?:[.,]\d+)?\b", clean_text))
    return punctuation_hits >= 5 or numeric_hits >= 10


def _recover_lines_from_collapsed_ocr(raw_text: str) -> list[str]:
    text = _clean_line(" ".join(str(raw_text or "").split()))
    if not text:
        return []

    parts = [text]
    split_patterns = [
        r"(?<=[\.\?!;])\s+(?=[A-ZÀ-ÖØ-Þ])",
        r"(?<=:)\s+(?=(?:QTE|Qte|Qt[ée]|Quantit|Livr[ée]|Le\s|La\s|Les\s|Un\s|Une\s|[A-ZÀ-ÖØ-Þ]))",
        r"\s+(?=(?:\d{1,3}\s+[A-Za-zÀ-ÖØ-öø-ÿ]))",
        r"\s+(?=(?:[A-ZÀ-ÖØ-Þ]{3,}(?:\s+[A-ZÀ-ÖØ-Þ0-9]{2,}){2,}))",
    ]

    for pattern in split_patterns:
        next_parts: list[str] = []
        for part in parts:
            chunks = [c for c in re.split(pattern, part) if _clean_line(c)]
            if chunks:
                next_parts.extend(chunks)
            else:
                next_parts.append(part)
        parts = next_parts

    out: list[str] = []
    for part in parts:
        clean = _clean_line(part)
        if not clean:
            continue
        if len(clean) <= 260:
            out.append(clean)
            continue

        # Long leftovers: split by nearest space around window to keep semantic chunks.
        start = 0
        while start < len(clean):
            end = min(start + 240, len(clean))
            if end < len(clean):
                split_at = clean.rfind(" ", start + 150, end)
                if split_at == -1:
                    split_at = end
            else:
                split_at = end
            chunk = _clean_line(clean[start:split_at])
            if chunk:
                out.append(chunk)
            start = split_at
            while start < len(clean) and clean[start] == " ":
                start += 1

    dedup: list[str] = []
    seen: set[str] = set()
    for line in out:
        key = _norm(line)
        if not key or key in seen:
            continue
        seen.add(key)
        dedup.append(line)
    return dedup[:600]


def _is_generic_product_line(line: str) -> bool:
    clean = _clean_line(_HEADING_PREFIX_RE.sub("", line))
    if len(clean) < 4 or len(clean) > 220:
        return False
    if not re.search(r"[A-Za-z]", clean):
        return False
    norm = _norm(clean)
    if any(norm.startswith(p) for p in _NON_PRODUCT_TITLE_PREFIXES):
        return False
    if norm.startswith("cam scanner") or norm.startswith("camscanner"):
        return False
    if _is_qty_header_line(clean):
        return False
    if any(norm.startswith(h) for h in _INVOICE_ROW_FOOTER_HINTS):
        return False

    has_keyword = any(k in norm for k in _PRODUCT_KEYWORDS)
    words = [w for w in clean.split() if w]
    if len(words) == 1 and not has_keyword:
        return False
    if len(words) > 18:
        return False

    digit_count = sum(1 for ch in clean if ch.isdigit())
    if digit_count > max(8, len(clean) // 2) and not has_keyword:
        return False

    if ":" in clean:
        left = clean.split(":", 1)[0]
        if len(left.split()) <= 3 and _looks_like_ambiguous_fragment(clean) and not has_keyword:
            return False

    indexed = _INDEXED_TITLE_RE.match(clean) is not None
    if has_keyword or indexed:
        return True

    letters = [ch for ch in clean if ch.isalpha()]
    upper_ratio = (sum(1 for ch in letters if ch.isupper()) / len(letters)) if letters else 0.0
    return len(words) >= 2 and (upper_ratio >= 0.45 or words[0][0].isupper())


def _split_flexible_product_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    candidate_indices: list[int] = []
    seen_names: set[str] = set()
    for idx, line in enumerate(lines):
        clean = _clean_line(line)
        if not _is_generic_product_line(clean):
            continue
        name = _clean_line(_HEADING_PREFIX_RE.sub("", clean))
        name_norm = _norm(name)
        if not name_norm:
            continue
        if name_norm in seen_names and len(name_norm) > 8:
            continue
        seen_names.add(name_norm)
        candidate_indices.append(idx)

    if not candidate_indices:
        return []

    blocks: list[list[str]] = []
    for pos, start in enumerate(candidate_indices):
        next_idx = candidate_indices[pos + 1] if pos + 1 < len(candidate_indices) else len(lines)
        end = min(next_idx, start + 18)
        block = [_clean_line(ln) for ln in lines[start:end] if _clean_line(ln)]
        if not block:
            continue
        title = _clean_line(_HEADING_PREFIX_RE.sub("", block[0]))
        if not title:
            continue
        blocks.append((start, block))
    return blocks[:120]


def _extract_flexible_product_items(
    lines: list[str],
    emb_threshold: float,
) -> list[dict[str, Any]]:
    blocks = _split_flexible_product_blocks(lines)
    if not blocks:
        return []
    _ = emb_threshold

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for idx, (start_idx, block) in enumerate(blocks, start=1):
        title = _sanitize_product_title(block[0]) or f"Produit {idx}"
        block_text = "\n".join(block)
        regex_kv, regex_score = _extract_light_characteristics(block)
        description = _build_description(block, {"pairs": []})
        accessories, exigences, to_verify = _extract_structured_lists_from_block(block, title)
        emb_kv, emb_ignored = {}, {}
        quantity_probe = _combine_requested_characteristics(regex_kv, emb_kv)
        quantity = _extract_requested_quantity(block, regex_kv, quantity_probe)
        if quantity is None and len(block) >= 2:
            quantity = _parse_simple_qty_line(block[1])
        regex_kv = _strip_quantity_characteristics(regex_kv)
        emb_kv = _strip_quantity_characteristics(emb_kv)
        requested_chars = _combine_requested_characteristics(regex_kv, emb_kv)
        obligations = _to_text_list(regex_kv.get("accessoires_obligatoires"))
        if obligations:
            exigences = _merge_text_lists(exigences, obligations, limit=180)

        title_norm = _norm(title)
        has_keyword = any(k in title_norm for k in _PRODUCT_KEYWORDS)
        if not has_keyword and not quantity and not requested_chars and not description:
            continue
        key = (title_norm, round(float(quantity or 0.0), 3))
        if key in seen:
            continue
        seen.add(key)

        desc_conf = 0.0
        if description:
            desc_conf = 0.78 if regex_kv else 0.42

        items.append(
            _decorate_preview_item(
                {
                    "product_name": title,
                    "description": description,
                    "quantity": quantity,
                    "unit_price": None,
                    "line_total": None,
                    "doc_type": "product_list",
                    "regex_score": round(regex_score, 4),
                    "regex_characteristics": regex_kv,
                    "embedding_characteristics": emb_kv,
                    "embedding_ignored_by_regex": emb_ignored,
                    "requested_characteristics": requested_chars,
                    "accessoires_structures": accessories,
                    "exigences": exigences,
                    "a_verifier": to_verify,
                    "source_index": start_idx,
                },
                raw_text=block_text,
                raw_product_name=block[0] if block else title,
                description_confidence=desc_conf,
            )
        )
    return items[:220]


def _looks_like_requirement_sheet(raw_text: str, lines: list[str], blocks: list[list[str]]) -> bool:
    text_norm = _norm(raw_text)
    hint_hits = sum(1 for token in _REQUIREMENT_DOC_HINTS if token in text_norm)
    heading_count = len(blocks)
    kv_lines = sum(1 for line in lines if ":" in line)
    if hint_hits >= 1 and heading_count >= 2:
        return True
    if heading_count >= 3 and kv_lines >= 4:
        return True
    return False


def _should_keep_requirement_item(
    title: str,
    regex_kv: dict[str, Any],
    emb_kv: dict[str, Any],
) -> bool:
    title_clean = _clean_line(title)
    if not title_clean:
        return False
    title_norm = _norm(title_clean)
    has_keyword = any(k in title_norm for k in _PRODUCT_KEYWORDS)

    if "cam scanner" in title_norm:
        return False
    if any(title_norm.startswith(p) for p in _NON_PRODUCT_TITLE_PREFIXES) and not has_keyword:
        return False

    # If there is no product keyword, require stronger heading-like signal.
    words = [w for w in title_clean.split() if w]
    letters = [ch for ch in title_clean if ch.isalpha()]
    upper_ratio = (sum(1 for ch in letters if ch.isupper()) / len(letters)) if letters else 0.0
    structured_count = len(regex_kv or {}) + len(emb_kv or {})

    if has_keyword:
        return True

    if structured_count <= 0:
        return False
    if len(words) < 2 or len(words) > 14:
        return False
    if structured_count >= 4 and upper_ratio >= 0.45:
        return True
    if structured_count >= 2 and upper_ratio >= 0.55 and len(words) <= 10:
        return True
    return False


def _combine_requested_characteristics(
    regex_kv: dict[str, Any],
    embedding_kv: dict[str, Any],
) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}

    def _append(key: str, value: str | None) -> None:
        k = _canonical_key(str(key or "")).strip().lower()
        v = " ".join(str(value or "").split()).strip()
        if not k or not v:
            return
        bucket = merged.setdefault(k, [])
        token = _norm(v)
        if any(_norm(existing) == token for existing in bucket):
            return
        bucket.append(v)

    for key, raw in (regex_kv or {}).items():
        if isinstance(raw, list):
            for value in raw:
                _append(str(key), str(value))
        elif raw is not None:
            _append(str(key), str(raw))

    for key, raw in (embedding_kv or {}).items():
        if isinstance(raw, list):
            for row in raw:
                if isinstance(row, dict):
                    _append(str(key), str(row.get("value") or ""))
                elif row is not None:
                    _append(str(key), str(row))
        elif isinstance(raw, dict):
            _append(str(key), str(raw.get("value") or ""))
        elif raw is not None:
            _append(str(key), str(raw))

    return merged


def _extract_light_characteristics(block_lines: list[str]) -> tuple[dict[str, Any], float]:
    kv: dict[str, Any] = {}

    def _add(key: str, value: str) -> None:
        canonical = _canonical_key(str(key or "")).strip().lower()
        clean_value = _clean_line(value)
        if not canonical or not clean_value:
            return
        existing = kv.get(canonical)
        if existing is None:
            kv[canonical] = clean_value
            return
        if isinstance(existing, list):
            if any(_norm(v) == _norm(clean_value) for v in existing):
                return
            existing.append(clean_value)
            return
        if _norm(str(existing)) == _norm(clean_value):
            return
        kv[canonical] = [existing, clean_value]

    inspected = 0
    hits = 0
    for raw in block_lines[1:260]:
        line = _clean_line(raw)
        if not line:
            continue
        inspected += 1
        line_hit = False

        if ":" in line:
            left, right = line.split(":", 1)
            if right.strip():
                _add(left, right)
                line_hit = True

        norm = _norm(line)
        m_cap = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(l|litre|litres)\b", line, re.IGNORECASE)
        if m_cap and ("autoclave" in norm or "capacite" in norm or "volume" in norm):
            _add("capacite", f"{m_cap.group(1).replace(',', '.')} L")
            line_hit = True

        m_dim = re.search(
            r"\b\d+(?:[.,]\d+)?\s*[x×*]\s*\d+(?:[.,]\d+)?(?:\s*[x×*]\s*\d+(?:[.,]\d+)?)?\s*(?:mm|cm|m)?\b",
            line,
            re.IGNORECASE,
        )
        if m_dim:
            _add("dimensions", m_dim.group(0))
            line_hit = True

        m_power = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(w|kw|va)\b", line, re.IGNORECASE)
        if m_power:
            _add("puissance", f"{m_power.group(1).replace(',', '.')} {m_power.group(2).lower()}")
            line_hit = True

        m_temp = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(°c|c)\b", line, re.IGNORECASE)
        if m_temp:
            _add("temperature", f"{m_temp.group(1).replace(',', '.')} C")
            line_hit = True

        # Capture obligation/accessory phrases as structured requirement.
        obligation_line = _norm(line)
        has_requirement = (
            ("doit" in obligation_line and any(t in obligation_line for t in _ACCESSORY_REQUIREMENT_TERMS))
            or "livre avec" in obligation_line
            or "fourni avec" in obligation_line
            or "instruments suivants" in obligation_line
        )
        if has_requirement:
            requirement_value = line
            if ":" in line:
                left, right = line.split(":", 1)
                left_norm = _norm(left)
                if right.strip() and any(
                    token in left_norm for token in ("doit", "livre", "fourni", "instrument", "accessoire")
                ):
                    requirement_value = _clean_line(right)
            requirement_value = _clean_line(requirement_value)
            if requirement_value:
                _add("accessoires_obligatoires", requirement_value)
                line_hit = True

        if re.match(r"^\s*[•\-]\s*\d{1,3}\b", line) and any(t in obligation_line for t in _ACCESSORY_REQUIREMENT_TERMS):
            _add("accessoires_obligatoires", line)
            line_hit = True

        if line_hit:
            hits += 1

    score = 0.0 if inspected == 0 else min(1.0, hits / inspected)
    return kv, round(float(score), 4)


def _build_description(block_lines: list[str], parsed: dict[str, Any]) -> str:
    pair_lines = {
        str(p.get("raw_line", "")).strip()
        for p in parsed.get("pairs", [])
        if str(p.get("raw_line", "")).strip() and str(p.get("raw_line", "")).strip() != "COMPOSED"
    }
    candidates: list[str] = []
    for line in block_lines[1:80]:
        ln = _clean_line(line)
        if not ln:
            continue
        if ln in pair_lines:
            continue
        if _is_product_heading(ln):
            continue
        candidates.append(ln)
        if len(candidates) >= 10:
            break

    if not candidates:
        candidates = [_clean_line(ln) for ln in block_lines[1:12] if _clean_line(ln)]

    return " ".join(candidates)[:960]


_INVOICE_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{1,3}(?:[ \u00A0]\d{3})+|\d+)(?:[.,]\d+)?(?![A-Za-z0-9])"
)
_INVOICE_QTY_RE = re.compile(
    r"\b(?:U|QTE|QT[EÉ]|QTY|QUANTITE|QUANTIT[EÉ])\b\s*[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)",
    flags=re.IGNORECASE,
)
_INVOICE_ROW_INDEX_RE = re.compile(r"^\s*(?:\d{1,3}[.)-]\s*|\d{1,2}\s+)")
_INVOICE_ROW_FOOTER_HINTS = (
    "sous total",
    "total",
    "net a payer",
    "montant ttc",
    "montant ht",
    "tva",
    "remise",
)
_INVOICE_ROW_HEADER_HINTS = (
    "designation",
    "description",
    "qte",
    "qty",
    "quantite",
    "prix unitaire",
    "pu",
    "montant",
)
_INDEXED_TITLE_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z].+)$")
_SIMPLE_QTY_LINE_RE = re.compile(r"^\s*(\d{1,5})(?:[.,](\d{1,3}))?\s*$")


def _parse_ocr_number(token: str | None) -> float | None:
    raw = str(token or "").replace("\u00A0", " ").strip()
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9,.\-]", "", raw)
    if not cleaned or cleaned in {"-", ".", ","}:
        return None

    sign = -1.0 if cleaned.startswith("-") else 1.0
    cleaned = cleaned.lstrip("+-")
    if not cleaned:
        return None

    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")
    dec_pos = max(last_dot, last_comma)

    if dec_pos >= 0 and (len(cleaned) - dec_pos - 1) in (1, 2, 3):
        int_part = re.sub(r"[.,]", "", cleaned[:dec_pos])
        frac_part = re.sub(r"[.,]", "", cleaned[dec_pos + 1 :])
        if not int_part and not frac_part:
            return None
        normalized = f"{int_part or '0'}.{frac_part or '0'}"
    else:
        normalized = re.sub(r"[.,]", "", cleaned)
        if not normalized:
            return None

    try:
        return sign * float(normalized)
    except Exception:
        return None


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return _clean_line(text)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda it: it[0]):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))

    out: list[str] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            out.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        out.append(text[cursor:])
    return _clean_line(" ".join(out))


def _format_preview_number(value: float | None) -> str:
    if value is None:
        return "-"
    rounded = round(float(value), 3)
    if float(rounded).is_integer():
        return str(int(rounded))
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def _build_qty_only_item(
    raw_line: str,
    qty_value: float | None,
    qty_span: tuple[int, int] | None = None,
) -> dict[str, Any] | None:
    if qty_value is None:
        return None
    spans = [qty_span] if qty_span is not None else []
    designation = _remove_spans(raw_line, spans)
    designation = re.sub(
        r"\b(?:U|QTE|QT[EÉ]|QTY|QUANTITE|QUANTIT[EÉ])\b",
        " ",
        designation,
        flags=re.IGNORECASE,
    )
    designation = _clean_line(designation)
    if not designation or len(designation) < 3 or not re.search(r"[A-Za-z]", designation):
        return None
    return _decorate_preview_item(
        {
            "product_name": designation[:240],
            "description": f"Qte: {_format_preview_number(qty_value)}",
            "quantity": qty_value,
            "unit_price": None,
            "line_total": None,
            "doc_type": "product_list",
            "regex_score": 0.0,
            "regex_characteristics": {},
            "embedding_characteristics": {},
            "embedding_ignored_by_regex": {},
            "requested_characteristics": {},
        },
        raw_text=raw_line,
        raw_product_name=designation,
        description_confidence=0.72,
    )


def _build_name_only_item(raw_line: str) -> dict[str, Any] | None:
    designation = _clean_line(_HEADING_PREFIX_RE.sub("", raw_line))
    if not designation:
        return None
    if len(designation) < 4 or not re.search(r"[A-Za-z]", designation):
        return None
    return _decorate_preview_item(
        {
            "product_name": designation[:240],
            "description": "",
            "quantity": None,
            "unit_price": None,
            "line_total": None,
            "doc_type": "product_list",
            "regex_score": 0.0,
            "regex_characteristics": {},
            "embedding_characteristics": {},
            "embedding_ignored_by_regex": {},
            "requested_characteristics": {},
        },
        raw_text=raw_line,
        raw_product_name=designation,
        description_confidence=0.58,
    )


def _parse_simple_qty_line(line: str) -> float | None:
    m = _SIMPLE_QTY_LINE_RE.match(str(line or "").strip())
    if not m:
        return None
    raw = m.group(0)
    value = _parse_ocr_number(raw)
    if value is None:
        return None
    if value <= 0 or value > 100000:
        return None
    return value


def _is_qty_header_line(line: str) -> bool:
    norm = _norm(line)
    return norm in {"qte", "qtte", "qty", "quantite", "quantite demandee"}


def _looks_like_tabular_product_list(lines: list[str]) -> bool:
    qty_hits = 0
    multi_num_hits = 0
    indexed_title_hits = 0
    standalone_qty_hits = 0
    for line in lines[:600]:
        ln = _clean_line(line)
        if not ln:
            continue
        if not re.search(r"[A-Za-z]", ln):
            if _parse_simple_qty_line(ln) is not None:
                standalone_qty_hits += 1
            continue
        norm = _norm(ln)
        if any(norm.startswith(hint) for hint in _INVOICE_ROW_FOOTER_HINTS):
            continue
        if _INDEXED_TITLE_RE.match(ln):
            indexed_title_hits += 1
        if _INVOICE_QTY_RE.search(ln):
            qty_hits += 1
        num_hits = len(list(_INVOICE_NUMBER_RE.finditer(ln)))
        if num_hits >= 2:
            multi_num_hits += 1
    return (
        qty_hits >= 2
        or (qty_hits >= 1 and multi_num_hits >= 2)
        or multi_num_hits >= 4
        or (indexed_title_hits >= 2 and standalone_qty_hits >= 2)
    )


def _parse_invoice_like_line(line: str) -> dict[str, Any] | None:
    raw = str(line or "").replace("\u00A0", " ")
    if not raw.strip():
        return None

    # OCR often glues "...966.39AUTOCLAVE..." in a single token.
    raw = re.sub(r"(?<=\d)(?=[A-Z]{2,})", " ", raw)
    raw = _INVOICE_ROW_INDEX_RE.sub("", raw)
    raw = _clean_line(raw)
    if len(raw) < 10:
        return None
    if not re.search(r"[A-Za-z]", raw):
        return None

    norm = _norm(raw)
    if any(norm.startswith(hint) for hint in _INVOICE_ROW_FOOTER_HINTS):
        return None
    header_hits = sum(1 for hint in _INVOICE_ROW_HEADER_HINTS if hint in norm)
    if header_hits >= 2 and not re.search(r"\d", raw):
        return None

    qty_value: float | None = None
    qty_span: tuple[int, int] | None = None
    qty_match = _INVOICE_QTY_RE.search(raw)
    indexed_line = _INDEXED_TITLE_RE.match(raw) is not None
    qty_anchor = -1
    if qty_match:
        qty_value = _parse_ocr_number(qty_match.group(1))
        qty_span = qty_match.span(1)
        qty_anchor = qty_match.end()

    parsed_numbers: list[tuple[re.Match[str], float]] = []
    for match in _INVOICE_NUMBER_RE.finditer(raw):
        value = _parse_ocr_number(match.group(0))
        if value is None:
            continue
        parsed_numbers.append((match, value))

    if qty_match is None and not indexed_line:
        # For product-list mode, avoid parsing arbitrary technical numeric lines.
        # Keep only explicit quantity-marked or indexed rows.
        return None

    if len(parsed_numbers) < 2:
        if qty_value is None:
            return None
        return _build_qty_only_item(raw, qty_value, qty_span)

    unit_match: re.Match[str] | None = None
    amount_match: re.Match[str] | None = None
    unit_price_value: float | None = None
    amount_value: float | None = None

    if qty_anchor >= 0:
        tail = [(m, v) for m, v in parsed_numbers if m.start() >= qty_anchor and v > 0]
        if len(tail) >= 2:
            unit_match, unit_price_value = tail[0]
            amount_match, amount_value = tail[1]

    if unit_match is None or amount_match is None:
        best_pair: tuple[int, int, float] | None = None
        for idx in range(len(parsed_numbers) - 1):
            first_val = parsed_numbers[idx][1]
            second_val = parsed_numbers[idx + 1][1]
            if first_val <= 0 or second_val <= 0:
                continue
            if second_val < first_val * 0.70:
                continue
            score = second_val + (first_val * 0.05)
            if best_pair is None or score > best_pair[2]:
                best_pair = (idx, idx + 1, score)

        if best_pair is not None:
            first_idx, second_idx, _ = best_pair
            unit_match, unit_price_value = parsed_numbers[first_idx]
            amount_match, amount_value = parsed_numbers[second_idx]
        else:
            unit_match, unit_price_value = parsed_numbers[-2]
            amount_match, amount_value = parsed_numbers[-1]

    if unit_match is None or amount_match is None:
        return None
    if unit_price_value is None or amount_value is None:
        return None
    if unit_price_value <= 0 or amount_value <= 0:
        return None
    if amount_value < unit_price_value:
        unit_match, amount_match = amount_match, unit_match
        unit_price_value, amount_value = amount_value, unit_price_value

    unit_token = unit_match.group(0)
    amount_token = amount_match.group(0)
    has_money_shape = (
        any(sep in unit_token for sep in (",", ".", " "))
        or any(sep in amount_token for sep in (",", ".", " "))
        or unit_price_value >= 1000
        or amount_value >= 1000
    )
    if not has_money_shape:
        if qty_value is None:
            last_match, last_value = parsed_numbers[-1]
            if 0 < last_value <= 100000:
                qty_value = last_value
                qty_span = last_match.span()
        return _build_qty_only_item(raw, qty_value, qty_span)

    if qty_value is None:
        unit_start = unit_match.start()
        for idx in range(len(parsed_numbers) - 1, -1, -1):
            candidate = parsed_numbers[idx][1]
            if candidate <= 0:
                continue
            if candidate > amount_value:
                continue
            if candidate > 100000:
                continue
            if parsed_numbers[idx][0].start() > unit_start:
                continue
            qty_value = candidate
            qty_span = parsed_numbers[idx][0].span()
            break

    if qty_value is None and unit_price_value > 0:
        derived = amount_value / unit_price_value
        if 0.01 <= derived <= 100000:
            qty_value = round(derived, 3)

    # If PU and amount were swapped by OCR, correct it.
    if qty_value and qty_value > 0 and unit_price_value > amount_value * 1.2:
        recomputed = amount_value / qty_value
        if recomputed > 0:
            unit_price_value = round(recomputed, 6)

    spans = [amount_match.span(), unit_match.span()]
    if qty_span is not None:
        spans.append(qty_span)
    designation = _remove_spans(raw, spans)
    designation = re.sub(
        r"\b(?:U|QTE|QT[EÉ]|QTY|QUANTITE|QUANTIT[EÉ]|PU|P\.U\.|MONTANT|TOTAL)\b",
        " ",
        designation,
        flags=re.IGNORECASE,
    )
    designation = _clean_line(designation)
    if not designation or len(designation) < 3 or not re.search(r"[A-Za-z]", designation):
        head = _clean_line(raw[: min(amount_match.start(), unit_match.start())])
        designation = head
    if not designation or len(designation) < 3:
        return None

    summary = (
        f"Qte: {_format_preview_number(qty_value)} | "
        f"PU: {_format_preview_number(unit_price_value)} | "
        f"Montant: {_format_preview_number(amount_value)}"
    )

    return _decorate_preview_item(
        {
            "product_name": designation[:240],
            "description": summary,
            "quantity": qty_value,
            "unit_price": round(float(unit_price_value), 6),
            "line_total": round(float(amount_value), 6),
            "doc_type": "product_list",
            "regex_score": 0.0,
            "regex_characteristics": {},
            "embedding_characteristics": {},
            "embedding_ignored_by_regex": {},
            "requested_characteristics": {},
        },
        raw_text=raw,
        raw_product_name=designation,
        description_confidence=0.80,
    )


def _extract_invoice_like_items(lines: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, float, float, float]] = set()
    idx = 0

    while idx < len(lines):
        line = _clean_line(lines[idx])
        parsed = _parse_invoice_like_line(line)

        if parsed is None and idx + 1 < len(lines):
            merged = _clean_line(f"{line} {lines[idx + 1]}")
            parsed = _parse_invoice_like_line(merged)
            if parsed is not None:
                idx += 1

        if parsed is None:
            m = _INDEXED_TITLE_RE.match(line)
            if m is not None:
                title = _clean_line(m.group(2))
                qty_idx = idx + 1
                if qty_idx < len(lines) and _is_qty_header_line(_clean_line(lines[qty_idx])):
                    qty_idx += 1
                qty_value = _parse_simple_qty_line(lines[qty_idx]) if qty_idx < len(lines) else None
                if qty_value is not None:
                    parsed = _build_qty_only_item(title, qty_value)
                    idx = qty_idx

        if parsed is not None:
            parsed["source_index"] = idx
            key = (
                _product_name_key(str(parsed.get("product_name") or "")),
                round(float(parsed.get("quantity") or 0.0), 6),
                round(float(parsed.get("unit_price") or 0.0), 6),
                round(float(parsed.get("line_total") or 0.0), 6),
            )
            if key not in seen:
                seen.add(key)
                items.append(parsed)
        idx += 1

    return items[:120]


def _split_pages_by_scan_marker(lines: list[str]) -> list[tuple[int, list[str]]]:
    pages: list[tuple[int, list[str]]] = []
    current: list[str] = []
    current_start: int | None = None

    for idx, raw in enumerate(lines):
        line = _clean_line(raw)
        if not line:
            continue
        norm = _norm(line)
        if "scann" in norm and "camscanner" in norm:
            if current:
                pages.append((current_start or 0, current))
                current = []
                current_start = None
            continue
        if current_start is None:
            current_start = idx
        current.append(line)

    if current:
        pages.append((current_start or 0, current))
    return pages if pages else [(0, lines)]


def _extract_product_title_items(lines: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = _split_pages_by_scan_marker(lines)
    for page_start, page in pages:
        head_window = page[:30]
        has_table_header = any(
            "designation" in _norm(line)
            and ("equip" in _norm(line) or "produit" in _norm(line))
            for line in head_window
        )
        if not has_table_header:
            continue

        for page_pos, line in enumerate(page):
            clean = _clean_line(line)
            if len(clean) < 6:
                continue
            norm = _norm(clean)
            if page_pos > 70:
                continue
            if any(norm.startswith(hint) for hint in _INVOICE_ROW_FOOTER_HINTS):
                continue
            if _is_qty_header_line(clean):
                continue
            if len(list(_INVOICE_NUMBER_RE.finditer(clean))) >= 2:
                continue

            indexed = _INDEXED_TITLE_RE.match(clean)
            if indexed is None:
                continue
            name = _clean_line(indexed.group(2))
            if not name or not re.search(r"[A-Za-z]", name):
                continue
            name_norm = _norm(name)
            if _is_noise_product_name(name_norm):
                continue
            if any(name_norm.startswith(p) for p in _NON_PRODUCT_TITLE_PREFIXES):
                continue
            if _looks_like_requirement_line(name):
                continue
            has_keyword = any(k in name_norm for k in _PRODUCT_KEYWORDS)
            if not has_keyword and len(name.split()) > 8:
                continue

            key = _norm(name)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                _decorate_preview_item(
                    {
                        "product_name": name[:240],
                        "description": "",
                        "quantity": None,
                        "unit_price": None,
                        "line_total": None,
                        "doc_type": "product_list",
                        "regex_score": 0.0,
                        "regex_characteristics": {},
                        "embedding_characteristics": {},
                        "embedding_ignored_by_regex": {},
                        "requested_characteristics": {},
                        "source_index": page_start + page_pos,
                    },
                    raw_text=clean,
                    raw_product_name=name,
                    description_confidence=0.56,
                )
            )
    return items[:80]


def _merge_product_list_items(
    primary: list[dict[str, Any]],
    title_only: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    index_by_name: dict[str, int] = {}

    for row in primary + title_only:
        name = _product_name_key(str(row.get("product_name") or ""))
        if not name:
            continue
        if _is_noise_product_name(name):
            continue
        idx = index_by_name.get(name)
        if idx is None:
            index_by_name[name] = len(out)
            out.append(row)
            continue

        existing = out[idx]
        if existing.get("quantity") is None and row.get("quantity") is not None:
            existing["quantity"] = row.get("quantity")
        if existing.get("unit_price") is None and row.get("unit_price") is not None:
            existing["unit_price"] = row.get("unit_price")
        if existing.get("line_total") is None and row.get("line_total") is not None:
            existing["line_total"] = row.get("line_total")
        existing_source = existing.get("source_index")
        row_source = row.get("source_index")
        if row_source is not None and (existing_source is None or int(row_source) < int(existing_source)):
            existing["source_index"] = row_source
        if not existing.get("description") and row.get("description"):
            existing["description"] = row.get("description")
        existing["accessoires_structures"] = _merge_text_lists(
            existing.get("accessoires_structures"),
            row.get("accessoires_structures"),
            limit=180,
        )
        existing["exigences"] = _merge_text_lists(
            existing.get("exigences"),
            row.get("exigences"),
            limit=180,
        )
        existing["a_verifier"] = _merge_text_lists(
            existing.get("a_verifier"),
            row.get("a_verifier"),
            limit=180,
        )
        if not existing.get("raw_text") and row.get("raw_text"):
            existing["raw_text"] = row.get("raw_text")
        if not existing.get("raw_product_name") and row.get("raw_product_name"):
            existing["raw_product_name"] = row.get("raw_product_name")
        existing_amb = existing.get("ambiguous_fragments") or []
        row_amb = row.get("ambiguous_fragments") or []
        if isinstance(existing_amb, list) and isinstance(row_amb, list):
            existing["ambiguous_fragments"] = (existing_amb + [x for x in row_amb if x not in existing_amb])[:4]
        if row.get("needs_review") or existing.get("a_verifier"):
            existing["needs_review"] = True
        existing_desc_conf = float(existing.get("description_confidence") or 0.0)
        row_desc_conf = float(row.get("description_confidence") or 0.0)
        if row_desc_conf and (existing_desc_conf == 0.0 or row_desc_conf < existing_desc_conf):
            existing["description_confidence"] = round(row_desc_conf, 2)
        existing_name_conf = float(existing.get("name_confidence") or 0.0)
        row_name_conf = float(row.get("name_confidence") or 0.0)
        if row_name_conf > existing_name_conf:
            existing["name_confidence"] = round(row_name_conf, 2)
    return out[:140]


def _extract_match_tokens(text: str) -> set[str]:
    norm = _norm(text)
    if not norm:
        return set()
    tokens = re.findall(r"[a-z0-9]+", norm)
    return {
        tok
        for tok in tokens
        if len(tok) >= 3 and tok not in _PRODUCT_MATCH_STOPWORDS
    }


def _looks_like_detail_sentence(text: str) -> bool:
    line = _clean_line(text)
    if not line or len(line) < 20:
        return False
    norm = _norm(line)
    if ":" in line:
        return True
    if any(hint in norm for hint in _DETAIL_LINE_HINTS):
        return True
    return bool(re.search(r"\b[a-z]{4,}\b", norm) and len(norm.split()) >= 5)


def _looks_like_accessory_line(text: str) -> bool:
    line = _clean_line(text)
    if not line:
        return False
    if _looks_like_requirement_line(line) and not _ACCESSORY_LEADING_QTY_RE.match(line):
        return False
    score = _accessory_structural_score(line)
    if score >= 1.35:
        return True
    if _ACCESSORY_LEADING_QTY_RE.match(line) and score >= 1.10:
        return True
    if line.endswith(":") and not _looks_like_requirement_line(line) and score >= 1.0:
        return True
    return False


def _looks_like_accessory_list_title(text: str) -> bool:
    line = _clean_line(text)
    if not line:
        return False
    if _looks_like_requirement_line(line):
        return False
    if line.endswith(":") and _accessory_structural_score(line[:-1]) >= 0.9:
        return True
    if _ACCESSORY_LEADING_QTY_RE.match(line) and _accessory_structural_score(line) >= 1.0:
        return True
    return False


def _extract_accessory_obligations(text: str) -> dict[str, Any]:
    line = _clean_line(text)
    if not line:
        return {}

    norm = _norm(line)
    has_requirement = (
        ("doit" in norm and any(t in norm for t in _ACCESSORY_REQUIREMENT_TERMS))
        or "livre avec" in norm
        or "fourni avec" in norm
        or "instruments suivants" in norm
    )
    if not has_requirement:
        return {}

    value = line
    if ":" in line:
        left, right = line.split(":", 1)
        left_norm = _norm(left)
        if right.strip() and any(token in left_norm for token in ("doit", "livre", "fourni", "instrument", "accessoire")):
            value = _clean_line(right)
    value = _clean_line(value)
    if not value:
        return {}
    return {"accessoires_obligatoires": [value[:420]]}


def _coerce_to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def _merge_scalar_characteristics(
    base: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = dict(base or {})
    for key, raw in (incoming or {}).items():
        clean_key = _canonical_key(str(key or "")).strip().lower()
        if not clean_key:
            continue
        current = _coerce_to_list(out.get(clean_key))
        seen = {_norm(str(v)) for v in current}
        for value in _coerce_to_list(raw):
            val_text = _clean_line(str(value or ""))
            if not val_text:
                continue
            token = _norm(val_text)
            if token in seen:
                continue
            seen.add(token)
            current.append(val_text)
        if not current:
            continue
        out[clean_key] = current[0] if len(current) == 1 else current
    return out


def _embedding_entry_text(value: Any) -> str:
    if isinstance(value, dict):
        return _clean_line(str(value.get("value") or ""))
    return _clean_line(str(value or ""))


def _embedding_entry_score(value: Any) -> float:
    if isinstance(value, dict):
        try:
            return float(value.get("score") or 0.0)
        except Exception:
            return 0.0
    return 0.0


def _merge_embedding_characteristics(
    base: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, list[Any]] = {}
    for source in (base or {}, incoming or {}):
        for key, raw in source.items():
            clean_key = _canonical_key(str(key or "")).strip().lower()
            if not clean_key:
                continue
            bucket = out.setdefault(clean_key, [])
            for value in _coerce_to_list(raw):
                text = _embedding_entry_text(value)
                if not text:
                    continue
                if any(_norm(_embedding_entry_text(existing)) == _norm(text) for existing in bucket):
                    continue
                bucket.append(value if isinstance(value, dict) else {"value": text, "score": 0.0})

    for key, values in list(out.items()):
        out[key] = sorted(values, key=_embedding_entry_score, reverse=True)[:3]
    return out


def _append_detail_description(anchor: dict[str, Any], detail: dict[str, Any]) -> None:
    anchor_name = _clean_line(str(anchor.get("product_name") or ""))
    detail_title = _clean_line(str(detail.get("product_name") or ""))
    detail_desc = _clean_line(str(detail.get("description") or ""))
    detail_raw = _clean_line(str(detail.get("raw_text") or ""))

    snippet_parts: list[str] = []
    if detail_title and _norm(detail_title) != _norm(anchor_name):
        snippet_parts.append(detail_title)
    if detail_desc:
        snippet_parts.append(detail_desc)
    elif detail_raw and _norm(detail_raw) != _norm(anchor_name):
        snippet_parts.append(detail_raw[:220])

    if not snippet_parts:
        return

    snippet = " | ".join(snippet_parts)
    existing = _clean_line(str(anchor.get("description") or ""))
    if existing and _norm(snippet) in _norm(existing):
        return
    merged = snippet if not existing else f"{existing} || {snippet}"
    anchor["description"] = merged[:640]

    existing_conf = float(anchor.get("description_confidence") or 0.0)
    incoming_conf = float(detail.get("description_confidence") or 0.45)
    if existing_conf == 0.0 or (incoming_conf and incoming_conf < existing_conf):
        anchor["description_confidence"] = round(max(0.0, min(1.0, incoming_conf)), 2)


def _is_anchor_product_row(row: dict[str, Any]) -> bool:
    name_norm = _norm(str(row.get("product_name") or ""))
    if _is_noise_product_name(name_norm):
        return False
    if row.get("quantity") is not None:
        return True
    if row.get("unit_price") is not None or row.get("line_total") is not None:
        return True
    return False


def _is_detail_candidate_row(row: dict[str, Any]) -> bool:
    if row.get("quantity") is not None:
        return False
    if row.get("unit_price") is not None or row.get("line_total") is not None:
        return False

    if _has_non_empty_characteristics(row):
        return True

    desc = _clean_line(str(row.get("description") or ""))
    raw = _clean_line(str(row.get("raw_text") or ""))
    name = _clean_line(str(row.get("product_name") or ""))

    if desc and _looks_like_detail_sentence(desc):
        return True
    if raw and _looks_like_detail_sentence(raw):
        return True
    if name and _looks_like_detail_sentence(name):
        return True
    if _looks_like_accessory_line(" ".join([name, desc, raw])):
        return True
    return False


def _row_match_text(row: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _clean_line(str(row.get("product_name") or "")),
            _clean_line(str(row.get("description") or "")),
            _clean_line(str(row.get("raw_text") or "")),
        )
        if part
    )


def _looks_like_forward_reference(row: dict[str, Any]) -> bool:
    text = _norm(_row_match_text(row))
    if not text:
        return False
    return text.startswith(
        (
            "le ",
            "la ",
            "les ",
            "il ",
            "elle ",
            "ce ",
            "cet ",
            "cette ",
            "de ",
            "du ",
            "des ",
            "avec ",
            "sans ",
            "pour ",
            "et ",
            "ou ",
        )
    )


def _looks_like_continuation_phrase(text: str) -> bool:
    clean = _clean_line(text)
    if not clean:
        return False
    if re.match(r"^[a-zà-ÿ]{2,}\b", clean):
        return True
    norm = _norm(clean)
    return norm.startswith(("de ", "du ", "des ", "avec ", "sans ", "pour ", "et ", "ou "))


def _row_source_index(row: dict[str, Any], fallback: int) -> int:
    value = row.get("source_index")
    try:
        return int(value)
    except Exception:
        return int(fallback)


def _detect_row_families(text_norm: str) -> set[str]:
    families: set[str] = set()
    for family, tokens in _ROW_FAMILY_TOKENS.items():
        if any(token in text_norm for token in tokens):
            families.add(family)
    return families


def _primary_family(text_norm: str) -> str | None:
    best_family: str | None = None
    best_pos: int | None = None
    for family, tokens in _ROW_FAMILY_TOKENS.items():
        positions = [text_norm.find(token) for token in tokens if text_norm.find(token) >= 0]
        if not positions:
            continue
        pos = min(positions)
        if best_pos is None or pos < best_pos:
            best_pos = pos
            best_family = family
    return best_family


def _best_anchor_index(
    row_idx: int,
    row: dict[str, Any],
    anchors: list[tuple[int, int, dict[str, Any], set[str], set[str]]],
) -> int | None:
    row_text = _row_match_text(row)
    row_tokens = _extract_match_tokens(row_text)
    row_norm = _norm(row_text)
    if not row_tokens and not row_norm:
        return None

    row_source_idx = _row_source_index(row, row_idx)
    prefer_previous = _looks_like_forward_reference(row) or _looks_like_continuation_phrase(
        str(row.get("product_name") or "")
    )
    row_families = _detect_row_families(row_norm)
    row_primary = _primary_family(row_norm)
    row_is_accessory = _looks_like_accessory_line(row_text)
    best_idx: int | None = None
    best_source_idx: int | None = None
    best_score = 0.0
    scored: list[dict[str, Any]] = []

    for anchor_idx, anchor_source_idx, anchor_row, anchor_name_tokens, anchor_all_tokens in anchors:
        score = 0.0
        overlap_name = len(row_tokens & anchor_name_tokens)
        overlap_all = len(row_tokens & anchor_all_tokens)

        if overlap_name > 0:
            denom = max(1.0, (len(row_tokens) + len(anchor_name_tokens)) / 2.0)
            score += 0.65 * (overlap_name / denom)
        elif overlap_all > 0:
            denom = max(1.0, (len(row_tokens) + len(anchor_all_tokens)) / 2.0)
            score += 0.35 * (overlap_all / denom)

        anchor_norm = _norm(str(anchor_row.get("product_name") or ""))
        anchor_families = _detect_row_families(anchor_norm)
        anchor_primary = _primary_family(anchor_norm)

        if row_primary and anchor_primary:
            if row_primary == anchor_primary:
                score += 0.40
            else:
                score -= 0.22
        elif row_families:
            if row_families & anchor_families:
                score += 0.26
            else:
                score -= 0.18

        distance = abs(anchor_source_idx - row_source_idx)
        score += max(0.0, 0.28 - (0.008 * distance))
        if prefer_previous:
            if anchor_source_idx <= row_source_idx:
                score += 0.16
            else:
                score -= 0.08

        scored.append(
            {
                "anchor_idx": anchor_idx,
                "anchor_source_idx": anchor_source_idx,
                "score": score,
                "overlap_name": overlap_name,
                "anchor_primary": anchor_primary,
            }
        )

        if score > best_score:
            best_score = score
            best_idx = anchor_idx
            best_source_idx = anchor_source_idx

    min_score = 0.36 if row_primary else 0.42
    if scored and row_primary and best_idx is not None:
        same_family = [item for item in scored if item.get("anchor_primary") == row_primary]
        if len(same_family) >= 2:
            max_overlap_name = max(int(item.get("overlap_name") or 0) for item in same_family)
            if max_overlap_name == 0:
                nearest_family = min(
                    same_family,
                    key=lambda item: (
                        abs(int(item.get("anchor_source_idx") or 0) - row_source_idx),
                        0 if int(item.get("anchor_source_idx") or 0) <= row_source_idx else 1,
                    ),
                )
                nearest_idx = int(nearest_family.get("anchor_idx"))
                nearest_score = float(nearest_family.get("score") or 0.0)
                if nearest_idx != best_idx and (best_score - nearest_score) <= 0.25:
                    best_idx = nearest_idx
                    best_score = nearest_score
                    best_source_idx = int(nearest_family.get("anchor_source_idx"))

    if (
        scored
        and row_primary
        and prefer_previous
        and best_idx is not None
    ):
        prev_same_family = [
            item
            for item in scored
            if item.get("anchor_primary") == row_primary and int(item.get("anchor_source_idx") or -1) <= row_source_idx
        ]
        if prev_same_family:
            nearest_prev = max(prev_same_family, key=lambda item: int(item.get("anchor_source_idx") or -1))
            prev_idx = int(nearest_prev.get("anchor_idx"))
            prev_score = float(nearest_prev.get("score") or 0.0)
            if prev_idx != best_idx and (best_score - prev_score) <= 0.22:
                best_idx = prev_idx
                best_score = prev_score
                best_source_idx = int(nearest_prev.get("anchor_source_idx"))

    if best_score < min_score:
        row_is_detail = _has_non_empty_characteristics(row) or row_is_accessory
        if row_is_detail and not row_families:
            prev_candidates = [
                (a_idx, a_source_idx)
                for a_idx, a_source_idx, _a_row, _a_name_toks, _a_all_toks in anchors
                if a_source_idx <= row_source_idx
            ]
            if prev_candidates:
                nearest_prev_idx, nearest_prev_source = max(prev_candidates, key=lambda item: item[1])
                if row_source_idx - nearest_prev_source <= 12:
                    return nearest_prev_idx
        return None
    return best_idx


def _extract_inline_row_characteristics(row: dict[str, Any]) -> dict[str, Any]:
    existing = row.get("regex_characteristics")
    if isinstance(existing, dict) and existing:
        return existing
    title = _clean_line(str(row.get("product_name") or ""))
    detail = _clean_line(str(row.get("description") or "")) or _clean_line(str(row.get("raw_text") or ""))
    if not title or not detail:
        return {}
    parsed_kv, _score = _extract_light_characteristics([title, detail])
    parsed_kv = _merge_scalar_characteristics(parsed_kv, _extract_accessory_obligations(detail))
    if _looks_like_accessory_line(detail):
        options = _coerce_to_list(parsed_kv.get("options"))
        if not any(_norm(str(v)) == _norm(detail) for v in options):
            options.append(detail[:260])
        parsed_kv["options"] = options[0] if len(options) == 1 else options
    return parsed_kv or {}


def _extract_row_structured_lists(row: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    accessories = _merge_text_lists([], row.get("accessoires_structures"))
    exigences = _merge_text_lists([], row.get("exigences"))
    to_verify = _merge_text_lists([], row.get("a_verifier"))
    if accessories or exigences or to_verify:
        return accessories, exigences, to_verify

    title = _clean_line(str(row.get("product_name") or "")) or "Produit"
    detail = _clean_line(str(row.get("description") or "")) or _clean_line(str(row.get("raw_text") or ""))
    if not detail:
        return [], [], []
    return _extract_structured_lists_from_block([title, detail], title)


def _merge_detail_into_anchor(anchor: dict[str, Any], detail: dict[str, Any]) -> None:
    regex_existing = anchor.get("regex_characteristics")
    emb_existing = anchor.get("embedding_characteristics")
    req_existing = anchor.get("requested_characteristics")
    regex_incoming = detail.get("regex_characteristics")
    emb_incoming = detail.get("embedding_characteristics")
    req_incoming = detail.get("requested_characteristics")

    regex_map = _merge_scalar_characteristics(
        regex_existing if isinstance(regex_existing, dict) else {},
        regex_incoming if isinstance(regex_incoming, dict) else _extract_inline_row_characteristics(detail),
    )
    emb_map = _merge_embedding_characteristics(
        emb_existing if isinstance(emb_existing, dict) else {},
        emb_incoming if isinstance(emb_incoming, dict) else {},
    )

    req_map = _combine_requested_characteristics(regex_map, emb_map)
    req_map = _merge_scalar_characteristics(
        req_map,
        req_existing if isinstance(req_existing, dict) else {},
    )
    req_map = _merge_scalar_characteristics(
        req_map,
        req_incoming if isinstance(req_incoming, dict) else {},
    )

    normalized_req: dict[str, list[str]] = {}
    for key, value in req_map.items():
        values = [str(v).strip() for v in _coerce_to_list(value) if str(v).strip()]
        if not values:
            continue
        seen: set[str] = set()
        dedup: list[str] = []
        for val in values:
            token = _norm(val)
            if token in seen:
                continue
            seen.add(token)
            dedup.append(val)
        if dedup:
            normalized_req[key] = dedup

    anchor["regex_characteristics"] = regex_map
    anchor["embedding_characteristics"] = emb_map
    anchor["requested_characteristics"] = normalized_req

    detail_accessories, detail_exigences, detail_to_verify = _extract_row_structured_lists(detail)
    anchor["accessoires_structures"] = _merge_text_lists(
        anchor.get("accessoires_structures"),
        detail_accessories,
        limit=180,
    )
    anchor["exigences"] = _merge_text_lists(
        anchor.get("exigences"),
        detail_exigences,
        limit=180,
    )
    anchor["a_verifier"] = _merge_text_lists(
        anchor.get("a_verifier"),
        detail_to_verify,
        limit=180,
    )

    _append_detail_description(anchor, detail)

    existing_amb = anchor.get("ambiguous_fragments") or []
    detail_amb = detail.get("ambiguous_fragments") or []
    if isinstance(existing_amb, list) and isinstance(detail_amb, list):
        anchor["ambiguous_fragments"] = (existing_amb + [x for x in detail_amb if x not in existing_amb])[:6]

    anchor["needs_review"] = bool(
        anchor.get("needs_review")
        or detail.get("needs_review")
        or bool(anchor.get("a_verifier"))
    )


def _attach_textual_characteristics_to_products(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) <= 1:
        return items

    anchors_idx = [idx for idx, row in enumerate(items) if _is_anchor_product_row(row)]
    if not anchors_idx:
        fallback = []
        for idx, row in enumerate(items):
            name_norm = _product_name_key(str(row.get("product_name") or ""))
            if _is_noise_product_name(name_norm):
                continue
            raw_name = _clean_line(str(row.get("product_name") or ""))
            if _looks_like_accessory_list_title(raw_name):
                continue
            if any(key in name_norm for key in _PRODUCT_KEYWORDS) and float(row.get("name_confidence") or 0.0) >= 0.7:
                fallback.append(idx)
        anchors_idx = fallback
    if not anchors_idx:
        return items

    anchors: list[tuple[int, int, dict[str, Any], set[str], set[str]]] = []
    for idx in anchors_idx:
        row = items[idx]
        name_tokens = _extract_match_tokens(str(row.get("product_name") or ""))
        all_tokens = _extract_match_tokens(
            " ".join(
                [
                    str(row.get("product_name") or ""),
                    str(row.get("raw_product_name") or ""),
                    str(row.get("description") or ""),
                ]
            )
        )
        source_idx = _row_source_index(row, idx)
        anchors.append((idx, source_idx, row, name_tokens, all_tokens))

    consumed: set[int] = set()
    for idx, row in enumerate(items):
        if idx in anchors_idx:
            continue
        if not _is_detail_candidate_row(row):
            continue
        best_anchor_idx = _best_anchor_index(idx, row, anchors)
        if best_anchor_idx is None:
            continue
        _merge_detail_into_anchor(items[best_anchor_idx], row)
        consumed.add(idx)

    if not consumed:
        return items
    return [row for idx, row in enumerate(items) if idx not in consumed]


def _consolidate_product_list_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return []

    out: list[dict[str, Any]] = []
    last_anchor_idx: int | None = None

    for row in items:
        raw_name = _clean_line(str(row.get("product_name") or ""))
        name_norm = _product_name_key(str(row.get("product_name") or ""))
        if _is_noise_product_name(name_norm):
            continue

        has_keyword = any(k in name_norm for k in _PRODUCT_KEYWORDS)
        accessory_title = _looks_like_accessory_list_title(raw_name)
        has_priced_or_qty = (
            row.get("quantity") is not None
            or row.get("unit_price") is not None
            or row.get("line_total") is not None
        )
        if accessory_title and last_anchor_idx is not None:
            _merge_detail_into_anchor(out[last_anchor_idx], row)
            continue

        if (has_keyword and not accessory_title) or has_priced_or_qty:
            out.append(row)
            last_anchor_idx = len(out) - 1
            continue

        row_text = _row_match_text(row)
        detail_like = (
            _looks_like_requirement_line(row_text)
            or _looks_like_detail_sentence(row_text)
            or _has_non_empty_characteristics(row)
        )
        if detail_like and last_anchor_idx is not None:
            _merge_detail_into_anchor(out[last_anchor_idx], row)
            continue

        words = [w for w in str(row.get("product_name") or "").split() if w]
        if len(words) <= 7 and float(row.get("name_confidence") or 0.0) >= 0.70:
            out.append(row)
            continue

        if last_anchor_idx is not None:
            _merge_detail_into_anchor(out[last_anchor_idx], row)

    return out[:220]


def _normalize_quantity_value(value: Any) -> int | float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if num <= 0:
        return None
    if abs(num - round(num)) < 1e-6:
        return int(round(num))
    return round(num, 3)


def _normalize_characteristic_key(key: str) -> str:
    clean = _canonical_key(str(key or "")).strip().lower()
    if not clean:
        return ""
    return _CHARACTERISTIC_KEY_ALIASES.get(clean, clean.replace(" ", "_"))


def _normalize_characteristics_payload(
    requested: dict[str, Any] | None,
    regex: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    source = requested if isinstance(requested, dict) and requested else (regex or {})
    if not isinstance(source, dict):
        return []

    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for raw_key, raw_values in source.items():
        key = _normalize_characteristic_key(str(raw_key or ""))
        if not key:
            continue
        if key in {"accessoires_obligatoires", "options"}:
            continue
        values = _merge_text_lists([], raw_values, limit=12)
        if not values:
            continue
        if key in seen_keys:
            # Merge into existing key.
            for row in out:
                if row.get("key") == key:
                    row["values"] = _merge_text_lists(row.get("values"), values, limit=18)
                    break
            continue
        seen_keys.add(key)
        out.append({"key": key, "values": values})
    return out


def _merge_characteristics_output(
    base: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, list[str]] = {}
    for row in (base or []) + (incoming or []):
        key = _normalize_characteristic_key(str(row.get("key") or ""))
        if not key:
            continue
        values = _merge_text_lists([], row.get("values"), limit=24)
        if not values:
            continue
        merged[key] = _merge_text_lists(merged.get(key), values, limit=24)
    return [{"key": key, "values": values} for key, values in merged.items()]


def _merge_object_rows(base: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in (base or []) + (incoming or []):
        token = _norm(json.dumps(row, ensure_ascii=False, sort_keys=True))
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(row)
    return out


def _extract_group_name(line: str) -> str | None:
    norm = _norm(line)
    if "davier" in norm and ("dent de lait" in norm or "enfant" in norm):
        return "Daviers dent de lait"
    if "davier" in norm and ("dents permanentes" in norm or "permanente" in norm):
        return "Daviers dents permanentes"
    return None


def _extract_material_from_line(line: str) -> str | None:
    norm = _norm(line)
    if "inox medical" in norm or "inox médical" in norm:
        return "inox médical"
    if "acier inoxydable" in norm:
        return "acier inoxydable"
    if "inox" in norm:
        return "inox"
    return None


def _strip_material_tokens(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"\b(?:en\s+)?inox\s+m[eé]dical\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:en\s+)?acier\s+inoxydable\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:en\s+)?inox\b", " ", value, flags=re.IGNORECASE)
    return _clean_line(value)


def _parse_accessory_entry(raw_line: str, current_group: str | None) -> tuple[dict[str, Any] | None, str | None]:
    line = _clean_line(raw_line)
    if not line:
        return None, current_group

    group_marker = _extract_group_name(line)
    if group_marker and (line.endswith(":") or "pour" in _norm(line)):
        return None, group_marker

    qty: int | None = None
    work = line
    m_qty = _ACCESSORY_QTY_PREFIX_RE.match(work)
    if m_qty:
        try:
            qty = int(m_qty.group(1))
        except Exception:
            qty = None
        work = _clean_line(work[m_qty.end() :])

    work = _clean_line(re.sub(r"^\W+\s*", "", work))
    if not work:
        return None, current_group

    details: str | None = None
    item_text = work

    # "item : detail" form
    if ":" in work:
        left, right = work.split(":", 1)
        left_clean = _clean_line(left)
        right_clean = _clean_line(right)
        if right_clean and len(left_clean.split()) <= 8:
            item_text = left_clean
            details = right_clean

    # "item avec detail" form
    if details is None and re.search(r"\bavec\b", item_text, flags=re.IGNORECASE):
        left, right = re.split(r"\bavec\b", item_text, maxsplit=1, flags=re.IGNORECASE)
        left_clean = _clean_line(left)
        right_clean = _clean_line(right)
        if left_clean and right_clean:
            item_text = left_clean
            details = f"avec {right_clean}"

    material = _extract_material_from_line(" ".join([item_text, details or ""]))
    item_text = _strip_material_tokens(item_text)
    if details:
        details = _strip_material_tokens(details)

    if not item_text or len(item_text) < 2:
        return None, current_group

    entry: dict[str, Any] = {
        "qty": qty,
        "item": item_text,
    }
    if details:
        entry["details"] = details
    if material:
        entry["material"] = material
    if current_group:
        entry["group"] = current_group
    return entry, current_group


def _normalize_accessoires_payload(
    accessoires: Any,
    exigences: Any,
) -> list[dict[str, Any]]:
    lines = _merge_text_lists(accessoires, [], limit=220)
    # Keep accessory-like obligations from exigences as accessories when explicit.
    for ex in _to_text_list(exigences):
        if _looks_like_accessory_line(ex) and ex not in lines:
            lines.append(ex)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    current_group: str | None = None

    for line in lines:
        group_marker = _extract_group_name(line)
        if group_marker and line.endswith(":"):
            current_group = group_marker
            continue
        entry, current_group = _parse_accessory_entry(line, current_group)
        if not entry:
            continue
        key = _norm(
            f"{entry.get('qty')}|{entry.get('item')}|{entry.get('details')}|{entry.get('material')}|{entry.get('group')}"
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _verify_reason(text: str) -> str:
    norm = _norm(text)
    if _is_noisy_ocr_line(text) or "--" in text:
        return "OCR bruité ou caractères altérés."
    if "a indiquer" in norm or "à indiquer" in norm:
        return "Valeur à renseigner manuellement."
    if _looks_like_accessory_line(text) and not _looks_like_accessory_list_title(text):
        return "Libellé potentiellement incomplet."
    return "Texte ambigu, vérification manuelle recommandée."


def _normalize_verify_payload(values: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in _to_text_list(values):
        token = _norm(line)
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(
            {
                "text": line,
                "reason": _verify_reason(line),
            }
        )
    return out


def _normalize_exigences_payload(values: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in _to_text_list(values):
        if _looks_like_accessory_list_title(line):
            continue
        if _looks_like_accessory_line(line) and not _looks_like_requirement_line(line):
            continue
        token = _norm(line)
        if not token or token in seen:
            continue
        seen.add(token)
        out.append({"text": line})
    return out


def _compute_product_quality(
    *,
    name: str,
    description: str,
    characteristics: list[dict[str, Any]],
    accessoires: list[dict[str, Any]],
    exigences: list[dict[str, Any]],
    to_verify: list[dict[str, Any]],
) -> tuple[float, str]:
    words = [w for w in _clean_line(name).split() if w]
    payload_count = len(characteristics) + len(accessoires) + len(exigences)
    has_description = bool(_clean_line(description))
    has_keyword = any(k in _norm(name) for k in _PRODUCT_KEYWORDS)

    score = 0.0
    if len(words) >= 2:
        score += 0.20
    elif len(words) == 1:
        score += 0.08

    if has_keyword:
        score += 0.15

    if payload_count >= 12:
        score += 0.35
    elif payload_count >= 5:
        score += 0.28
    elif payload_count >= 1:
        score += 0.18
    else:
        score += 0.05

    if has_description:
        score += 0.15

    verify_penalty = min(0.45, 0.07 * len(to_verify))
    score = max(0.0, min(1.0, score - verify_penalty))

    if score >= 0.78:
        return round(score, 2), "high"
    if score >= 0.52:
        return round(score, 2), "medium"
    return round(score, 2), "low"


def _ensure_quality_review_badge(product: dict[str, Any]) -> None:
    score, level = _compute_product_quality(
        name=str(product.get("name") or ""),
        description=str(product.get("description") or ""),
        characteristics=product.get("characteristics") or [],
        accessoires=product.get("accessoires_structures") or [],
        exigences=product.get("exigences") or [],
        to_verify=product.get("a_verifier") or [],
    )
    product["quality_score"] = score
    product["quality_level"] = level

    # Only force review badge for truly weak rows, not medium quality rows.
    needs_badge = level == "low" and score < 0.35
    product["needs_review"] = bool(product.get("a_verifier")) or needs_badge
    if needs_badge and not (product.get("a_verifier") or []):
        product["a_verifier"] = [
            {
                "text": str(product.get("name") or ""),
                "reason": f"Qualité extraction {level} (score={score:.2f})",
            }
        ]


def _is_very_weak_product_candidate(product: dict[str, Any]) -> bool:
    """Fail-closed guardrail: avoid publishing likely false products."""
    score = float(product.get("quality_score") or 0.0)
    qty = _normalize_quantity_value(product.get("quantity"))
    has_payload = bool(
        (product.get("characteristics") or [])
        or (product.get("accessoires_structures") or [])
        or (product.get("exigences") or [])
    )
    has_review = bool(product.get("a_verifier") or [])
    name = _clean_line(str(product.get("name") or ""))
    has_keyword = any(k in _norm(name) for k in _PRODUCT_KEYWORDS)

    # Keep rows that at least have a quantity or structured payload.
    if qty is not None or has_payload:
        return False

    # A weak row with no payload and no business keyword is likely a false anchor.
    if score < 0.42 and not has_keyword:
        return True

    # Very weak + only review noise should also be hidden from final products.
    if score < 0.35 and has_review:
        return True

    return False


def _build_structured_products_payload(
    items: list[dict[str, Any]],
    doc_type: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    products: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    product_index_by_name: dict[str, int] = {}
    current_idx: int | None = None
    block_mode: str | None = None
    block_target_idx: int | None = None

    def _upsert_product(
        *,
        name: str,
        name_norm: str,
        quantity: int | float | None,
        description: str,
        characteristics: list[dict[str, Any]],
        accessoires: list[dict[str, Any]],
        exigences: list[dict[str, Any]],
        to_verify: list[dict[str, Any]],
    ) -> int:
        existing_idx = product_index_by_name.get(name_norm)
        if existing_idx is None:
            products.append(
                {
                    "name": name,
                    "quantity": quantity,
                    "description": description,
                    "characteristics": characteristics,
                    "accessoires_structures": accessoires,
                    "exigences": exigences,
                    "a_verifier": to_verify,
                }
            )
            idx = len(products) - 1
            product_index_by_name[name_norm] = idx
            return idx

        target = products[existing_idx]
        if target.get("quantity") is None and quantity is not None:
            target["quantity"] = quantity
        if description:
            existing_desc = _clean_line(str(target.get("description") or ""))
            if not existing_desc:
                target["description"] = description
            elif _norm(description) not in _norm(existing_desc):
                target["description"] = f"{existing_desc} || {description}"[:1200]
        target["characteristics"] = _merge_characteristics_output(
            target.get("characteristics") or [],
            characteristics,
        )
        target["accessoires_structures"] = _merge_object_rows(
            target.get("accessoires_structures") or [],
            accessoires,
        )
        target["exigences"] = _merge_object_rows(
            target.get("exigences") or [],
            exigences,
        )
        target["a_verifier"] = _merge_object_rows(
            target.get("a_verifier") or [],
            to_verify,
        )
        return existing_idx

    def _merge_into_product(
        target_idx: int,
        *,
        name: str,
        description: str,
        characteristics: list[dict[str, Any]],
        accessoires: list[dict[str, Any]],
        exigences: list[dict[str, Any]],
        to_verify: list[dict[str, Any]],
    ) -> None:
        target = products[target_idx]
        extra_desc = description or name
        if extra_desc:
            existing_desc = _clean_line(str(target.get("description") or ""))
            if not existing_desc:
                target["description"] = extra_desc[:1200]
            elif _norm(extra_desc) not in _norm(existing_desc):
                target["description"] = f"{existing_desc} || {extra_desc}"[:1200]
        target["characteristics"] = _merge_characteristics_output(
            target.get("characteristics") or [],
            characteristics,
        )
        target["accessoires_structures"] = _merge_object_rows(
            target.get("accessoires_structures") or [],
            accessoires,
        )
        target["exigences"] = _merge_object_rows(
            target.get("exigences") or [],
            exigences,
        )
        target["a_verifier"] = _merge_object_rows(
            target.get("a_verifier") or [],
            to_verify,
        )

    def _best_existing_product_for_detail(name: str, description: str) -> int | None:
        if not products:
            return None
        row_text = _clean_line(" ".join([name, description]))
        row_norm = _norm(row_text)
        row_tokens = _extract_match_tokens(row_text)
        row_families = _detect_row_families(row_norm)

        best_idx: int | None = None
        best_score = 0.0
        for idx, prod in enumerate(products):
            prod_text = _clean_line(
                " ".join(
                    [
                        str(prod.get("name") or ""),
                        str(prod.get("description") or ""),
                    ]
                )
            )
            prod_tokens = _extract_match_tokens(prod_text)
            prod_families = _detect_row_families(_norm(prod_text))

            overlap = len(row_tokens & prod_tokens)
            score = float(overlap)
            if row_families and prod_families and (row_families & prod_families):
                score += 1.25
            if current_idx is not None and idx == current_idx:
                score += 0.35
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx is None:
            return None
        if best_score >= 2.0:
            return best_idx
        if best_score >= 1.25 and row_families:
            return best_idx
        return None

    for row in items:
        raw_name = _clean_line(str(row.get("product_name") or ""))
        name = _normalize_product_display_name(raw_name)
        if not name:
            continue

        description = _clean_line(str(row.get("description") or ""))
        quantity = _normalize_quantity_value(row.get("quantity"))
        characteristics = _normalize_characteristics_payload(
            row.get("requested_characteristics") if isinstance(row.get("requested_characteristics"), dict) else None,
            row.get("regex_characteristics") if isinstance(row.get("regex_characteristics"), dict) else None,
        )
        accessoires = _normalize_accessoires_payload(
            row.get("accessoires_structures"),
            row.get("exigences"),
        )
        exigences = _normalize_exigences_payload(row.get("exigences"))
        to_verify = _normalize_verify_payload(row.get("a_verifier"))

        lexicon_match = _match_medical_lexicon_name(name)
        if isinstance(lexicon_match, dict):
            lexicon_canonical = _clean_line(str(lexicon_match.get("canonical") or ""))
            lexicon_score = float(lexicon_match.get("score") or 0.0)
            if lexicon_canonical and bool(lexicon_match.get("auto_correct")):
                canonical_key = _product_name_key(lexicon_canonical)
                if canonical_key and not _is_noise_product_name(canonical_key):
                    name = lexicon_canonical
            elif lexicon_canonical:
                to_verify = _merge_object_rows(
                    to_verify,
                    [
                        {
                            "text": name,
                            "reason": (
                                "Match lexique moyen "
                                f"({lexicon_score:.2f}) -> candidat: {lexicon_canonical}"
                            ),
                        }
                    ],
                )

        name_norm = _product_name_key(name)
        if _is_noise_product_name(name_norm):
            unassigned.append({"text": name, "reason": "Nom générique/non exploitable"})
            continue

        name_conf = float(row.get("name_confidence") or 0.0)
        desc_conf = float(row.get("description_confidence") or 0.0)
        regex_score = float(row.get("regex_score") or 0.0)
        if row.get("needs_review") and not to_verify:
            to_verify = _merge_object_rows(
                to_verify,
                [{"text": name, "reason": "Ligne marquée à vérifier par le moteur"}],
            )

        has_prices = row.get("unit_price") is not None or row.get("line_total") is not None
        has_strong_payload = bool(characteristics or accessoires or exigences or to_verify)
        has_payload_without_verify = bool(characteristics or accessoires or exigences)
        is_accessory_title = _looks_like_accessory_list_title(name)
        is_accessory_line = _looks_like_accessory_line(" ".join([name, description]))
        has_keyword = any(k in name_norm for k in _PRODUCT_KEYWORDS)
        weak_extraction = bool(
            (
                name_conf < 0.50
                and not has_payload_without_verify
                and not has_prices
            )
            or (
                0.0 < desc_conf < 0.28
                and not has_payload_without_verify
                and not has_prices
            )
            or (
                regex_score < 0.12
                and not has_payload_without_verify
                and not has_prices
                and not has_keyword
            )
        )
        if weak_extraction:
            to_verify = _merge_object_rows(
                to_verify,
                [
                    {
                        "text": name,
                        "reason": (
                            "Extraction faible "
                            f"(name_conf={name_conf:.2f}, desc_conf={desc_conf:.2f}, regex={regex_score:.2f})"
                        ),
                    }
                ],
            )
            has_strong_payload = bool(characteristics or accessoires or exigences or to_verify)
        if is_accessory_line and not accessoires:
            accessoires = _merge_object_rows(
                accessoires,
                _normalize_accessoires_payload([name], []),
            )
            has_strong_payload = bool(characteristics or accessoires or exigences or to_verify)
            has_payload_without_verify = bool(characteristics or accessoires or exigences)
        row_text = _clean_line(" ".join([name, description]))
        row_norm = _norm(row_text)
        is_requirement_line = _looks_like_requirement_line(row_text or name)
        opens_accessory_block = bool(
            _looks_like_accessory_section_header(row_text or name)
            or is_accessory_title
            or (
                is_requirement_line
                and any(token in row_norm for token in _ACCESSORY_REQUIREMENT_TERMS)
            )
        )

        # Explicit accessory list title is always attached to the current product when possible.
        if is_accessory_title and current_idx is not None:
            block_mode = "accessories"
            block_target_idx = current_idx
            _merge_into_product(
                current_idx,
                name=name,
                description=description,
                characteristics=[],
                accessoires=accessoires,
                exigences=[],
                to_verify=to_verify,
            )
            continue

        # Decide whether the row starts a new product or is a detail for the previous one.
        looks_like_anchor_heading = _is_product_heading(name) or (
            any(k in name_norm for k in _PRODUCT_KEYWORDS)
            and len(name.split()) <= 14
            and name_conf >= 0.62
            and (has_payload_without_verify or name_conf >= 0.80)
            and not _looks_like_continuation_phrase(name)
            and ":" not in name
            and "a indiquer" not in name_norm
            and not _looks_like_requirement_line(name)
            and not is_accessory_line
        )
        is_anchor = bool(
            row.get("title_priority")
            or quantity is not None
            or has_prices
            or looks_like_anchor_heading
        )
        if is_anchor:
            block_mode = None
            block_target_idx = None
        elif current_idx is not None:
            if opens_accessory_block:
                block_mode = "accessories"
                block_target_idx = current_idx
            elif is_requirement_line and block_mode is None:
                block_mode = "requirements"
                block_target_idx = current_idx

        # Block mode: after a section trigger, keep attaching lines to the same product
        # until a new anchor product is detected.
        if (
            not is_anchor
            and block_mode is not None
            and block_target_idx is not None
        ):
            block_accessoires = accessoires
            block_exigences = exigences
            if block_mode == "accessories":
                if is_requirement_line and not block_exigences:
                    block_exigences = _normalize_exigences_payload([row_text or name])
                if not block_accessoires and not is_requirement_line:
                    block_accessoires = _merge_object_rows(
                        block_accessoires,
                        _normalize_accessoires_payload([row_text or name], []),
                    )
            elif block_mode == "requirements":
                if not block_exigences:
                    block_exigences = _normalize_exigences_payload([row_text or name])

            _merge_into_product(
                block_target_idx,
                name=name,
                description=description,
                characteristics=characteristics,
                accessoires=block_accessoires,
                exigences=block_exigences,
                to_verify=to_verify,
            )
            continue

        if not is_anchor and quantity is None and not has_prices:
            inferred_idx = _best_existing_product_for_detail(name, description)
            if inferred_idx is None and not has_payload_without_verify:
                row_families = _detect_row_families(_norm(" ".join([name, description])))
                if row_families:
                    family_candidates: list[int] = []
                    for idx, prod in enumerate(products):
                        prod_families = _detect_row_families(
                            _norm(str(prod.get("name") or ""))
                        )
                        if row_families & prod_families:
                            family_candidates.append(idx)
                    if len(family_candidates) == 1:
                        inferred_idx = family_candidates[0]
            if inferred_idx is not None:
                _merge_into_product(
                    inferred_idx,
                    name=name,
                    description=description,
                    characteristics=characteristics,
                    accessoires=accessoires,
                    exigences=exigences,
                    to_verify=to_verify,
                )
                continue

        if (
            (current_idx is not None or bool(products))
            and not is_anchor
            and (
                is_accessory_line
                or has_strong_payload
                or _looks_like_requirement_line(row_text)
                or ":" in name
                or "a indiquer" in name_norm
                or len(name.split()) >= 8
            )
        ):
            target_idx = _best_existing_product_for_detail(name, description)
            if target_idx is None and (
                is_accessory_line
                or has_payload_without_verify
                or len(name.split()) >= 8
            ):
                target_idx = current_idx
            if target_idx is not None:
                _merge_into_product(
                    target_idx,
                    name=name,
                    description=description,
                    characteristics=characteristics,
                    accessoires=accessoires,
                    exigences=exigences,
                    to_verify=to_verify,
                )
                continue

        # For product_list docs, weak rows without quantity and without signals become unassigned.
        if (
            doc_type == "product_list"
            and quantity is None
            and not has_prices
            and not has_payload_without_verify
            and (
                len(name.split()) > 8
                or ":" in name
                or "a indiquer" in name_norm
                or len(name.split()) <= 7
            )
        ):
            unassigned.append({"text": name, "reason": "Détail ambigu sans ancre produit"})
            continue

        current_idx = _upsert_product(
            name=name,
            name_norm=name_norm,
            quantity=quantity,
            description=description,
            characteristics=characteristics,
            accessoires=accessoires,
            exigences=exigences,
            to_verify=to_verify,
        )
        block_mode = None
        block_target_idx = None

    # Final quality + fail-closed filtering + product ids.
    filtered_products: list[dict[str, Any]] = []
    for product in products:
        _ensure_quality_review_badge(product)
        if _is_very_weak_product_candidate(product):
            unassigned.append(
                {
                    "text": str(product.get("name") or ""),
                    "reason": "Produit trop incertain (qualité faible, sans données exploitables).",
                }
            )
            continue
        filtered_products.append(product)

    for idx, product in enumerate(filtered_products, start=1):
        product["product_id"] = f"p{idx}"

    return filtered_products, unassigned


def _product_expected_count_rank(product: dict[str, Any]) -> tuple[float, int, int, int]:
    quality_score = float(product.get("quality_score") or 0.0)
    has_qty = 1 if _normalize_quantity_value(product.get("quantity")) is not None else 0
    payload_size = int(
        len(product.get("characteristics") or [])
        + len(product.get("accessoires_structures") or [])
        + len(product.get("exigences") or [])
    )
    review_bonus = 0 if bool(product.get("needs_review")) else 1
    return (quality_score, payload_size, has_qty, review_bonus)


def _enforce_expected_product_count(
    payload: dict[str, Any],
    expected_product_count: int | None,
    *,
    strict_expected_count: bool,
) -> dict[str, Any]:
    if expected_product_count is None:
        return payload
    try:
        expected = int(expected_product_count)
    except Exception:
        return payload
    if expected <= 0:
        return payload

    products_raw = payload.get("products")
    unassigned_raw = payload.get("unassigned")
    products = [p for p in (products_raw if isinstance(products_raw, list) else []) if isinstance(p, dict)]
    unassigned = list(unassigned_raw if isinstance(unassigned_raw, list) else [])

    for product in products:
        if "quality_score" not in product or "quality_level" not in product:
            _ensure_quality_review_badge(product)

    def _merge_dropped_into_target(
        target: dict[str, Any],
        dropped_row: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        if target.get("quantity") is None and dropped_row.get("quantity") is not None:
            target["quantity"] = dropped_row.get("quantity")

        dropped_desc = _clean_line(str(dropped_row.get("description") or ""))
        if dropped_desc:
            existing_desc = _clean_line(str(target.get("description") or ""))
            if not existing_desc:
                target["description"] = dropped_desc[:1200]
            elif _norm(dropped_desc) not in _norm(existing_desc):
                target["description"] = f"{existing_desc} || {dropped_desc}"[:1200]

        target["characteristics"] = _merge_characteristics_output(
            target.get("characteristics") or [],
            dropped_row.get("characteristics") or [],
        )
        target["accessoires_structures"] = _merge_object_rows(
            target.get("accessoires_structures") or [],
            dropped_row.get("accessoires_structures") or [],
        )
        target["exigences"] = _merge_object_rows(
            target.get("exigences") or [],
            dropped_row.get("exigences") or [],
        )
        target["a_verifier"] = _merge_object_rows(
            target.get("a_verifier") or [],
            dropped_row.get("a_verifier") or [],
        )
        dropped_name = _clean_line(str(dropped_row.get("name") or ""))
        target["a_verifier"] = _merge_object_rows(
            target.get("a_verifier") or [],
            [
                {
                    "text": dropped_name or "Produit fusionné",
                    "reason": reason,
                }
            ],
        )
        _ensure_quality_review_badge(target)

    dropped = 0
    merged_dropped = 0
    promoted = 0
    placeholders = 0

    if len(products) > expected and strict_expected_count:
        ranked = sorted(
            enumerate(products),
            key=lambda pair: _product_expected_count_rank(pair[1]),
            reverse=True,
        )
        kept_indices = sorted(idx for idx, _ in ranked[:expected])
        dropped_pairs = sorted(
            [(idx, products[idx]) for idx, _ in ranked[expected:]],
            key=lambda pair: pair[0],
        )
        products = [products[idx] for idx in kept_indices]
        kept_to_new_index = {orig_idx: new_idx for new_idx, orig_idx in enumerate(kept_indices)}
        dropped = len(dropped_pairs)
        for dropped_idx, row in dropped_pairs:
            name = _clean_line(str(row.get("name") or ""))
            if not name:
                continue

            previous_candidates = [idx for idx in kept_indices if idx < dropped_idx]
            next_candidates = [idx for idx in kept_indices if idx > dropped_idx]
            preferred_idx: int | None = None
            if previous_candidates:
                preferred_idx = max(previous_candidates)
            elif next_candidates:
                preferred_idx = min(next_candidates)

            if preferred_idx is not None:
                target = products[kept_to_new_index[preferred_idx]]
                _merge_dropped_into_target(
                    target,
                    row,
                    reason=(
                        "Ligne fusionnée après expected_product_count strict "
                        "(rattachement prioritaire vers le produit précédent)."
                    ),
                )
                merged_dropped += 1
                continue

            unassigned.append(
                {
                    "text": name,
                    "reason": "Retiré (dépasse expected_product_count).",
                    "dropped_payload": {
                        "name": row.get("name"),
                        "quantity": row.get("quantity"),
                        "description": row.get("description"),
                        "characteristics": row.get("characteristics") or [],
                        "accessoires_structures": row.get("accessoires_structures") or [],
                        "exigences": row.get("exigences") or [],
                        "a_verifier": row.get("a_verifier") or [],
                    },
                }
            )

    if len(products) < expected and strict_expected_count:
        seen = {_product_name_key(str(row.get("name") or "")) for row in products}
        seen.discard("")
        remaining_unassigned: list[Any] = []
        for raw in unassigned:
            if len(products) >= expected:
                remaining_unassigned.append(raw)
                continue
            text = ""
            if isinstance(raw, dict):
                text = _clean_line(str(raw.get("text") or ""))
            else:
                text = _clean_line(str(raw or ""))
            dropped_payload = raw.get("dropped_payload") if isinstance(raw, dict) else None
            payload_name = (
                _clean_line(str(dropped_payload.get("name") or ""))
                if isinstance(dropped_payload, dict)
                else ""
            )
            if payload_name:
                text = payload_name
            key = _product_name_key(text)
            if (
                not text
                or not key
                or key in seen
                or _is_noise_product_name(key)
                or any(key.startswith(prefix) for prefix in _NON_PRODUCT_TITLE_PREFIXES)
            ):
                remaining_unassigned.append(raw)
                continue

            base_verify = (
                dropped_payload.get("a_verifier") if isinstance(dropped_payload, dict) else []
            )
            candidate = {
                "name": text[:240],
                "quantity": (
                    dropped_payload.get("quantity") if isinstance(dropped_payload, dict) else None
                ),
                "description": (
                    _clean_line(str(dropped_payload.get("description") or ""))[:1200]
                    if isinstance(dropped_payload, dict)
                    else ""
                ),
                "characteristics": (
                    dropped_payload.get("characteristics") if isinstance(dropped_payload, dict) else []
                )
                or [],
                "accessoires_structures": (
                    dropped_payload.get("accessoires_structures")
                    if isinstance(dropped_payload, dict)
                    else []
                )
                or [],
                "exigences": (
                    dropped_payload.get("exigences") if isinstance(dropped_payload, dict) else []
                )
                or [],
                "a_verifier": _merge_object_rows(
                    base_verify or [],
                    [
                        {
                            "text": text[:240],
                            "reason": "Promu depuis unassigned pour respecter expected_product_count.",
                        }
                    ],
                ),
            }
            _ensure_quality_review_badge(candidate)
            products.append(candidate)
            seen.add(key)
            promoted += 1

        unassigned = remaining_unassigned

        while len(products) < expected:
            label = f"Produit manquant {len(products) + 1}"
            candidate = {
                "name": label,
                "quantity": None,
                "description": "",
                "characteristics": [],
                "accessoires_structures": [],
                "exigences": [],
                "a_verifier": [
                    {
                        "text": label,
                        "reason": "Aucun candidat fiable trouvé; ajout placeholder (expected_product_count strict).",
                    }
                ],
            }
            _ensure_quality_review_badge(candidate)
            products.append(candidate)
            placeholders += 1

    for idx, product in enumerate(products, start=1):
        product["product_id"] = f"p{idx}"

    payload["products"] = products
    payload["unassigned"] = unassigned
    payload["expected_product_count"] = expected
    payload["actual_product_count"] = len(products)
    payload["expected_count_enforced"] = bool(strict_expected_count)
    payload["expected_count_dropped"] = dropped
    payload["expected_count_merged"] = merged_dropped
    payload["expected_count_promoted"] = promoted
    payload["expected_count_placeholders"] = placeholders
    return payload


def _with_structured_products(
    payload: dict[str, Any],
    *,
    expected_product_count: int | None = None,
    strict_expected_count: bool = False,
) -> dict[str, Any]:
    items = payload.get("items")
    doc_type = str(payload.get("doc_type") or "")
    if not isinstance(items, list):
        payload["products"] = []
        payload["unassigned"] = []
        return payload
    products, unassigned = _build_structured_products_payload(items, doc_type)
    payload["products"] = products
    payload["unassigned"] = unassigned
    return _enforce_expected_product_count(
        payload,
        expected_product_count,
        strict_expected_count=strict_expected_count,
    )


def _find_best_product_match_index(
    products: list[dict[str, Any]],
    source_name: str,
) -> int | None:
    source_key = _product_name_key(source_name)
    if not source_key:
        return None

    best_idx: int | None = None
    best_score = -1.0
    source_tokens = _extract_match_tokens(source_name)
    source_families = _detect_row_families(_norm(source_name))
    for idx, product in enumerate(products):
        target_name = _clean_line(str(product.get("name") or ""))
        target_key = _product_name_key(target_name)
        if not target_key:
            continue
        if target_key == source_key:
            return idx

        ratio = difflib.SequenceMatcher(None, source_key, target_key).ratio()
        target_tokens = _extract_match_tokens(target_name)
        token_union = len(source_tokens | target_tokens)
        token_inter = len(source_tokens & target_tokens)
        token_score = (token_inter / token_union) if token_union else 0.0
        target_families = _detect_row_families(_norm(target_name))
        family_bonus = 0.10 if (source_families and target_families and (source_families & target_families)) else 0.0
        prefix_bonus = 0.07 if (
            source_key.startswith(target_key)
            or target_key.startswith(source_key)
        ) else 0.0
        score = (0.74 * ratio) + (0.19 * token_score) + family_bonus + prefix_bonus

        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx is not None and best_score >= 0.79:
        return best_idx
    return None


def _is_structured_products_payload_weak(payload: dict[str, Any]) -> bool:
    products = payload.get("products")
    if not isinstance(products, list) or not products:
        return True

    strong_count = 0
    for product in products:
        if not isinstance(product, dict):
            continue
        quantity = _normalize_quantity_value(product.get("quantity"))
        description = _clean_line(str(product.get("description") or ""))
        payload_size = (
            len(product.get("characteristics") or [])
            + len(product.get("accessoires_structures") or [])
            + len(product.get("exigences") or [])
        )
        if quantity is not None or payload_size > 0 or len(description) >= 24:
            strong_count += 1
    return strong_count == 0


def _llm_payload_product_count(payload: dict[str, Any] | None) -> int:
    if not isinstance(payload, dict):
        return 0
    rows = payload.get("products")
    if not isinstance(rows, list):
        return 0
    return len([row for row in rows if isinstance(row, dict)])


def _llm_payload_quality_score(payload: dict[str, Any] | None, *, target_count: int | None = None) -> float:
    if not isinstance(payload, dict):
        return -999.0
    products = payload.get("products")
    if not isinstance(products, list):
        return -999.0
    prod_rows = [row for row in products if isinstance(row, dict)]
    if not prod_rows:
        return -999.0

    score = 0.0
    count = len(prod_rows)
    if target_count and target_count > 0:
        diff = abs(count - int(target_count))
        score += max(0.0, 2.6 - (0.85 * diff))
    else:
        score += min(1.0, count / 10.0)

    for row in prod_rows:
        payload_size = (
            len(row.get("characteristics") or [])
            + len(row.get("accessoires_structures") or [])
            + len(row.get("exigences") or [])
        )
        if payload_size > 0:
            score += 0.07
        if _normalize_quantity_value(row.get("quantity")) is not None:
            score += 0.03
        if len(_clean_line(str(row.get("description") or "")).split()) >= 4:
            score += 0.02
        name = _clean_line(str(row.get("name") or ""))
        if name and not _is_noise_product_name(_product_name_key(name)):
            score += 0.02

    unassigned = payload.get("unassigned")
    if isinstance(unassigned, list):
        score -= min(1.0, len(unassigned) / 50.0)
    return score


def _select_best_llm_payload(
    primary: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    *,
    target_count: int | None = None,
) -> dict[str, Any] | None:
    if not isinstance(primary, dict):
        return candidate
    if not isinstance(candidate, dict):
        return primary
    primary_score = _llm_payload_quality_score(primary, target_count=target_count)
    candidate_score = _llm_payload_quality_score(candidate, target_count=target_count)
    return candidate if candidate_score > primary_score else primary


def _should_retry_llm_with_strong_model(
    llm_payload: dict[str, Any] | None,
    *,
    target_count: int | None = None,
) -> bool:
    if not isinstance(llm_payload, dict):
        return True
    count = _llm_payload_product_count(llm_payload)
    if count <= 0:
        return True
    if target_count and target_count > 0:
        if abs(count - int(target_count)) >= 2:
            return True
    if _llm_payload_quality_score(llm_payload, target_count=target_count) < 1.15:
        return True
    return False


def _promote_unmatched_llm_products(
    heuristic_products: list[dict[str, Any]],
    unmatched_llm_products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    promoted: list[dict[str, Any]] = []
    seen_keys = {
        _product_name_key(str(row.get("name") or ""))
        for row in heuristic_products
        if isinstance(row, dict)
    }
    for raw in unmatched_llm_products:
        if not isinstance(raw, dict):
            continue
        name = _clean_line(str(raw.get("name") or ""))
        if not name:
            continue
        key = _product_name_key(name)
        if not key or _is_noise_product_name(key) or key in seen_keys:
            continue

        quantity = _normalize_quantity_value(raw.get("quantity"))
        description = _clean_line(str(raw.get("description") or ""))
        characteristics = raw.get("characteristics") if isinstance(raw.get("characteristics"), list) else []
        accessoires = (
            raw.get("accessoires_structures")
            if isinstance(raw.get("accessoires_structures"), list)
            else []
        )
        exigences = raw.get("exigences") if isinstance(raw.get("exigences"), list) else []
        to_verify = _merge_object_rows(
            raw.get("a_verifier") or [],
            [
                {
                    "text": name,
                    "reason": "Produit LLM promu (ancrage heuristique non trouvé).",
                }
            ],
        )

        payload_size = len(characteristics) + len(accessoires) + len(exigences)
        # Skip pure section headings with no usable payload.
        if _is_product_heading(name) and quantity is None and payload_size == 0 and len(description) < 24:
            continue

        product = {
            "name": name,
            "quantity": quantity,
            "description": description,
            "characteristics": characteristics,
            "accessoires_structures": accessoires,
            "exigences": exigences,
            "a_verifier": to_verify,
            "llm_confidence": raw.get("llm_confidence"),
            "needs_review": True,
        }
        _ensure_quality_review_badge(product)
        promoted.append(product)
        seen_keys.add(key)
    return promoted


def _merge_hybrid_attachment_payloads(
    heuristic_payload: dict[str, Any],
    llm_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(heuristic_payload, dict):
        return heuristic_payload
    if not isinstance(llm_payload, dict):
        return heuristic_payload

    heuristic_products = heuristic_payload.get("products")
    llm_products = llm_payload.get("products")
    if not isinstance(heuristic_products, list):
        heuristic_products = []
    if not isinstance(llm_products, list):
        llm_products = []

    if not heuristic_products and llm_products:
        fallback = dict(llm_payload)
        fallback["engine"] = f"{heuristic_payload.get('engine', 'heuristic')}+llm_fallback"
        return fallback

    if _is_structured_products_payload_weak(heuristic_payload) and llm_products:
        fallback = dict(llm_payload)
        fallback["engine"] = f"{heuristic_payload.get('engine', 'heuristic')}+llm_fallback"
        return fallback

    if not llm_products:
        return heuristic_payload

    unassigned_base = heuristic_payload.get("unassigned")
    unassigned: list[dict[str, Any]] = (
        [row for row in unassigned_base if isinstance(row, dict)]
        if isinstance(unassigned_base, list)
        else []
    )
    unmatched_llm_products: list[dict[str, Any]] = []

    for llm_product in llm_products:
        if not isinstance(llm_product, dict):
            continue
        llm_name = _clean_line(str(llm_product.get("name") or ""))
        if not llm_name:
            continue
        target_idx = _find_best_product_match_index(heuristic_products, llm_name)
        if target_idx is None:
            unmatched_llm_products.append(llm_product)
            unassigned = _merge_object_rows(
                unassigned,
                [
                    {
                        "text": llm_name,
                        "reason": "Produit détecté par LLM non rattaché aux ancres heuristiques.",
                    }
                ],
            )
            continue

        target = heuristic_products[target_idx]
        if target.get("quantity") is None and llm_product.get("quantity") is not None:
            target["quantity"] = _normalize_quantity_value(llm_product.get("quantity"))

        llm_desc = _clean_line(str(llm_product.get("description") or ""))
        if llm_desc:
            existing_desc = _clean_line(str(target.get("description") or ""))
            if not existing_desc:
                target["description"] = llm_desc
            elif _norm(llm_desc) not in _norm(existing_desc):
                target["description"] = f"{existing_desc} || {llm_desc}"[:1200]

        target["characteristics"] = _merge_characteristics_output(
            target.get("characteristics") or [],
            llm_product.get("characteristics") or [],
        )
        target["accessoires_structures"] = _merge_object_rows(
            target.get("accessoires_structures") or [],
            llm_product.get("accessoires_structures") or [],
        )
        target["exigences"] = _merge_object_rows(
            target.get("exigences") or [],
            llm_product.get("exigences") or [],
        )
        target["a_verifier"] = _merge_object_rows(
            target.get("a_verifier") or [],
            llm_product.get("a_verifier") or [],
        )
        llm_conf = llm_product.get("llm_confidence")
        if llm_conf is not None:
            target["llm_confidence"] = llm_conf
        _ensure_quality_review_badge(target)

    # Auto-fallback: if many LLM products could not be attached to heuristic anchors,
    # keep them as reviewable products instead of losing them in unassigned.
    if unmatched_llm_products:
        unmatched_ratio = len(unmatched_llm_products) / max(1, len(llm_products))
        large_unassigned_pressure = len(unassigned) >= max(25, len(heuristic_products) * 2)
        many_unmatched_llm = len(unmatched_llm_products) >= 20
        overwhelming_unmatched = (
            unmatched_ratio >= 0.60
            and len(unmatched_llm_products) >= 10
            and len(heuristic_products) <= 30
        )
        should_promote = (many_unmatched_llm and large_unassigned_pressure) or overwhelming_unmatched
        if should_promote:
            promoted = _promote_unmatched_llm_products(heuristic_products, unmatched_llm_products)
            if promoted:
                heuristic_products.extend(promoted)
                promoted_keys = {
                    _product_name_key(str(row.get("name") or ""))
                    for row in promoted
                    if isinstance(row, dict)
                }
                if promoted_keys:
                    unassigned = [
                        row
                        for row in unassigned
                        if _product_name_key(str(row.get("text") or "")) not in promoted_keys
                    ]

    heuristic_payload["products"] = heuristic_products
    heuristic_payload["unassigned"] = unassigned
    heuristic_payload["items_count"] = len(heuristic_products)
    heuristic_payload["engine"] = f"{heuristic_payload.get('engine', 'heuristic')}+llm_enriched"
    return heuristic_payload


def _has_non_empty_characteristics(row: dict[str, Any]) -> bool:
    for key in ("requested_characteristics", "regex_characteristics", "embedding_characteristics"):
        value = row.get(key)
        if isinstance(value, dict) and value:
            return True
    for key in ("accessoires_structures", "exigences", "a_verifier"):
        if _to_text_list(row.get(key)):
            return True
    return False


def _is_noise_product_name(name_norm: str) -> bool:
    if not name_norm:
        return True
    bad = {
        "n",
        "no",
        "n°",
        "numero",
        "qte",
        "qtte",
        "designation d'equipement",
        "designation dequipement",
        "jo designation d'equipement",
        "jo designation dequipement",
    }
    return name_norm in bad


def _best_anchor_index_by_name_semantics(
    detail_row: dict[str, Any],
    anchors: list[dict[str, Any]],
) -> int | None:
    if not anchors:
        return None

    detail_name = _clean_line(str(detail_row.get("product_name") or ""))
    detail_text = _row_match_text(detail_row)
    detail_norm = _norm(detail_text)
    detail_name_key = _product_name_key(detail_name)
    detail_tokens = _extract_match_tokens(detail_text)
    detail_families = _detect_row_families(detail_norm)

    if not detail_name_key and not detail_tokens and not detail_families:
        return None

    best_idx: int | None = None
    best_score = -1.0
    best_ratio = 0.0

    for idx, anchor in enumerate(anchors):
        anchor_name = _clean_line(str(anchor.get("product_name") or ""))
        anchor_name_key = _product_name_key(anchor_name)
        if not anchor_name_key:
            continue

        anchor_text = _row_match_text(anchor)
        anchor_tokens = _extract_match_tokens(anchor_text)
        anchor_families = _detect_row_families(_norm(anchor_text))

        ratio = difflib.SequenceMatcher(None, detail_name_key, anchor_name_key).ratio()
        inter = len(detail_tokens & anchor_tokens)
        union = len(detail_tokens | anchor_tokens)
        token_sim = (inter / union) if union else 0.0
        family_bonus = 0.18 if (detail_families and anchor_families and (detail_families & anchor_families)) else 0.0
        prefix_bonus = 0.08 if (
            detail_name_key.startswith(anchor_name_key)
            or anchor_name_key.startswith(detail_name_key)
        ) else 0.0

        score = (0.62 * ratio) + (0.22 * token_sim) + family_bonus + prefix_bonus
        if score > best_score:
            best_score = score
            best_ratio = ratio
            best_idx = idx

    if best_idx is None:
        return None
    if best_ratio >= 0.82:
        return best_idx
    if best_score >= 0.58:
        return best_idx
    return None


def _merge_mixed_preview_items(
    product_list_items: list[dict[str, Any]],
    tech_items: list[dict[str, Any]],
    *,
    anchor_lock_to_list: bool = False,
) -> list[dict[str, Any]]:
    if anchor_lock_to_list and product_list_items:
        anchors = [dict(row) for row in product_list_items if isinstance(row, dict)]
        anchors = _attach_textual_characteristics_to_products(anchors)
        anchors = _consolidate_product_list_rows(anchors)
        if not anchors:
            anchors = [dict(row) for row in product_list_items if isinstance(row, dict)]

        last_target_idx: int | None = None
        for raw in tech_items:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            target_idx = _best_anchor_index_by_name_semantics(row, anchors)
            if target_idx is None and _is_detail_candidate_row(row) and last_target_idx is not None:
                target_idx = last_target_idx
                row["a_verifier"] = _merge_text_lists(
                    row.get("a_verifier"),
                    ["Rattachement faible: vérifier le produit cible."],
                    limit=180,
                )
            if target_idx is None:
                continue
            _merge_detail_into_anchor(anchors[target_idx], row)
            last_target_idx = target_idx

        return _consolidate_product_list_rows(anchors)

    merged: list[dict[str, Any]] = []
    index_by_name: dict[str, int] = {}

    for row in product_list_items + tech_items:
        name_norm = _product_name_key(str(row.get("product_name") or ""))
        if _is_noise_product_name(name_norm):
            continue
        if not name_norm:
            merged.append(row)
            continue

        idx = index_by_name.get(name_norm)
        if idx is None:
            index_by_name[name_norm] = len(merged)
            merged.append(row)
            continue

        existing = merged[idx]
        existing_chars = _has_non_empty_characteristics(existing)
        incoming_chars = _has_non_empty_characteristics(row)

        if incoming_chars and not existing_chars:
            # Keep richer technical details when they match same product name.
            if existing.get("quantity") is not None and row.get("quantity") is None:
                row["quantity"] = existing.get("quantity")
            if not row.get("raw_text") and existing.get("raw_text"):
                row["raw_text"] = existing.get("raw_text")
            if not row.get("raw_product_name") and existing.get("raw_product_name"):
                row["raw_product_name"] = existing.get("raw_product_name")
            if not row.get("description") and existing.get("description"):
                row["description"] = existing.get("description")
            row["accessoires_structures"] = _merge_text_lists(
                existing.get("accessoires_structures"),
                row.get("accessoires_structures"),
                limit=180,
            )
            row["exigences"] = _merge_text_lists(
                existing.get("exigences"),
                row.get("exigences"),
                limit=180,
            )
            row["a_verifier"] = _merge_text_lists(
                existing.get("a_verifier"),
                row.get("a_verifier"),
                limit=180,
            )
            existing_amb = existing.get("ambiguous_fragments") or []
            row_amb = row.get("ambiguous_fragments") or []
            if isinstance(existing_amb, list) and isinstance(row_amb, list):
                row["ambiguous_fragments"] = (row_amb + [x for x in existing_amb if x not in row_amb])[:4]
            if existing.get("needs_review") and not row.get("needs_review"):
                row["needs_review"] = True
            if row.get("a_verifier") and not row.get("needs_review"):
                row["needs_review"] = True
            if not row.get("description_confidence") and existing.get("description_confidence"):
                row["description_confidence"] = existing.get("description_confidence")
            if not row.get("name_confidence") and existing.get("name_confidence"):
                row["name_confidence"] = existing.get("name_confidence")
            merged[idx] = row
            continue

        if existing.get("quantity") is None and row.get("quantity") is not None:
            existing["quantity"] = row.get("quantity")
        if existing.get("unit_price") is None and row.get("unit_price") is not None:
            existing["unit_price"] = row.get("unit_price")
        if existing.get("line_total") is None and row.get("line_total") is not None:
            existing["line_total"] = row.get("line_total")
        if (not existing.get("description")) and row.get("description"):
            existing["description"] = row.get("description")
        existing["accessoires_structures"] = _merge_text_lists(
            existing.get("accessoires_structures"),
            row.get("accessoires_structures"),
            limit=180,
        )
        existing["exigences"] = _merge_text_lists(
            existing.get("exigences"),
            row.get("exigences"),
            limit=180,
        )
        existing["a_verifier"] = _merge_text_lists(
            existing.get("a_verifier"),
            row.get("a_verifier"),
            limit=180,
        )
        if not existing.get("raw_text") and row.get("raw_text"):
            existing["raw_text"] = row.get("raw_text")
        if not existing.get("raw_product_name") and row.get("raw_product_name"):
            existing["raw_product_name"] = row.get("raw_product_name")
        existing_amb = existing.get("ambiguous_fragments") or []
        row_amb = row.get("ambiguous_fragments") or []
        if isinstance(existing_amb, list) and isinstance(row_amb, list):
            existing["ambiguous_fragments"] = (existing_amb + [x for x in row_amb if x not in existing_amb])[:4]
        if row.get("needs_review") or existing.get("a_verifier"):
            existing["needs_review"] = True
        existing_desc_conf = float(existing.get("description_confidence") or 0.0)
        row_desc_conf = float(row.get("description_confidence") or 0.0)
        if row_desc_conf and (existing_desc_conf == 0.0 or row_desc_conf < existing_desc_conf):
            existing["description_confidence"] = round(row_desc_conf, 2)
        existing_name_conf = float(existing.get("name_confidence") or 0.0)
        row_name_conf = float(row.get("name_confidence") or 0.0)
        if row_name_conf > existing_name_conf:
            existing["name_confidence"] = round(row_name_conf, 2)

    return merged[:200]


def _weak_preview_items(items: list[dict[str, Any]]) -> bool:
    if not items:
        return True
    meaningful = 0
    for row in items:
        name = _product_name_key(str(row.get("product_name") or ""))
        if not name:
            continue
        if _is_noise_product_name(name):
            continue
        if len(name) < 4:
            continue
        meaningful += 1
    return meaningful == 0


def _extract_embedding_characteristics(
    block_lines: list[str],
    parsed: dict[str, Any],
    threshold: float,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    parsed_pairs = parsed.get("pairs", []) if isinstance(parsed.get("pairs", []), list) else []
    used_lines = {
        str(p.get("raw_line", "")).strip()
        for p in parsed_pairs
        if str(p.get("raw_line", "")).strip()
        and str(p.get("raw_line", "")).strip() not in {"COMPOSED", "EMBED"}
    }
    # Keep regex-only keys separate from embedding-enriched keys.
    regex_keys = {
        _canonical_key(str(p.get("key", ""))).strip().lower()
        for p in parsed_pairs
        if str(p.get("raw_line", "")).strip() not in {"COMPOSED", "EMBED"}
        and str(p.get("key", "")).strip()
    }

    candidates: list[str] = []
    for line in block_lines[1:]:
        ln = _clean_line(line)
        if not ln or ln in used_lines:
            continue
        if len(ln) > 220:
            continue
        # Keep technical lines only to reduce noise and runtime.
        ln_norm = _norm(ln)
        has_numeric = any(ch.isdigit() for ch in ln)
        has_text_signal = any(token in ln_norm for token in _TEXTUAL_SIGNAL_KEYWORDS)
        if has_numeric or has_text_signal:
            candidates.append(ln)
    # Deduplicate while preserving order, then cap volume per block.
    seen: set[str] = set()
    uniq: list[str] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        uniq.append(c)
    candidates = uniq[:60]

    if not candidates:
        return {}, {}

    queries = list(_EMBED_KEY_PROMPTS.values())
    try:
        emb_map = _embed_texts(queries + candidates)
    except Exception:
        return {}, {}

    extracted: dict[str, list[dict[str, Any]]] = {}
    ignored: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        c_emb = emb_map.get(cand)
        if c_emb is None:
            continue
        best_key = None
        best_score = -1.0
        for key, prompt in _EMBED_KEY_PROMPTS.items():
            q_emb = emb_map.get(prompt)
            if q_emb is None:
                continue
            from app.services.openai_embeddings import cosine_similarity

            score = cosine_similarity(c_emb, q_emb)
            if score > best_score:
                best_score = score
                best_key = key
        if not best_key or best_score < threshold:
            continue
        entry = {"value": cand, "score": round(float(best_score), 4)}
        key_norm = _canonical_key(best_key).strip().lower()
        if key_norm in regex_keys:
            ignored.setdefault(key_norm, []).append(entry)
        else:
            extracted.setdefault(key_norm, []).append(entry)

    # Keep top-2 lines per key to stay readable.
    for bucket in (extracted, ignored):
        for key, values in list(bucket.items()):
            bucket[key] = sorted(values, key=lambda x: x["score"], reverse=True)[:2]

    return extracted, ignored


def _llm_characteristics_to_dict(lines: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for raw in _to_text_list(lines):
        line = _clean_line(raw)
        if not line:
            continue
        if ":" in line:
            left, right = line.split(":", 1)
            key = _normalize_characteristic_key(left)
            value = _clean_line(right)
            if key and value:
                out[key] = _merge_text_lists(out.get(key), [value], limit=12)
                continue
        out["details"] = _merge_text_lists(out.get("details"), [line], limit=24)
    return out


def _extract_products_with_openai_schema(
    raw_text: str,
    *,
    doc_type_hint: str = "auto",
    layout_hint: str = "auto",
    model_override: str | None = None,
) -> dict[str, Any] | None:
    if not config.OPENAI_API_KEY:
        return None
    clean = _clean_line(raw_text)
    if not clean:
        return None

    max_chars = max(8000, int(getattr(config, "OPENAI_ATTACHMENT_MAX_INPUT_CHARS", 90000) or 90000))
    text_for_model = clean[:max_chars]
    model = str(
        (
            model_override
            or getattr(config, "OPENAI_ATTACHMENT_MODEL", "gpt-4.1-mini")
            or "gpt-4.1-mini"
        )
    )
    timeout_s = max(30, int(getattr(config, "OPENAI_ATTACHMENT_TIMEOUT_SECONDS", 120) or 120))
    strict_produit = doc_type_hint == "strict_produit"

    system_prompt = (
        "Tu extrais des produits depuis des cahiers des charges OCR, sans inventer. "
        "Retourne strictement le JSON schema demandé. "
        "Règles strictes: "
        "1) Chaque ligne produit identifiable doit aller dans products, même sans caractéristiques. "
        "2) Pour les listes/lot/tableaux, ne garde pas les lignes en unassigned sauf bruit illisible. "
        "3) Si la quantité n'est pas lisible, mets quantity=null. "
        "3bis) Si la quantité est donnée en min/max ou en intervalle, prends le minimum. "
        "4) Mets dans to_verify uniquement les points ambigus, pas les lignes normales. "
        "5) Ne fusionne pas deux produits différents."
    )
    forced_doc_type_hint = _effective_doc_type_hint(doc_type_hint)
    if doc_type_hint not in {"", "auto"}:
        system_prompt += f" 6) Profil document demande: {doc_type_hint}."
    if forced_doc_type_hint in {"product_list", "requirement_sheet", "tech_sheet"}:
        system_prompt += (
            f" 7) Type de document force: {forced_doc_type_hint} (respecte ce type)."
        )
    if layout_hint in {"table", "paragraph", "mixed"}:
        system_prompt += (
            f" 8) Mise en page dominante: {layout_hint} (adapte l'extraction a cette structure)."
        )
    if strict_produit:
        system_prompt += (
            " 9) Mode strict_produit: ne garde QUE les lignes produit explicites "
            "(nom produit clair). Ignore les phrases generales, titres, ou descriptions "
            "sans nom produit net. N'infere pas de produit a partir de contexte."
        )
    lexicon_hints = _build_llm_medical_lexicon_hints(text_for_model)
    lexicon_section = f"\n\n{lexicon_hints}" if lexicon_hints else ""
    user_prompt = (
        "Extrait tous les produits demandés, leur quantité si présente, "
        "leurs caractéristiques, accessoires, exigences, et points à vérifier.\n\n"
        f"Document OCR:\n{text_for_model}"
        f"{lexicon_section}"
    )
    if strict_produit:
        user_prompt = (
            "Mode strict_produit: extrais uniquement les lignes produit explicites. "
            "Ne retourne que: name, quantity, unit, packaging, dimensions, size, weight, vat_rate, confidence. "
            "Si un produit n'est pas clairement identifié, ne le retourne pas.\n\n"
            f"Document OCR:\n{text_for_model}"
            f"{lexicon_section}"
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
                "name": "attachment_products",
                "strict": True,
                "schema": _LLM_ATTACHMENT_STRICT_SCHEMA
                if strict_produit
                else _LLM_ATTACHMENT_SCHEMA,
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
            operation="attachment_llm_structured",
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
        operation="attachment_llm_structured",
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
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text)
            content = "\n".join(chunks)
        if not isinstance(content, str) or not content.strip():
            return None
        parsed = json.loads(content)
    except Exception:
        return None

    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("products"), list):
        return None
    if strict_produit:
        strict_payload = _sanitize_strict_products_payload(parsed)
        parsed = _convert_strict_products_payload(strict_payload)
        parsed["strict_payload"] = strict_payload
    if forced_doc_type_hint in {"product_list", "requirement_sheet", "tech_sheet"}:
        parsed["doc_type"] = forced_doc_type_hint
    return parsed


def _build_payload_from_llm_products(
    parsed: dict[str, Any],
    *,
    file_path: str,
    engine: str,
) -> dict[str, Any]:
    doc_type = str(parsed.get("doc_type") or "unknown").strip().lower()
    if doc_type not in {"product_list", "requirement_sheet", "tech_sheet"}:
        doc_type = "tech_sheet"

    products: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    fail_closed = bool(getattr(config, "OPENAI_ATTACHMENT_FAIL_CLOSED", True))
    verify_conf = float(getattr(config, "OPENAI_ATTACHMENT_VERIFY_CONFIDENCE", 0.70) or 0.70)

    for raw in parsed.get("products") or []:
        if not isinstance(raw, dict):
            continue
        name = _clean_line(str(raw.get("name") or ""))
        if not name:
            continue
        lexicon_warning: str | None = None
        lexicon_match = _match_medical_lexicon_name(name)
        if isinstance(lexicon_match, dict):
            lexicon_canonical = _clean_line(str(lexicon_match.get("canonical") or ""))
            lexicon_score = float(lexicon_match.get("score") or 0.0)
            if lexicon_canonical and bool(lexicon_match.get("auto_correct")):
                canonical_key = _product_name_key(lexicon_canonical)
                if canonical_key and not _is_noise_product_name(canonical_key):
                    name = lexicon_canonical
            elif lexicon_canonical:
                lexicon_warning = (
                    f"Match lexique moyen ({lexicon_score:.2f}) -> candidat: {lexicon_canonical}"
                )

        name_norm = _product_name_key(name)
        if _is_noise_product_name(name_norm) or bool(re.fullmatch(r"\d{1,4}", name)):
            continue

        quantity = _normalize_quantity_value(raw.get("quantity"))
        description = _clean_line(str(raw.get("description") or ""))
        char_dict = _llm_characteristics_to_dict(raw.get("characteristics"))
        characteristics = _normalize_characteristics_payload(char_dict, char_dict)
        accessoires = _normalize_accessoires_payload(raw.get("accessories"), raw.get("requirements"))
        exigences = _normalize_exigences_payload(raw.get("requirements"))
        to_verify = _normalize_verify_payload(raw.get("to_verify"))
        if lexicon_warning:
            to_verify = _merge_object_rows(to_verify, [{"text": name, "reason": lexicon_warning}])

        confidence = 0.0
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        if 0.0 < confidence < verify_conf:
            to_verify = _merge_object_rows(
                to_verify,
                [{"text": name, "reason": f"Confiance IA faible ({confidence:.2f} < {verify_conf:.2f})"}],
            )

        product = {
            "name": name,
            "quantity": quantity,
            "description": description,
            "characteristics": characteristics,
            "accessoires_structures": accessoires,
            "exigences": exigences,
            "a_verifier": to_verify,
            "llm_confidence": round(confidence, 2),
        }
        _ensure_quality_review_badge(product)
        if _is_very_weak_product_candidate(product):
            if fail_closed:
                unassigned.append(
                    {
                        "text": name,
                        "reason": "Produit trop incertain (IA faible, sans données exploitables).",
                    }
                )
                continue
            product["a_verifier"] = _merge_object_rows(
                product.get("a_verifier") or [],
                [
                    {
                        "text": name,
                        "reason": "Confiance faible: conservé en résultat (mode fail_open).",
                    }
                ],
            )
            product["needs_review"] = True
        products.append(product)

    llm_unassigned = _to_text_list(parsed.get("unassigned"))
    for text in llm_unassigned:
        if fail_closed:
            unassigned.append({"text": text, "reason": "Non rattaché par extraction IA"})
            continue
        name = _clean_line(text)
        if not name:
            continue
        name_norm = _product_name_key(name)
        if _is_noise_product_name(name_norm) or bool(re.fullmatch(r"\d{1,4}", name)):
            continue
        product = {
            "name": name,
            "quantity": None,
            "description": "",
            "characteristics": [],
            "accessoires_structures": [],
            "exigences": [],
            "a_verifier": [
                {
                    "text": name,
                    "reason": "Non rattaché par extraction IA (mode fail_open).",
                }
            ],
            "llm_confidence": 0.0,
            "needs_review": True,
        }
        _ensure_quality_review_badge(product)
        products.append(product)

    # If model returned only unassigned rows, expose them as low-confidence products
    # so the user still sees exploitable candidates in the UI.
    if not products and llm_unassigned:
        unassigned = []
        for raw_name in llm_unassigned:
            name = _clean_line(raw_name)
            if not name:
                continue
            name_norm = _product_name_key(name)
            if _is_noise_product_name(name_norm) or bool(re.fullmatch(r"\d{1,4}", name)):
                continue
            fallback_product = {
                "name": name,
                "quantity": None,
                "description": "",
                "characteristics": [],
                "accessoires_structures": [],
                "exigences": [],
                "a_verifier": [
                    {
                        "text": name,
                        "reason": "Produit extrait avec confiance insuffisante (IA).",
                    }
                ],
                "llm_confidence": 0.0,
            }
            _ensure_quality_review_badge(fallback_product)
            products.append(fallback_product)

    for idx, product in enumerate(products, start=1):
        product["product_id"] = f"p{idx}"

    return {
        "engine": f"{engine}+openai_structured",
        "file_name": Path(file_path).name,
        "doc_type": doc_type,
        "embedding_threshold": None,
        "items_count": len(products),
        "avg_regex_score": 0.0,
        "items": [],
        "products": products,
        "unassigned": unassigned,
    }


def extract_attachment_products_preview(
    file_path: str,
    embedding_threshold: float | None = None,
    expected_product_count: int | None = None,
    strict_expected_count: bool = False,
    document_type_hint: str | None = None,
    layout_hint: str | None = None,
    strict_count_mode: str | None = None,
    require_specs_for_acceptance: bool = False,
) -> dict[str, Any]:
    engine = "document_ai_ocr"
    raw_text = ""
    doc_type_hint_norm = _normalize_doc_type_hint(document_type_hint)
    layout_hint_norm = _normalize_layout_hint(layout_hint)
    forced_doc_type_hint = _effective_doc_type_hint(doc_type_hint_norm)
    hint_behavior = _hint_behavior_profile(doc_type_hint_norm)
    if layout_hint_norm == "auto":
        layout_hint_norm = _effective_layout_from_doc_type_hint(doc_type_hint_norm) or "auto"
    strict_count_mode_norm = _normalize_strict_count_mode(strict_count_mode)
    effective_expected_count = expected_product_count
    effective_strict_expected_count = bool(strict_expected_count)
    if strict_count_mode_norm == "strict":
        effective_strict_expected_count = True
    elif strict_count_mode_norm == "soft":
        effective_strict_expected_count = False
    elif strict_count_mode_norm == "off":
        effective_expected_count = None
        effective_strict_expected_count = False

    def _apply_runtime_rules(payload: dict[str, Any]) -> dict[str, Any]:
        if require_specs_for_acceptance:
            return _apply_require_specs_for_acceptance(payload)
        return payload

    # Cost guard: try free local PDF text extraction first, then fallback to OCR only if weak.
    local_text = _extract_pdf_text_local(file_path) or ""
    if local_text:
        local_ok_chars = len(local_text.strip()) >= config.TECH_SHEET_LOCAL_MIN_CHARS
        local_ok_quality = _raw_text_quality(local_text) >= config.TECH_SHEET_RAW_MIN_SCORE
        if local_ok_chars and local_ok_quality:
            raw_text = local_text
            engine = "local_text"

    if not raw_text:
        ocr_provider = str(getattr(config, "OPENAI_OCR_PROVIDER", "document_ai") or "document_ai").strip().lower()
        if ocr_provider in {"gpt4_vision", "openai_vision", "vision"}:
            try:
                from app.services.openai_vision_ocr import extract_text_from_file

                raw_text = extract_text_from_file(
                    file_path=file_path,
                    operation="attachment_ocr_openai_vision",
                    max_pages=getattr(config, "OPENAI_OCR_VISION_MAX_PAGES", 8),
                )
                if raw_text:
                    engine = "openai_vision_ocr"
            except Exception:
                raw_text = ""

    if not raw_text:
        raw_text = extract_full_text(file_path)
        engine = "document_ai_ocr"

    extraction_mode = str(getattr(config, "OPENAI_ATTACHMENT_EXTRACTION_MODE", "heuristic") or "heuristic").lower()
    if extraction_mode == "smart_hybrid":
        extraction_mode = "smart"
    llm_payload: dict[str, Any] | None = None

    def _maybe_retry_llm_payload(
        current_payload: dict[str, Any] | None,
        *,
        target_count: int | None = None,
    ) -> dict[str, Any] | None:
        if extraction_mode not in {"llm", "llm_structured", "ai", "hybrid", "smart"}:
            return current_payload
        if not bool(getattr(config, "OPENAI_ATTACHMENT_SMART_RETRY", True)):
            return current_payload
        if not isinstance(current_payload, dict):
            return current_payload

        strong_model = _clean_line(str(getattr(config, "OPENAI_ATTACHMENT_STRONG_MODEL", "") or ""))
        base_model = _clean_line(str(getattr(config, "OPENAI_ATTACHMENT_MODEL", "") or ""))
        if not strong_model or strong_model == base_model:
            return current_payload
        if not _should_retry_llm_with_strong_model(current_payload, target_count=target_count):
            return current_payload

        parsed_retry = _extract_products_with_openai_schema(
            raw_text,
            doc_type_hint=doc_type_hint_norm,
            layout_hint=layout_hint_norm,
            model_override=strong_model,
        )
        if not parsed_retry:
            return current_payload

        llm_retry_payload = _build_payload_from_llm_products(
            parsed_retry,
            file_path=file_path,
            engine=engine,
        )
        return _select_best_llm_payload(
            current_payload,
            llm_retry_payload,
            target_count=target_count,
        )

    if extraction_mode in {"llm", "llm_structured", "ai", "hybrid", "smart"}:
        parsed = _extract_products_with_openai_schema(
            raw_text,
            doc_type_hint=doc_type_hint_norm,
            layout_hint=layout_hint_norm,
        )
        if parsed:
            llm_payload = _build_payload_from_llm_products(
                parsed,
                file_path=file_path,
                engine=engine,
            )
            if isinstance(parsed, dict) and "strict_payload" in parsed:
                llm_payload["strict_payload"] = parsed.get("strict_payload")
            if extraction_mode in {"llm", "llm_structured", "ai"}:
                llm_target_count = (
                    int(effective_expected_count)
                    if isinstance(effective_expected_count, int) and effective_expected_count > 0
                    else None
                )
                llm_payload = _maybe_retry_llm_payload(
                    llm_payload,
                    target_count=llm_target_count,
                )
                return _apply_runtime_rules(
                    _enforce_expected_product_count(
                        llm_payload,
                        effective_expected_count,
                        strict_expected_count=effective_strict_expected_count,
                    )
                )

    def _finalize_preview_payload(base_payload: dict[str, Any]) -> dict[str, Any]:
        if extraction_mode in {"hybrid", "smart"}:
            structured = _with_structured_products(
                base_payload,
                expected_product_count=None,
                strict_expected_count=False,
            )
            merged = _merge_hybrid_attachment_payloads(structured, llm_payload)
            return _apply_runtime_rules(
                _enforce_expected_product_count(
                    merged,
                    effective_expected_count,
                    strict_expected_count=effective_strict_expected_count,
                )
            )
        return _apply_runtime_rules(
            _with_structured_products(
                base_payload,
                expected_product_count=effective_expected_count,
                strict_expected_count=effective_strict_expected_count,
            )
        )

    raw_lines = [_clean_line(ln) for ln in raw_text.splitlines() if _clean_line(ln)]
    analysis_lines = list(raw_lines)
    if _looks_like_collapsed_ocr(raw_text, raw_lines):
        recovered = _recover_lines_from_collapsed_ocr(raw_text)
        if len(recovered) > len(raw_lines):
            analysis_lines = recovered

    list_lines = _explode_compound_product_lines(analysis_lines)
    strict_blocks = _split_product_blocks(analysis_lines)
    if not strict_blocks and analysis_lines:
        strict_blocks = [analysis_lines]

    # Preview mode: lower threshold for better recall; user can override.
    emb_threshold = (
        config.OPENAI_EXTRACTION_EMBEDDING_THRESHOLD
        if embedding_threshold is None
        else float(embedding_threshold)
    )
    emb_threshold = max(0.20, min(1.0, emb_threshold))

    looks_like_invoice = _looks_like_invoice_text(raw_text)
    looks_like_product_list = _looks_like_tabular_product_list(analysis_lines)
    if layout_hint_norm == "table":
        looks_like_product_list = True
    elif layout_hint_norm == "paragraph":
        looks_like_product_list = False
    if hint_behavior.get("force_invoice_like") is True:
        looks_like_invoice = True
    elif hint_behavior.get("force_invoice_like") is False:
        looks_like_invoice = False
    if hint_behavior.get("force_product_list_signal") is True:
        looks_like_product_list = True
    elif hint_behavior.get("force_product_list_signal") is False:
        looks_like_product_list = False

    doc_type = (
        "requirement_sheet"
        if _looks_like_requirement_sheet(raw_text, analysis_lines, strict_blocks)
        else "tech_sheet"
    )
    if forced_doc_type_hint in {"product_list", "requirement_sheet", "tech_sheet"}:
        doc_type = forced_doc_type_hint
        if forced_doc_type_hint == "product_list":
            looks_like_invoice = True
        else:
            looks_like_invoice = False

    # Use exploded lines for list extraction to recover OCR-collapsed indexed rows
    # like: "01 PRODUCT ... 02 PRODUCT ...".
    list_items_base = _merge_product_list_items(
        _extract_invoice_like_items(list_lines),
        _extract_product_title_items(list_lines),
    )

    strict_items: list[dict[str, Any]] = []
    for idx, block in enumerate(strict_blocks, start=1):
        block_title = _clean_line(_HEADING_PREFIX_RE.sub("", block[0])) or f"Produit {idx}"
        title_priority = _is_title_priority_block(block)
        block_text = "\n".join(block)
        parsed = parse_characteristics(block_text)
        regex_kv = parsed.get("kv", {}) or {}
        # Keep a pure regex score in preview, even if parse_characteristics
        # enriched kv with embedding candidates.
        regex_score = float(parsed.get("score_regex_only", parsed.get("score", 0.0)) or 0.0)
        description = _build_description(block, parsed)
        accessories, exigences, to_verify = _extract_structured_lists_from_block(
            block,
            block_title,
            aggressive_mode=bool(hint_behavior.get("aggressive_accessory_capture")),
        )
        emb_kv, emb_ignored = _extract_embedding_characteristics(block, parsed, emb_threshold)
        quantity_probe = _combine_requested_characteristics(regex_kv, emb_kv)
        quantity = _extract_requested_quantity(block, regex_kv, quantity_probe)
        regex_kv = _strip_quantity_characteristics(regex_kv)
        emb_kv = _strip_quantity_characteristics(emb_kv)
        requested_chars = _combine_requested_characteristics(regex_kv, emb_kv)
        obligations = _to_text_list(regex_kv.get("accessoires_obligatoires"))
        if obligations:
            exigences = _merge_text_lists(exigences, obligations, limit=180)
        if quantity is None and doc_type == "requirement_sheet" and not looks_like_product_list:
            quantity = 1.0

        # Skip clearly empty/noisy blocks.
        if not regex_kv and not emb_kv and not description:
            continue
        if (
            (doc_type == "requirement_sheet" or bool(hint_behavior.get("strict_requirement_filter")))
            and not _should_keep_requirement_item(block_title, regex_kv, emb_kv)
            and not title_priority
        ):
            continue

        base_desc_conf = 0.0
        if description:
            base_desc_conf = 0.80 if regex_kv else 0.46
        strict_items.append(
            _decorate_preview_item(
                {
                    "product_name": block_title,
                    "description": description,
                    "quantity": quantity,
                    "doc_type": doc_type,
                    "title_priority": title_priority,
                    "regex_score": round(regex_score, 4),
                    "regex_characteristics": regex_kv,
                    "embedding_characteristics": emb_kv,
                    "embedding_ignored_by_regex": emb_ignored,
                    "requested_characteristics": requested_chars,
                    "accessoires_structures": accessories,
                    "exigences": exigences,
                    "a_verifier": to_verify,
                },
                raw_text=block_text,
                raw_product_name=block[0] if block else block_title,
                description_confidence=base_desc_conf,
            )
        )

    strict_is_strong = bool(strict_items and not _weak_preview_items(strict_items))
    use_flexible_list_mode = (
        bool(hint_behavior.get("allow_flexible_mode"))
        and doc_type != "requirement_sheet"
        and not strict_is_strong
        and not looks_like_invoice
        and (looks_like_product_list or len(strict_blocks) <= 1)
    )

    list_items = list_items_base
    if use_flexible_list_mode:
        flexible_items = _extract_flexible_product_items(list_lines, emb_threshold)
        if flexible_items:
            list_items = _merge_product_list_items(list_items, flexible_items)
    if list_items:
        # A second pass helps after first merge enriched anchor descriptions/tokens.
        for _ in range(2):
            updated = _attach_textual_characteristics_to_products(list_items)
            if len(updated) == len(list_items):
                list_items = updated
                break
            list_items = updated
        list_items = _consolidate_product_list_rows(list_items)

    if extraction_mode in {"hybrid", "smart"} and isinstance(llm_payload, dict):
        target_count = len(list_items) if list_items else (
            int(effective_expected_count)
            if isinstance(effective_expected_count, int) and effective_expected_count > 0
            else None
        )
        llm_payload = _maybe_retry_llm_payload(
            llm_payload,
            target_count=target_count,
        )

    if looks_like_invoice and not looks_like_product_list:
        if list_items:
            return _finalize_preview_payload(
                {
                    "engine": engine,
                    "file_name": Path(file_path).name,
                    "doc_type": "product_list",
                    "embedding_threshold": None,
                    "items_count": len(list_items),
                    "avg_regex_score": 0.0,
                    "items": list_items,
                }
            )
        return _finalize_preview_payload(
            {
                "engine": engine,
                "file_name": Path(file_path).name,
                "doc_type": "product_list",
                "embedding_threshold": None,
                "items_count": 0,
                "avg_regex_score": 0.0,
                "items": [],
            }
        )

    avg_score = 0.0
    if strict_items:
        avg_score = sum(float(i.get("regex_score") or 0.0) for i in strict_items) / len(strict_items)

    if list_items and strict_is_strong:
        if bool(hint_behavior.get("prefer_list_when_available")) and not bool(
            hint_behavior.get("prefer_mixed_merge_with_strict")
        ):
            list_doc_type = forced_doc_type_hint or doc_type or "product_list"
            return _finalize_preview_payload(
                {
                    "engine": engine,
                    "file_name": Path(file_path).name,
                    "doc_type": list_doc_type,
                    "embedding_threshold": None,
                    "items_count": len(list_items),
                    "avg_regex_score": 0.0,
                    "items": list_items,
                }
            )
        anchor_lock_to_list = bool(
            list_items
            and strict_items
            and looks_like_product_list
            and layout_hint_norm in {"table", "mixed"}
            and doc_type in {"product_list", "requirement_sheet"}
        )
        mixed_items = _merge_mixed_preview_items(
            list_items,
            strict_items,
            anchor_lock_to_list=anchor_lock_to_list,
        )
        return _finalize_preview_payload(
            {
                "engine": engine,
                "file_name": Path(file_path).name,
                "doc_type": doc_type,
                "embedding_threshold": round(emb_threshold, 4),
                "items_count": len(mixed_items),
                "avg_regex_score": round(avg_score, 4),
                "items": mixed_items,
            }
        )

    if extraction_mode == "smart" and list_items:
        # Smart mode: keep list/table anchors as truth and enrich with strict details.
        anchor_lock_items = list_items
        if strict_items:
            anchor_lock_items = _merge_mixed_preview_items(
                list_items,
                strict_items,
                anchor_lock_to_list=True,
            )
        smart_doc_type = forced_doc_type_hint or ("product_list" if looks_like_product_list else doc_type)
        return _finalize_preview_payload(
            {
                "engine": engine,
                "file_name": Path(file_path).name,
                "doc_type": smart_doc_type,
                "embedding_threshold": round(emb_threshold, 4),
                "items_count": len(anchor_lock_items),
                "avg_regex_score": round(avg_score, 4),
                "items": anchor_lock_items,
            }
        )

    if list_items and not strict_is_strong:
        if not bool(hint_behavior.get("prefer_list_when_available")) and doc_type == "requirement_sheet":
            return _finalize_preview_payload(
                {
                    "engine": engine,
                    "file_name": Path(file_path).name,
                    "doc_type": doc_type,
                    "embedding_threshold": round(emb_threshold, 4),
                    "items_count": len(strict_items),
                    "avg_regex_score": round(avg_score, 4),
                    "items": strict_items,
                }
            )
        fallback_doc_type = forced_doc_type_hint or doc_type
        if not fallback_doc_type:
            fallback_doc_type = "product_list"
        return _finalize_preview_payload(
            {
                "engine": engine,
                "file_name": Path(file_path).name,
                "doc_type": fallback_doc_type,
                "embedding_threshold": None,
                "items_count": len(list_items),
                "avg_regex_score": 0.0,
                "items": list_items,
            }
        )

    return _finalize_preview_payload(
        {
            "engine": engine,
            "file_name": Path(file_path).name,
            "doc_type": doc_type,
            "embedding_threshold": round(emb_threshold, 4),
            "items_count": len(strict_items),
            "avg_regex_score": round(avg_score, 4),
            "items": strict_items,
        }
    )

from typing import Any, Dict

from sqlalchemy.orm import Session

from app.models import Setting, UserSetting
from app.schemas import Settings, EmbeddingThresholds


SETTING_TYPES = {
    "smtp_port": int,
    "use_tls": bool,
    "mode_mock": bool,
    "throttle_seconds": float,
    "max_emails_per_minute": int,
    "max_emails_per_domain_per_minute": int,
    "retry_max_attempts": int,
    "retry_base_delay_seconds": int,
    "embedding_confirm_threshold": float,
    "embedding_verify_threshold": float,
    "embedding_characteristics_threshold": float,
    "embedding_extraction_threshold": float,
    "use_ai_prompt_for_conversion": bool,
}

USER_SETTING_KEYS = {
    "embedding_confirm_threshold",
    "embedding_verify_threshold",
    "embedding_characteristics_threshold",
    "embedding_extraction_threshold",
}

EMBED_CONFIRM_DB_KEY = "embedding_confirm_threshold"
EMBED_VERIFY_DB_KEY = "embedding_verify_threshold"
EMBED_CHARACTERISTICS_DB_KEY = "embedding_characteristics_threshold"
EMBED_EXTRACTION_DB_KEY = "embedding_extraction_threshold"
EMBED_EXTRACTION_MIN = 0.20
EMBED_VERIFY_FIXED = 0.40


def _clamp_extraction_threshold(value: float) -> float:
    return max(EMBED_EXTRACTION_MIN, min(1.0, float(value)))


def _parse_value(key: str, value: str) -> Any:
    if key in SETTING_TYPES:
        cast = SETTING_TYPES[key]
        if cast is bool:
            return value.lower() in ("1", "true", "yes", "on")
        return cast(value)
    return value


def _serialize_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def ensure_defaults(db: Session, tenant_id: int) -> None:
    defaults = Settings().dict()
    existing = {row.key for row in db.query(Setting).filter(Setting.tenant_id == tenant_id).all()}
    for key, value in defaults.items():
        if key not in existing:
            db.add(Setting(tenant_id=tenant_id, key=key, value=_serialize_value(key, value)))
    db.commit()


def get_settings(db: Session, tenant_id: int) -> Settings:
    ensure_defaults(db, tenant_id)
    values: Dict[str, Any] = Settings().dict()
    for row in db.query(Setting).filter(Setting.tenant_id == tenant_id).all():
        values[row.key] = _parse_value(row.key, row.value)
    # Verify threshold is globally fixed for extraction pipeline stability.
    values[EMBED_VERIFY_DB_KEY] = EMBED_VERIFY_FIXED
    return Settings(**values)


def update_settings(db: Session, payload: Settings, tenant_id: int) -> Settings:
    data = payload.dict()
    for key, value in data.items():
        if key == EMBED_VERIFY_DB_KEY:
            value = EMBED_VERIFY_FIXED
        row = db.query(Setting).filter(Setting.tenant_id == tenant_id, Setting.key == key).first()
        if row:
            row.value = _serialize_value(key, value)
        else:
            db.add(Setting(tenant_id=tenant_id, key=key, value=_serialize_value(key, value)))
    db.commit()
    return get_settings(db, tenant_id)


def get_user_embedding_thresholds(db: Session, tenant_id: int, user_id: int) -> EmbeddingThresholds:
    base = EmbeddingThresholds()
    values = base.dict()
    rows = (
        db.query(UserSetting)
        .filter(
            UserSetting.tenant_id == tenant_id,
            UserSetting.user_id == user_id,
            UserSetting.key.in_(USER_SETTING_KEYS),
        )
        .all()
    )
    for row in rows:
        if row.key == EMBED_CONFIRM_DB_KEY:
            values["confirm_threshold"] = _parse_value(row.key, row.value)
        elif row.key == EMBED_VERIFY_DB_KEY:
            # Persisted for compatibility, but ignored at runtime (fixed threshold).
            continue
        elif row.key == EMBED_CHARACTERISTICS_DB_KEY:
            values["characteristics_threshold"] = _parse_value(row.key, row.value)
        elif row.key == EMBED_EXTRACTION_DB_KEY:
            values["extraction_threshold"] = _clamp_extraction_threshold(
                _parse_value(row.key, row.value)
            )
    values["extraction_threshold"] = _clamp_extraction_threshold(
        values["extraction_threshold"]
    )
    values["verify_threshold"] = EMBED_VERIFY_FIXED
    return EmbeddingThresholds(**values)


def update_user_embedding_thresholds(
    db: Session, tenant_id: int, user_id: int, payload: EmbeddingThresholds
) -> EmbeddingThresholds:
    data = {
        EMBED_CONFIRM_DB_KEY: payload.confirm_threshold,
        EMBED_VERIFY_DB_KEY: EMBED_VERIFY_FIXED,
        EMBED_CHARACTERISTICS_DB_KEY: payload.characteristics_threshold,
        EMBED_EXTRACTION_DB_KEY: _clamp_extraction_threshold(
            payload.extraction_threshold
        ),
    }
    for key, value in data.items():
        row = (
            db.query(UserSetting)
            .filter(
                UserSetting.tenant_id == tenant_id,
                UserSetting.user_id == user_id,
                UserSetting.key == key,
            )
            .first()
        )
        if row:
            row.value = _serialize_value(key, value)
        else:
            db.add(
                UserSetting(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    key=key,
                    value=_serialize_value(key, value),
                )
            )
    db.commit()
    return get_user_embedding_thresholds(db, tenant_id, user_id)

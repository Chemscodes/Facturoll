from uuid import uuid4
from datetime import datetime, timedelta

from app import config


def create_token() -> str:
    return uuid4().hex


def token_expiry() -> datetime:
    return datetime.utcnow() + timedelta(days=config.TOKEN_EXPIRE_DAYS)

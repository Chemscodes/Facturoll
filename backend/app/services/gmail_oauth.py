import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from jose import JWTError, jwt

from app import config

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _credentials_path() -> Path:
    path = Path(config.GMAIL_CREDENTIALS_PATH).expanduser()
    if not path.is_file():
        raise HTTPException(status_code=500, detail=f"Gmail credentials file not found: {path}")
    return path


def oauth_redirect_uri() -> str:
    uri = (config.GMAIL_OAUTH_REDIRECT_URI or "").strip()
    if not uri:
        raise HTTPException(status_code=500, detail="GMAIL_OAUTH_REDIRECT_URI is empty.")
    return uri


def _read_credentials_json() -> dict:
    path = _credentials_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Invalid Gmail credentials JSON: {exc}")


def _append_unique(target: list[str], value: str) -> None:
    v = (value or "").strip()
    if not v:
        return
    if v not in target:
        target.append(v)


def _is_loopback_host(hostname: str | None) -> bool:
    host = (hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _is_secure_redirect_uri(uri: str) -> bool:
    parsed = urlparse((uri or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.hostname:
        return False
    # Google OAuth blocks non-HTTPS redirect URIs except localhost/loopback.
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        return False
    return True


def _credentials_redirect_uris() -> list[str]:
    values: list[str] = []
    data = _read_credentials_json()
    client = data.get("web") or data.get("installed") or {}
    for uri in client.get("redirect_uris", []) or []:
        text = str(uri).strip()
        if _is_secure_redirect_uri(text):
            _append_unique(values, text)
    return values


def _candidate_redirect_uris(preferred_redirect_uri: str | None = None) -> list[str]:
    candidates: list[str] = []

    configured = oauth_redirect_uri()
    credential_uris = _credentials_redirect_uris()

    # Always prefer configured/declared redirects over dynamic request URLs.
    for uri in [configured, *credential_uris]:
        if _is_secure_redirect_uri(uri):
            _append_unique(candidates, uri)

    app_uri = f"{config.APP_URL.rstrip('/')}/gmail/oauth/callback"
    if _is_secure_redirect_uri(app_uri):
        _append_unique(candidates, app_uri)

    preferred = (preferred_redirect_uri or "").strip()
    if preferred and _is_secure_redirect_uri(preferred):
        parsed_preferred = urlparse(preferred)
        # Allow explicit preferred value only for loopback/local dev, or if already declared.
        if (
            _is_loopback_host(parsed_preferred.hostname)
            or preferred in credential_uris
            or preferred == configured
        ):
            _append_unique(candidates, preferred)

    # Local aliases are common mismatch sources (localhost vs 127.0.0.1, 8000 vs 8001).
    for base in list(candidates):
        parsed = urlparse(base)
        if parsed.scheme and _is_loopback_host(parsed.hostname):
            path = parsed.path or "/gmail/oauth/callback"
            for host in ("127.0.0.1", "localhost"):
                for port in (8001, 8000):
                    _append_unique(candidates, f"{parsed.scheme}://{host}:{port}{path}")
    return candidates


def choose_redirect_uri(preferred_redirect_uri: str | None = None) -> str:
    candidates = _candidate_redirect_uris(preferred_redirect_uri=preferred_redirect_uri)
    if not candidates:
        raise HTTPException(status_code=500, detail="No Gmail OAuth redirect URI available.")
    return candidates[0]


def create_oauth_state(
    user_id: int,
    tenant_id: int,
    redirect_uri: str | None = None,
) -> str:
    payload = {
        "purpose": "gmail_oauth",
        "user_id": int(user_id),
        "tenant_id": int(tenant_id),
        "redirect_uri": (redirect_uri or "").strip(),
        "exp": datetime.utcnow() + timedelta(minutes=15),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_oauth_state(state: str) -> tuple[int, int, str | None]:
    try:
        payload = jwt.decode(state, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    if payload.get("purpose") != "gmail_oauth":
        raise HTTPException(status_code=400, detail="Invalid OAuth state purpose.")

    try:
        user_id = int(payload.get("user_id") or 0)
        tenant_id = int(payload.get("tenant_id") or 0)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid OAuth state payload.")

    if user_id <= 0 or tenant_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid OAuth state payload.")
    redirect_uri = str(payload.get("redirect_uri") or "").strip() or None
    return user_id, tenant_id, redirect_uri


def build_authorization_url(state: str, redirect_uri: str | None = None) -> tuple[str, str]:
    flow = Flow.from_client_secrets_file(str(_credentials_path()), scopes=SCOPES)
    flow.redirect_uri = choose_redirect_uri(redirect_uri)
    auth_url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return auth_url, returned_state


def exchange_code_for_gmail_account(
    code: str,
    preferred_redirect_uri: str | None = None,
) -> tuple[str, str]:
    errors: list[str] = []
    for redirect_uri in _candidate_redirect_uris(preferred_redirect_uri=preferred_redirect_uri):
        flow = Flow.from_client_secrets_file(str(_credentials_path()), scopes=SCOPES)
        flow.redirect_uri = redirect_uri
        try:
            flow.fetch_token(code=code)
            creds = flow.credentials

            service = build("gmail", "v1", credentials=creds)
            profile = service.users().getProfile(userId="me").execute()
            email_address = str(profile.get("emailAddress") or "").strip().lower()
            if not email_address:
                raise HTTPException(status_code=400, detail="Gmail profile email not found.")
            return creds.to_json(), email_address
        except HTTPException:
            raise
        except Exception as exc:
            errors.append(f"{redirect_uri}: {exc}")

    detail = "Token exchange failed."
    if errors:
        preview = " | ".join(errors[:3])
        detail = f"Token exchange failed ({preview})"
    raise HTTPException(status_code=400, detail=detail)

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import GmailAccount, User
from app.services.gmail_oauth import decode_oauth_state, exchange_code_for_gmail_account

router = APIRouter(prefix="/gmail", tags=["gmail_oauth"])


def _callback_page(title: str, message: str, ok: bool) -> str:
    border = "#16a34a" if ok else "#b91c1c"
    status = "Success" if ok else "Error"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{status}</title>
</head>
<body style="font-family:Arial,sans-serif;background:#f6f7fb;padding:24px;">
  <div style="max-width:560px;margin:40px auto;background:#fff;border:1px solid {border};border-radius:12px;padding:20px;">
    <h2 style="margin-top:0;">{title}</h2>
    <p>{message}</p>
    <p>You can close this tab and return to the app.</p>
  </div>
</body>
</html>"""


@router.get("/oauth/callback", response_class=HTMLResponse)
def gmail_oauth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if error:
        return HTMLResponse(
            _callback_page("Gmail connection failed", f"OAuth error: {error}", ok=False),
            status_code=400,
        )
    if not code:
        return HTMLResponse(
            _callback_page("Gmail connection failed", "Missing OAuth code.", ok=False),
            status_code=400,
        )
    if not state:
        return HTMLResponse(
            _callback_page("Gmail connection failed", "Missing OAuth state.", ok=False),
            status_code=400,
        )

    try:
        user_id, tenant_id, state_redirect_uri = decode_oauth_state(state)
    except HTTPException as exc:
        return HTMLResponse(
            _callback_page("Gmail connection failed", str(exc.detail), ok=False),
            status_code=exc.status_code,
        )

    user = (
        db.query(User)
        .filter(
            User.id == user_id,
            User.tenant_id == tenant_id,
        )
        .first()
    )
    if not user:
        return HTMLResponse(
            _callback_page("Gmail connection failed", "User not found for OAuth state.", ok=False),
            status_code=404,
        )

    callback_redirect_uri = str(request.url.replace(query=""))
    preferred_redirect_uri = state_redirect_uri or callback_redirect_uri
    try:
        token_json, google_email = exchange_code_for_gmail_account(
            code,
            preferred_redirect_uri=preferred_redirect_uri,
        )
    except HTTPException as exc:
        return HTMLResponse(
            _callback_page("Gmail connection failed", str(exc.detail), ok=False),
            status_code=exc.status_code,
        )
    except Exception as exc:
        return HTMLResponse(
            _callback_page("Gmail connection failed", f"Token exchange failed: {exc}", ok=False),
            status_code=500,
        )

    account = (
        db.query(GmailAccount)
        .filter(
            GmailAccount.tenant_id == tenant_id,
            GmailAccount.user_id == user_id,
        )
        .first()
    )
    if not account:
        account = GmailAccount(
            tenant_id=tenant_id,
            user_id=user_id,
            google_email=google_email,
            token_json=token_json,
            enabled=True,
            last_error=None,
        )
        db.add(account)
    else:
        account.google_email = google_email
        account.token_json = token_json
        account.enabled = True
        account.last_error = None

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return HTMLResponse(
            _callback_page("Gmail connection failed", f"Database save failed: {exc}", ok=False),
            status_code=500,
        )

    return HTMLResponse(
        _callback_page("Gmail connected", f"Account linked: {google_email}", ok=True),
        status_code=200,
    )

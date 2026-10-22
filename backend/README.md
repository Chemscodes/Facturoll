# Backend (FastAPI)

Local FastAPI + SQLite backend for the trial app.

## Requirements

- Python 3.10+

## Install

```bash
cd backend
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
cd backend
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

The DB is created automatically at `backend/app.db` and seeded with:
- admin user: admin@local / admin123

## PostgreSQL (recommended for production)

Set `APP_DB_URL` to a Postgres connection string, for example:

```bash
APP_DB_URL=postgresql+psycopg2://trial_user:trial_pass@127.0.0.1:5432/trial_db
```

You can copy `backend/.env.example` to `backend/.env` and update the values.

## Gmail sync on VPS (multi-user)

Use one backend on VPS + one Postgres DB. Each app user can connect one Gmail account.

Required env values:

```bash
APP_URL=https://api.your-domain.com
GMAIL_CREDENTIALS_PATH=/path/to/backend/credentials.json
GMAIL_OAUTH_REDIRECT_URI=https://api.your-domain.com/gmail/oauth/callback
```

User flow:
- Login in desktop app
- Open `Settings`
- Click `Connect Gmail`
- Complete Google consent in browser

Sync worker:

```bash
cd backend
python scripts/gmail_fetch.py
```

The script now syncs all enabled Gmail accounts from DB (`gmail_accounts` table).
If no account exists, it falls back to legacy `gmail_token.json` behavior.

### Linux scheduler (systemd) for Gmail sync

The admin endpoints below can control Gmail sync on Linux when a systemd timer exists:
- `GET /admin/gmail-sync/status`
- `POST /admin/gmail-sync/enable`
- `POST /admin/gmail-sync/disable`

Create units on VPS:

```bash
cat >/etc/systemd/system/saas-gmail-sync.service <<'UNIT'
[Unit]
Description=SAAS Gmail Fetch
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/saas/backend
ExecStart=/opt/saas/backend/.venv/bin/python /opt/saas/backend/scripts/gmail_fetch.py
UNIT

cat >/etc/systemd/system/saas-gmail-sync.timer <<'UNIT'
[Unit]
Description=Run SAAS Gmail Fetch every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
Persistent=true
Unit=saas-gmail-sync.service

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now saas-gmail-sync.timer
systemctl status saas-gmail-sync.timer --no-pager -l
```

If you use custom unit names, set these env vars:
- `GMAIL_SYNC_TIMER_UNIT`
- `GMAIL_SYNC_SERVICE_UNIT`

### Production process manager (systemd, safe install)

The repo includes managed systemd templates for:
- `saas-backend.service`
- `saas-email-worker.service`
- `saas-embedding-worker.service`
- `saas-gmail-sync.service`
- `saas-gmail-sync.timer`

Installer script (runs on VPS, with backup + rollback on failure):

```bash
cd /opt/saas/backend
bash scripts/install_systemd_units.sh \
  --backend-dir /opt/saas/backend \
  --run-user root \
  --restart \
  --validate-health \
  --health-url http://127.0.0.1:8001/health
```

If health validation fails, unit files are restored from backup automatically.

## SQLite -> PostgreSQL migration

If you want to keep your existing SQLite data, use:

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --sqlite d:/SAAS/backend/app.db \
  --postgres postgresql+psycopg2://trial_user:trial_pass@127.0.0.1:5432/trial_db
```

If the target Postgres already has data, add `--wipe` to overwrite.

## Mock email mode

By default, `mode_mock=true` in settings and emails are stored at `backend/outbox` as `.eml` files.

## Email queue + worker

`/campaigns/{id}/send` now queues email jobs instead of sending inline. Run the worker in a separate process:

```bash
cd backend
python -m app.email_worker --parallel 3
```

Optional watchdog timeout (requeue stale `SENDING` jobs after crash/restart):

```bash
python -m app.email_worker --parallel 3 --stuck-timeout 600
```

On Windows you can use:

```powershell
d:\SAAS\start_worker.ps1 -Parallel 3
```

Embedding queue worker:

```bash
python -m app.embedding_worker --stuck-timeout 900
```

## Queue watchdog + DB indexes

At startup (`init_db`), the backend now ensures queue indexes are present for:
- `email_jobs` queue polling and active job lookup
- `embedding_jobs` queue polling and stuck-job watchdog
- `invoice_extraction_items` pending-embedding scans

## SES bounces/complaints webhook

Configure SES/SNS to POST notifications to:

```
POST http://127.0.0.1:8000/webhooks/ses
```

If you set `WEBHOOK_TOKEN` in `.env`, add `X-Webhook-Token` header or `?token=...` in the URL.

## SMTP configuration

Use the Settings screen in the desktop app or call:

```bash
curl -X PUT http://127.0.0.1:8000/settings \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "email_provider":"smtp",
    "smtp_host":"smtp.gmail.com",
    "smtp_port":587,
    "smtp_user":"you@gmail.com",
    "smtp_password":"app-password",
    "sendgrid_api_key":"",
    "from_email":"you@gmail.com",
    "reply_to":"you@gmail.com",
    "use_tls":true,
    "mode_mock":false,
    "throttle_seconds":1
  }'
```

## SendGrid configuration

Use provider `sendgrid` with an API key:

```bash
curl -X PUT http://127.0.0.1:8000/settings \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "email_provider":"sendgrid",
    "sendgrid_api_key":"SG.xxxxx",
    "from_email":"verified-sender@yourdomain.com",
    "reply_to":"ops@yourdomain.com",
    "mode_mock":false
  }'
```

## Backups (PostgreSQL)

Windows PowerShell:

```powershell
cd d:\SAAS\backend
.\scripts\backup_postgres.ps1
```

Python (cross-platform):

```bash
python scripts/backup_postgres.py --db-url postgresql+psycopg2://user:pass@127.0.0.1:5432/db --output backups
```

## DB migrations (Alembic)

Initialize a migration:

```bash
alembic revision --autogenerate -m "init"
```

Apply migrations:

```bash
alembic upgrade head
```

Schema management mode (`backend/.env`):

```bash
# local/dev compatibility mode (default)
APP_DB_SCHEMA_MODE=legacy

# production/VPS strict mode (recommended)
# backend startup fails fast if DB is not at Alembic head
APP_DB_SCHEMA_MODE=alembic_strict
```

Recommended VPS deploy order:
1. `alembic upgrade head`
2. start API process
3. start workers

This avoids startup against an outdated schema.

## Tests

```bash
cd backend
pytest
```

## Attachment Extraction Evaluation (batch)

Run extraction quality benchmark on one PDF or a folder:

```bash
cd backend
python scripts/evaluate_attachment_extraction.py --input "D:/my_pdfs" --limit 20
```

Outputs:
- `backend/reports/attachment_extraction_eval_summary.json`
- `backend/reports/attachment_extraction_eval_rows.csv`

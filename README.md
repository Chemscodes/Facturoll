# Trial Local App

Local trial app with Flutter Desktop + FastAPI + SQLite.

## Quick start

1) Backend

```bash
cd backend
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

2) Desktop

```bash
cd desktop
flutter pub get
flutter run -d windows   # or linux / macos
```

## Default credentials

- admin@local / admin123

## Mock email

- Settings -> enable mock mode to write `.eml` files to `backend/outbox`.

## Response form

- Email includes a link like `http://127.0.0.1:8000/r/<token>`
- The form is hosted by FastAPI (HTML).

## Tests

```bash
cd backend
pytest
```

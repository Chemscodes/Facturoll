import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from app.db import init_db
from app.seeds import seed_data
from app.routes import (
    admin,
    auth,
    gmail_oauth,
    suppliers,
    groups,
    products,
    campaigns,
    public_forms,
    settings,
    templates,
    bulk,
    webhooks,
    invoice_extractions,
    tech_sheets,
)


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = config.LOG_DIR / "app.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


setup_logging()
logger = logging.getLogger("app")

app = FastAPI(title="Trial Local API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(gmail_oauth.router)
app.include_router(suppliers.router)
app.include_router(groups.router)
app.include_router(products.router)
app.include_router(campaigns.router)
app.include_router(settings.router)
app.include_router(public_forms.router)
app.include_router(admin.router)
app.include_router(templates.router)
app.include_router(bulk.router)
app.include_router(webhooks.router)
app.include_router(invoice_extractions.router)
app.include_router(tech_sheets.router)


@app.on_event("startup")
def on_startup() -> None:
    logger.info("Initializing database")
    init_db()
    seed_data()
    logger.info("Database ready")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}

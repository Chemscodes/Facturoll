import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    func,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class SupplierType(enum.Enum):
    MEDICAMENT = "MEDICAMENT"
    MATERIEL = "MATERIEL"


class CampaignStatus(enum.Enum):
    DRAFT = "DRAFT"
    SENDING = "SENDING"
    SENT = "SENT"
    CLOSED = "CLOSED"


class DeliveryStatus(enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class EmailJobStatus(enum.Enum):
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class SuppressionReason(enum.Enum):
    BOUNCE = "BOUNCE"
    COMPLAINT = "COMPLAINT"
    MANUAL = "MANUAL"


class ExtractionStatus(enum.Enum):
    PENDING = "PENDING"
    TO_VERIFY = "TO_VERIFY"
    CONFIRMED = "CONFIRMED"


class EmbeddingJobStatus(enum.Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class SupplierInvoiceFormat(Base):
    __tablename__ = "supplier_invoice_formats"
    __table_args__ = (UniqueConstraint("tenant_id", "sender_email", name="uq_supplier_invoice_format"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    sender_email = Column(String(255), nullable=False, index=True)
    signature = Column(String(500), nullable=False)
    parser_type = Column(String(50), nullable=False, default="table_v1")
    created_at = Column(DateTime, default=func.now())


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class GmailAccount(Base):
    __tablename__ = "gmail_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_gmail_account_user"),
        UniqueConstraint("tenant_id", "google_email", name="uq_gmail_account_email"),
        Index("ix_gmail_accounts_enabled", "enabled", "tenant_id", "user_id"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    google_email = Column(String(255), nullable=False)
    token_json = Column(Text, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    last_sync_at = Column(DateTime)
    last_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User")


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    supplier_type = Column(Enum(SupplierType), nullable=False)
    supplier_category = Column(String(120))
    phone = Column(String(50))
    notes = Column(Text)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    group_memberships = relationship("GroupMember", back_populates="supplier", cascade="all, delete")


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    members = relationship("GroupMember", back_populates="group", cascade="all, delete")


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "supplier_id", name="uq_group_supplier"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)

    group = relationship("Group", back_populates="members")
    supplier = relationship("Supplier", back_populates="group_memberships")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    sku = Column(String(100))
    unit = Column(String(50))
    description = Column(Text)
    characteristics = Column(JSON)
    embedding_score = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    subject = Column(String(255), nullable=False)
    message_html = Column(Text, nullable=False)
    priority = Column(Integer, default=0, nullable=False)
    establishment_name = Column(String(255))
    lot_reference = Column(String(255))
    establishment_address = Column(Text)
    establishment_phone = Column(String(50))
    pharmacy_service = Column(String(255))
    market_office_service = Column(String(255))
    establishment_type = Column(String(255))
    processing_mode = Column(String(32))
    deposit_date = Column(DateTime)
    processing_in_progress = Column(Boolean, default=False, nullable=False)
    processing_validated = Column(Boolean, default=False, nullable=False)
    status = Column(Enum(CampaignStatus), default=CampaignStatus.DRAFT, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    sent_at = Column(DateTime)

    items = relationship("CampaignItem", back_populates="campaign", cascade="all, delete-orphan")
    recipients = relationship("CampaignRecipient", back_populates="campaign", cascade="all, delete-orphan")
    responses = relationship("Response", back_populates="campaign", cascade="all, delete-orphan")
    attachments = relationship("CampaignAttachment", back_populates="campaign", cascade="all, delete-orphan")
    results_caches = relationship(
        "CampaignResultsCache",
        back_populates="campaign",
        cascade="all, delete-orphan",
    )


class CampaignAttachment(Base):
    __tablename__ = "campaign_attachments"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(Text, nullable=False)
    file_size = Column(Integer, default=0, nullable=False)
    parse_engine = Column(String(50))
    parse_score = Column(Float)
    extraction_preview = Column(JSON)
    strict_extraction_preview = Column(JSON)
    extracted_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    campaign = relationship("Campaign", back_populates="attachments")


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    subject = Column(String(255), nullable=False)
    body_html = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class CampaignItem(Base):
    __tablename__ = "campaign_items"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    requested_qty = Column(Float)
    requested_qty_source = Column(String(32))
    requested_qty_confidence = Column(Float)
    requested_description = Column(Text)
    requested_characteristics = Column(JSON)
    requested_embedding_score = Column(Float)

    campaign = relationship("Campaign", back_populates="items")
    product = relationship("Product")


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (UniqueConstraint("campaign_id", "supplier_id", name="uq_campaign_supplier"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(64), unique=True, nullable=False)
    token_expires_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime)
    delivery_status = Column(Enum(DeliveryStatus), default=DeliveryStatus.PENDING, nullable=False)
    last_error = Column(Text)

    campaign = relationship("Campaign", back_populates="recipients")
    supplier = relationship("Supplier")


class EmailJob(Base):
    __tablename__ = "email_jobs"
    __table_args__ = (
        Index(
            "ix_email_jobs_queue_pick",
            "status",
            "next_attempt_at",
            "priority",
            "created_at",
        ),
        Index(
            "ix_email_jobs_campaign_active",
            "tenant_id",
            "campaign_id",
            "status",
            "recipient_id",
        ),
        Index(
            "ix_email_jobs_recipient_latest",
            "tenant_id",
            "recipient_id",
            "created_at",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    recipient_id = Column(Integer, ForeignKey("campaign_recipients.id", ondelete="CASCADE"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    to_email = Column(String(255), nullable=False)
    subject = Column(String(255), nullable=False)
    html_body = Column(Text, nullable=False)
    priority = Column(Integer, default=0, nullable=False)
    status = Column(Enum(EmailJobStatus), default=EmailJobStatus.QUEUED, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=5, nullable=False)
    last_error = Column(Text)
    last_attempt_at = Column(DateTime)
    next_attempt_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    sent_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    recipient = relationship("CampaignRecipient")
    supplier = relationship("Supplier")


class Response(Base):
    __tablename__ = "responses"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    submitted_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    responder_email = Column(String(255), nullable=False)
    ip = Column(String(64))

    campaign = relationship("Campaign", back_populates="responses")
    supplier = relationship("Supplier")
    items = relationship("ResponseItem", back_populates="response", cascade="all, delete-orphan")


class ResponseItem(Base):
    __tablename__ = "response_items"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    response_id = Column(Integer, ForeignKey("responses.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    offered_qty = Column(Float, nullable=False)
    unit_price = Column(Float, nullable=False)
    currency = Column(String(8), default="DZD", nullable=False)
    notes = Column(Text)

    response = relationship("Response", back_populates="items")
    product = relationship("Product")


class SuppressedEmail(Base):
    __tablename__ = "suppressed_emails"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_tenant_suppressed_email"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False)
    reason = Column(Enum(SuppressionReason), nullable=False)
    details = Column(Text)


class InboundInvoice(Base):
    __tablename__ = "inbound_invoices"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    sender_email = Column(String(255), nullable=False)
    sender_name = Column(String(255))
    sender_phone = Column(String(50))
    parse_engine = Column(String(50))
    parse_score = Column(Float)
    ocr_text_snapshot = Column(Text)
    subject = Column(String(255))
    message_id = Column(String(255))
    file_name = Column(String(255), nullable=False)
    file_path = Column(Text, nullable=False)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    campaign = relationship("Campaign")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    extraction_items = relationship(
        "InvoiceExtractionItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


class TechSheet(Base):
    __tablename__ = "tech_sheets"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"))
    sender_email = Column(String(255), nullable=True)

    file_name = Column(String(255), nullable=False)
    file_path = Column(Text, nullable=False)
    file_hash = Column(String(64), index=True)

    parse_engine = Column(String(50))
    parse_score = Column(Float)
    raw_text = Column(Text)
    characteristics = Column(JSON)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    campaign = relationship("Campaign")
    supplier = relationship("Supplier")


class TechSheetLink(Base):
    __tablename__ = "tech_sheet_links"
    __table_args__ = (UniqueConstraint("invoice_item_id", name="uq_invoice_item_link"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    campaign_id = Column(Integer, nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"))
    invoice_item_id = Column(Integer, ForeignKey("invoice_extraction_items.id", ondelete="CASCADE"), nullable=False)
    tech_sheet_id = Column(Integer, ForeignKey("tech_sheets.id", ondelete="CASCADE"), nullable=False)
    score = Column(Float, nullable=False, default=0.0)
    method = Column(String(50), nullable=False, default="embedding")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    invoice_item = relationship("InvoiceExtractionItem")
    tech_sheet = relationship("TechSheet")


class InvoiceExtractionItem(Base):
    __tablename__ = "invoice_extraction_items"
    __table_args__ = (
        Index(
            "ix_invoice_items_embedding_pending",
            "status",
            "notes",
            "campaign_id",
            "tenant_id",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("inbound_invoices.id", ondelete="CASCADE"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"))
    source_engine = Column(String(50), nullable=False, default="document_ai")
    status = Column(Enum(ExtractionStatus), default=ExtractionStatus.PENDING, nullable=False)

    product_name = Column(String(255), nullable=False)
    description = Column(Text)
    raw_description = Column(Text)
    product_name_norm = Column(String(255))
    unit = Column(String(50))
    quantity = Column(Float)
    unit_price = Column(Float)
    currency = Column(String(8), default="DZD", nullable=False)
    confidence = Column(Float)

    corrected_product_name = Column(String(255))
    corrected_unit = Column(String(50))
    corrected_quantity = Column(Float)
    corrected_unit_price = Column(Float)
    corrected_currency = Column(String(8))
    similarity_score = Column(Float)
    similarity_threshold = Column(Float)
    similarity_points = Column(JSON)
    strict_components_payload = Column(JSON)

    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    invoice = relationship("InboundInvoice", back_populates="extraction_items")
    campaign = relationship("Campaign")
    supplier = relationship("Supplier")


class InvoiceItemCharacteristicMatch(Base):
    __tablename__ = "invoice_item_characteristic_matches"
    __table_args__ = (
        UniqueConstraint(
            "invoice_item_id",
            "requested_product_norm",
            name="uq_invoice_item_requested_match",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_item_id = Column(
        Integer,
        ForeignKey("invoice_extraction_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requested_product_name = Column(String(255), nullable=False)
    requested_product_norm = Column(String(255), nullable=False, index=True)
    matched_product_name = Column(String(255))
    threshold = Column(Float)
    score = Column(Float)
    matched_points = Column(Integer)
    total_points = Column(Integer)
    points = Column(JSON)
    source = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    invoice_item = relationship("InvoiceExtractionItem")
    campaign = relationship("Campaign")


class InvoiceItemOfferAnalysisCache(Base):
    __tablename__ = "invoice_item_offer_analysis_cache"
    __table_args__ = (
        UniqueConstraint(
            "invoice_item_id",
            "requested_product_norm",
            name="uq_invoice_item_offer_analysis",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_item_id = Column(
        Integer,
        ForeignKey("invoice_extraction_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requested_product_name = Column(String(255), nullable=False)
    requested_product_norm = Column(String(255), nullable=False, index=True)
    tech_sheet_id = Column(Integer, ForeignKey("tech_sheets.id", ondelete="SET NULL"))
    signature = Column(String(64), nullable=False, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    invoice_item = relationship("InvoiceExtractionItem")
    campaign = relationship("Campaign")
    tech_sheet = relationship("TechSheet")


class OfferAnalysisResult(Base):
    __tablename__ = "offer_analysis_results"
    __table_args__ = (
        UniqueConstraint(
            "invoice_item_id",
            "requested_product_norm",
            name="uq_offer_analysis_result",
        ),
        Index(
            "ix_offer_analysis_result_campaign",
            "tenant_id",
            "campaign_id",
            "requested_product_id",
        ),
        Index(
            "ix_offer_analysis_result_item",
            "tenant_id",
            "campaign_id",
            "invoice_item_id",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_item_id = Column(
        Integer,
        ForeignKey("invoice_extraction_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requested_product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"))
    requested_product_name = Column(String(255), nullable=False)
    requested_product_norm = Column(String(255), nullable=False, index=True)
    tech_sheet_id = Column(Integer, ForeignKey("tech_sheets.id", ondelete="SET NULL"))
    signature = Column(String(64), nullable=False, index=True)
    gpt_match_score = Column(Float)
    tech_sheet_similarity_score = Column(Float)
    tech_sheet_similarity_status = Column(String(50))
    tech_sheet_similarity_display_score = Column(Float)
    tech_sheet_similarity_display_status = Column(String(50))
    characteristics_matched = Column(Integer)
    characteristics_total = Column(Integer)
    characteristics_coverage = Column(Float)
    requirements_matched = Column(Integer)
    requirements_total = Column(Integer)
    requirements_coverage = Column(Float)
    accessories_matched = Column(Integer)
    accessories_total = Column(Integer)
    accessories_coverage = Column(Float)
    description_score = Column(Float)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    invoice_item = relationship("InvoiceExtractionItem")
    requested_product = relationship("Product")
    campaign = relationship("Campaign")
    tech_sheet = relationship("TechSheet")


class CampaignResultsCache(Base):
    __tablename__ = "campaign_results_cache"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "campaign_id",
            name="uq_campaign_results_cache_scope",
        ),
        Index(
            "ix_campaign_results_cache_lookup",
            "tenant_id",
            "user_id",
            "campaign_id",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    cache_key = Column(String(100), nullable=False, default="results_v1")
    signature = Column(JSON)
    payload = Column(JSON)
    built_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    campaign = relationship("Campaign", back_populates="results_caches")
    user = relationship("User")


class CampaignMarketEstimateOffer(Base):
    __tablename__ = "campaign_market_estimate_offers"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "campaign_id",
            "product_id",
            name="uq_campaign_market_estimate_offer_scope",
        ),
        Index(
            "ix_campaign_market_estimate_offer_lookup",
            "tenant_id",
            "campaign_id",
            "product_id",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    model_name = Column(String(100), nullable=False, default="gpt-5.1")
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    campaign = relationship("Campaign")
    product = relationship("Product")
    created_by_user = relationship("User")


class OpenAIPayloadCache(Base):
    __tablename__ = "openai_payload_cache"
    __table_args__ = (
        UniqueConstraint(
            "cache_type",
            "model",
            "cache_key",
            name="uq_openai_payload_cache",
        ),
        Index(
            "ix_openai_payload_cache_lookup",
            "cache_type",
            "model",
            "cache_key",
        ),
    )

    id = Column(Integer, primary_key=True)
    cache_type = Column(String(50), nullable=False)
    model = Column(String(100), nullable=False)
    cache_key = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class OpenAIUsageLog(Base):
    __tablename__ = "openai_usage_logs"
    __table_args__ = (
        Index("ix_openai_usage_logs_created_at", "created_at"),
        Index("ix_openai_usage_logs_model_created_at", "model", "created_at"),
        Index("ix_openai_usage_logs_operation_created_at", "operation", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    operation = Column(String(80), nullable=False)
    endpoint = Column(String(120), nullable=False)
    model = Column(String(100), nullable=False)
    success = Column(Boolean, default=True, nullable=False)
    error_type = Column(String(120))
    latency_ms = Column(Integer)
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    total_tokens = Column(Integer)
    estimated_cost_usd = Column(Float)
    metadata_json = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_tenant_setting"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    key = Column(String(100), nullable=False)
    value = Column(String(500), nullable=False)


class UserSetting(Base):
    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", "key", name="uq_user_setting"),)

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key = Column(String(100), nullable=False)
    value = Column(String(500), nullable=False)


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"
    __table_args__ = (UniqueConstraint("model", "text_hash", name="uq_embedding_cache"),)

    id = Column(Integer, primary_key=True)
    model = Column(String(100), nullable=False)
    text_hash = Column(String(64), nullable=False)
    text = Column(Text)
    embedding = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EmbeddingJob(Base):
    __tablename__ = "embedding_jobs"
    __table_args__ = (
        UniqueConstraint("model", "text_hash", name="uq_embedding_job"),
        Index(
            "ix_embedding_jobs_queue_pick",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index(
            "ix_embedding_jobs_processing_watchdog",
            "status",
            "updated_at",
        ),
    )

    id = Column(Integer, primary_key=True)
    model = Column(String(100), nullable=False)
    text_hash = Column(String(64), nullable=False)
    text = Column(Text)
    status = Column(Enum(EmbeddingJobStatus), default=EmbeddingJobStatus.QUEUED, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    last_error = Column(Text)
    next_attempt_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

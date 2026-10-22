from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, EmailStr, Field, validator

from app.models import CampaignStatus, DeliveryStatus, SupplierType, ExtractionStatus
from app import config


class AuthLogin(BaseModel):
    email: str
    password: str


class AuthToken(BaseModel):
    access_token: str


class UserProfile(BaseModel):
    email: str
    tenant_id: int
    is_admin: bool


class SupplierBase(BaseModel):
    name: str
    email: str
    supplier_type: SupplierType
    supplier_category: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    active: bool = True


class SupplierCreate(SupplierBase):
    pass


class SupplierUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    supplier_type: Optional[SupplierType] = None
    supplier_category: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    active: Optional[bool] = None


class SupplierOut(SupplierBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True


class GroupBase(BaseModel):
    name: str
    description: Optional[str] = None


class GroupCreate(GroupBase):
    pass


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class GroupOut(GroupBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True


class GroupMembersUpdate(BaseModel):
    supplier_ids: List[int] = Field(default_factory=list)


class GroupMemberOut(BaseModel):
    id: int
    group_id: int
    supplier_id: int

    class Config:
        orm_mode = True


class ProductBase(BaseModel):
    name: str
    sku: Optional[str] = None
    unit: Optional[str] = None
    description: Optional[str] = None
    characteristics: Optional[dict] = None
    embedding_score: Optional[float] = None


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None
    unit: Optional[str] = None
    description: Optional[str] = None
    characteristics: Optional[dict] = None
    embedding_score: Optional[float] = None


class ProductOut(ProductBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True


class CampaignBase(BaseModel):
    title: str
    subject: str
    message_html: str
    priority: int = 0
    processing_mode: Optional[str] = None
    establishment_name: Optional[str] = None
    lot_reference: Optional[str] = None
    establishment_address: Optional[str] = None
    establishment_phone: Optional[str] = None
    pharmacy_service: Optional[str] = None
    market_office_service: Optional[str] = None
    establishment_type: Optional[str] = None
    deposit_date: Optional[datetime] = None
    processing_in_progress: bool = False
    processing_validated: bool = False


class CampaignCreate(CampaignBase):
    pass


class CampaignUpdate(BaseModel):
    title: Optional[str] = None
    subject: Optional[str] = None
    message_html: Optional[str] = None
    priority: Optional[int] = None
    processing_mode: Optional[str] = None
    establishment_name: Optional[str] = None
    lot_reference: Optional[str] = None
    establishment_address: Optional[str] = None
    establishment_phone: Optional[str] = None
    pharmacy_service: Optional[str] = None
    market_office_service: Optional[str] = None
    establishment_type: Optional[str] = None
    deposit_date: Optional[datetime] = None
    processing_in_progress: Optional[bool] = None
    processing_validated: Optional[bool] = None
    status: Optional[CampaignStatus] = None


class CampaignOut(CampaignBase):
    id: int
    status: CampaignStatus
    created_at: datetime
    sent_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class CampaignAttachmentOut(BaseModel):
    id: int
    campaign_id: int
    file_name: str
    file_path: str
    file_size: int
    parse_engine: Optional[str] = None
    parse_score: Optional[float] = None
    extraction_preview: Optional[dict] = None
    strict_extraction_preview: Optional[dict] = None
    extracted_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        orm_mode = True


class CampaignItemIn(BaseModel):
    product_id: int
    requested_qty: Optional[float] = None
    requested_description: Optional[str] = None
    requested_characteristics: Optional[dict] = None
    requested_embedding_score: Optional[float] = None


class CampaignItemsPayload(BaseModel):
    items: List[CampaignItemIn] = Field(default_factory=list)


class CampaignItemOut(BaseModel):
    id: int
    product_id: int
    requested_qty: Optional[float] = None
    requested_qty_source: Optional[str] = None
    requested_qty_confidence: Optional[float] = None
    product_name: Optional[str] = None
    requested_description: Optional[str] = None
    requested_characteristics: Optional[dict] = None
    requested_embedding_score: Optional[float] = None
    product_description: Optional[str] = None
    product_characteristics: Optional[dict] = None
    product_embedding_score: Optional[float] = None

    class Config:
        orm_mode = True


class ParseCharacteristicsFromTextIn(BaseModel):
    text: str


class ParseCharacteristicsFromTextOut(BaseModel):
    requested_characteristics: dict = Field(default_factory=dict)
    score: float = 0.0
    score_regex_only: float = 0.0
    pairs_count: int = 0


class CampaignRecipientPayload(BaseModel):
    group_ids: List[int] = Field(default_factory=list)
    supplier_ids: List[int] = Field(default_factory=list)


class CampaignRecipientOut(BaseModel):
    id: int
    campaign_id: int
    supplier_id: int
    token: str
    token_expires_at: datetime
    sent_at: Optional[datetime] = None
    delivery_status: DeliveryStatus
    last_error: Optional[str] = None

    class Config:
        orm_mode = True


class ResponseItemOut(BaseModel):
    id: int
    product_id: int
    offered_qty: float
    unit_price: float
    currency: str
    notes: Optional[str] = None

    class Config:
        orm_mode = True


class ResponseOut(BaseModel):
    id: int
    campaign_id: int
    supplier_id: int
    submitted_at: datetime
    responder_email: str
    ip: Optional[str] = None
    items: List[ResponseItemOut] = Field(default_factory=list)

    class Config:
        orm_mode = True


class InboundInvoiceOut(BaseModel):
    id: int
    campaign_id: int
    sender_email: str
    sender_name: Optional[str] = None
    sender_phone: Optional[str] = None
    subject: Optional[str] = None
    message_id: Optional[str] = None
    file_name: str
    file_path: str
    received_at: datetime

    class Config:
        orm_mode = True


class ImportedEmailFileOut(BaseModel):
    file_type: str
    file_id: int
    file_name: str
    sender_email: Optional[str] = None
    sender_name: Optional[str] = None
    subject: Optional[str] = None
    campaign_id: int
    campaign_title: Optional[str] = None
    processing_mode: Optional[str] = None
    received_at: datetime
    linked_items_count: Optional[int] = None
    parse_engine: Optional[str] = None
    parse_score: Optional[float] = None


class ImportedEmailFileReassignPayload(BaseModel):
    campaign_id: int
    invoice_ids: List[int] = Field(default_factory=list)
    tech_sheet_ids: List[int] = Field(default_factory=list)


class ImportedEmailFileReassignResult(BaseModel):
    moved_invoices: int
    moved_tech_sheets: int
    cleared_links: int
    cleared_offer_analysis_results: int
    cleared_offer_analysis_cache: int
    invalidated_results_cache: int


class ImportedEmailFileExtractPayload(BaseModel):
    invoice_ids: List[int] = Field(default_factory=list)
    tech_sheet_ids: List[int] = Field(default_factory=list)
    force: bool = True


class ImportedEmailFileExtractResult(BaseModel):
    extracted_invoices: int
    extracted_invoice_items: int
    extracted_tech_sheets: int
    relinked_tech_sheet_links: int
    cleared_links: int
    cleared_offer_analysis_results: int
    cleared_offer_analysis_cache: int
    invalidated_results_cache: int


class InvoiceExtractionItemOut(BaseModel):
    id: int
    invoice_id: int
    campaign_id: int
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    supplier_email: Optional[str] = None
    invoice_file_name: Optional[str] = None
    invoice_file_path: Optional[str] = None
    tech_sheet_id: Optional[int] = None
    tech_sheet_file: Optional[str] = None
    tech_sheet_file_path: Optional[str] = None
    source_engine: str
    status: ExtractionStatus
    product_name: str
    description: Optional[str] = None
    unit: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    currency: str
    confidence: Optional[float] = None
    corrected_product_name: Optional[str] = None
    corrected_unit: Optional[str] = None
    corrected_quantity: Optional[float] = None
    corrected_unit_price: Optional[float] = None
    corrected_currency: Optional[str] = None
    quantity_raw: Optional[str] = None
    quantity_num_type: Optional[str] = None
    unit_price_raw: Optional[str] = None
    unit_price_num_type: Optional[str] = None
    line_total_raw: Optional[str] = None
    line_total_num_type: Optional[str] = None
    similarity_score: Optional[float] = None
    similarity_threshold: Optional[float] = None
    similarity_points: Optional[dict] = None
    strict_components_payload: Optional[dict] = None
    embedding_match_rate: Optional[float] = None
    matched_product_name: Optional[str] = None
    matched_characteristics: Optional[int] = None
    total_characteristics: Optional[int] = None
    tech_sheet_similarity_score: Optional[float] = None
    tech_sheet_similarity_status: Optional[str] = None
    tech_sheet_similarity_breakdown: Optional[dict] = None
    tech_sheet_similarity_display_score: Optional[float] = None
    tech_sheet_similarity_display_status: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

    def dict(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        data = super().dict(*args, **kwargs)
        payload = data.get("strict_components_payload")
        if isinstance(payload, dict):
            trace = payload.get("numeric_trace")
            if isinstance(trace, dict):
                data["quantity_raw"] = str(trace.get("quantity_raw") or "").strip() or None
                data["quantity_num_type"] = str(trace.get("quantity_num_type") or "").strip() or None
                data["unit_price_raw"] = str(trace.get("unit_price_raw") or "").strip() or None
                data["unit_price_num_type"] = str(trace.get("unit_price_num_type") or "").strip() or None
                data["line_total_raw"] = str(trace.get("line_total_raw") or "").strip() or None
                data["line_total_num_type"] = str(trace.get("line_total_num_type") or "").strip() or None
        return data


class InvoiceExtractionItemCreate(BaseModel):
    invoice_id: int
    product_name: str
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    unit_price: Optional[float] = None
    currency: Optional[str] = None
    status: Optional[ExtractionStatus] = None


class InvoiceExtractionItemUpdate(BaseModel):
    product_name: Optional[str] = None
    description: Optional[str] = None
    unit: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    currency: Optional[str] = None
    corrected_product_name: Optional[str] = None
    corrected_unit: Optional[str] = None
    corrected_quantity: Optional[float] = None
    corrected_unit_price: Optional[float] = None
    corrected_currency: Optional[str] = None
    status: Optional[ExtractionStatus] = None
    notes: Optional[str] = None


class CampaignStats(BaseModel):
    total_recipients: int
    sent_count: int
    failed_count: int
    responses_count: int
    pending_responses_count: int
    response_rate: float
    costs: dict[str, Any] | None = None


class Settings(BaseModel):
    email_provider: str = "sendgrid"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    sendgrid_api_key: str = ""
    from_email: str = ""
    reply_to: str = ""
    use_tls: bool = True
    mode_mock: bool = True
    throttle_seconds: float = 1.0
    max_emails_per_minute: int = 300
    max_emails_per_domain_per_minute: int = 60
    retry_max_attempts: int = 5
    retry_base_delay_seconds: int = 30
    embedding_confirm_threshold: float = config.OPENAI_EMBEDDING_THRESHOLD
    embedding_verify_threshold: float = 0.40
    use_ai_prompt_for_conversion: bool = True

    @validator("email_provider", pre=True, always=True)
    def normalize_email_provider(cls, value: str | None) -> str:
        provider = str(value or "sendgrid").strip().lower()
        if provider == "smtp":
            return "smtp"
        return "sendgrid"


class SettingsTestEmail(BaseModel):
    to_email: str


class EmbeddingThresholds(BaseModel):
    confirm_threshold: float = config.OPENAI_EMBEDDING_THRESHOLD
    verify_threshold: float = 0.40
    characteristics_threshold: float = Field(
        default=config.OPENAI_CHARACTERISTICS_MATCH_THRESHOLD,
        ge=0.0,
        le=1.0,
    )
    extraction_threshold: float = Field(
        default=config.OPENAI_EXTRACTION_EMBEDDING_THRESHOLD,
        ge=0.20,
        le=1.0,
    )


class EmailQueueStats(BaseModel):
    queued: int
    sending: int
    sent: int
    failed: int
    suppressed: int


class CharacteristicMatchDetail(BaseModel):
    key: str
    requested_values: List[str] = Field(default_factory=list)
    offered_values: List[str] = Field(default_factory=list)
    best_requested: Optional[str] = None
    best_offered: Optional[str] = None
    score: Optional[float] = None
    matched: Optional[bool] = None


class OfferEntry(BaseModel):
    supplier_id: int
    supplier_name: str
    supplier_email: Optional[str] = None
    supplier_phone: Optional[str] = None
    sender_email: Optional[str] = None
    invoice_sender_email: Optional[str] = None
    market_estimate: Optional[bool] = None
    market_estimate_id: Optional[int] = None
    market_estimate_region: Optional[str] = None
    market_estimate_model: Optional[str] = None
    market_estimate_sources: Optional[List[str]] = None
    requested_catalog_name: Optional[str] = None
    requested_strict_name_used: Optional[str] = None
    requested_strict_line_used: Optional[str] = None
    requested_strict_source: Optional[str] = None
    requested_qty: Optional[float] = None
    invoice_item_id: Optional[int] = None
    invoice_id: Optional[int] = None
    invoice_file_name: Optional[str] = None
    invoice_file_path: Optional[str] = None
    tech_sheet_id: Optional[int] = None
    tech_sheet_file: Optional[str] = None
    tech_sheet_file_path: Optional[str] = None
    offered_qty: Optional[float] = None
    offered_qty_raw: Optional[float] = None
    offered_qty_strict: Optional[float] = None
    unit_price: Optional[float] = None
    vat_rate: Optional[float] = None
    equivalent_unit_price: Optional[float] = None
    equivalence_factor: Optional[float] = None
    equivalence_basis: Optional[str] = None
    equivalence_confidence: Optional[float] = None
    currency: str
    price_source: Optional[str] = None
    extracted_name: Optional[str] = None
    extracted_description: Optional[str] = None
    gpt_matched_name: Optional[str] = None
    gpt_match_rate: Optional[float] = None
    embedding_match_rate: Optional[float] = None
    name_match_rate: Optional[float] = None
    matched_characteristics: Optional[int] = None
    total_characteristics: Optional[int] = None
    characteristics_threshold: Optional[float] = None
    characteristics_details: Optional[List[CharacteristicMatchDetail]] = None
    tech_characteristics_matched: Optional[int] = None
    tech_characteristics_total: Optional[int] = None
    tech_characteristics_coverage: Optional[float] = None
    tech_requirements_matched: Optional[int] = None
    tech_requirements_total: Optional[int] = None
    tech_requirements_coverage: Optional[float] = None
    tech_accessories_matched: Optional[int] = None
    tech_accessories_total: Optional[int] = None
    tech_accessories_coverage: Optional[float] = None
    tech_description_score: Optional[float] = None
    tech_sheet_similarity_score: Optional[float] = None
    tech_sheet_similarity_status: Optional[str] = None
    tech_sheet_similarity_breakdown: Optional[dict] = None
    tech_sheet_similarity_display_score: Optional[float] = None
    tech_sheet_similarity_display_status: Optional[str] = None
    match_compensable: Optional[bool] = None
    match_compensation_mode: Optional[str] = None
    match_compensation_factor: Optional[float] = None
    match_compensation_basis: Optional[str] = None
    match_compensation_confidence: Optional[float] = None
    match_unit_missing: Optional[bool] = None
    match_unit_inferred: Optional[str] = None
    match_units_present_both: Optional[bool] = None
    match_unit_offered: Optional[str] = None
    match_unit_requested: Optional[str] = None
    match_unit_inferred_offered: Optional[str] = None
    match_unit_inferred_requested: Optional[str] = None
    match_unit_compare_mode: Optional[str] = None
    match_unit_compatible: Optional[bool] = None
    match_badges: Optional[List[str]] = None
    match_badge_score: Optional[float] = None
    match_badge_band: Optional[str] = None
    match_badge_breakdown: Optional[dict] = None
    strict_match_score: Optional[float] = None
    strict_match_threshold: Optional[float] = None
    strict_match_decision: Optional[str] = None
    strict_match_reason: Optional[str] = None
    strict_penalty_applied: Optional[bool] = None
    strict_penalty_value: Optional[float] = None
    strict_penalty_labels: Optional[List[str]] = None
    strict_family_match: Optional[bool] = None
    strict_family_reason: Optional[str] = None
    strict_spec_comparison: Optional[dict] = None
    strict_conversion_allowed: Optional[bool] = None
    strict_conversion_mode: Optional[str] = None
    strict_conversion_factor: Optional[float] = None
    strict_conversion_basis: Optional[str] = None
    strict_final_decision: Optional[str] = None
    strict_decision_reason: Optional[str] = None
    strict_alerts: Optional[List[str]] = None
    strict_alert_colors: Optional[dict] = None


class TechSheetExtractPayload(BaseModel):
    campaign_id: int
    sender_email: Optional[str] = None
    file_paths: List[str] = Field(default_factory=list)
    folder_path: Optional[str] = None


class RequestedCharacteristic(BaseModel):
    key: str
    value: str


class RequestedProduct(BaseModel):
    name: str
    characteristics: List[RequestedCharacteristic] = Field(default_factory=list)


class TechSheetComparePayload(BaseModel):
    campaign_id: int
    sender_email: Optional[str] = None
    requested: List[RequestedProduct] = Field(default_factory=list)
    mode: str = "both"


class TechSheetLinkPayload(BaseModel):
    campaign_id: int
    invoice_id: Optional[int] = None
    invoice_item_id: Optional[int] = None
    sender_email: Optional[str] = None
    min_score: float = 0.60
    max_text_chars: int = 2000


class TechSheetLinkOut(BaseModel):
    invoice_item_id: int
    product_name: str
    tech_sheet_id: Optional[int] = None
    tech_sheet_file: Optional[str] = None
    score: float = 0.0
    method: Optional[str] = None


class TechSheetOut(BaseModel):
    id: int
    campaign_id: int
    sender_email: Optional[str] = None
    file_name: str
    file_path: str
    parse_engine: Optional[str] = None
    parse_score: Optional[float] = None
    characteristics: Optional[dict] = None
    raw_text: Optional[str] = None
    created_at: datetime

    class Config:
        orm_mode = True


class ProductOffers(BaseModel):
    product_id: int
    product_name: str
    requested_qty: Optional[float] = None
    requested_source_attachment_id: Optional[int] = None
    requested_source_attachment_file: Optional[str] = None
    requested_source_attachment_path: Optional[str] = None
    requested_source_attachment_score: Optional[float] = None
    offers: List[OfferEntry] = Field(default_factory=list)
    best_offer: Optional[OfferEntry] = None


class ProductOffersPage(BaseModel):
    items: List[ProductOffers] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    has_more: bool = False
    cache_ready: bool = True


class SimpleProductOfferEntry(BaseModel):
    supplier_id: int = 0
    supplier_name: str
    supplier_email: Optional[str] = None
    supplier_phone: Optional[str] = None
    sender_email: Optional[str] = None
    invoice_item_id: Optional[int] = None
    invoice_id: Optional[int] = None
    invoice_file_name: Optional[str] = None
    invoice_file_path: Optional[str] = None
    source_engine: Optional[str] = None
    produit_demande: str
    produit_offert: str
    produit_offert_ocr_snippet: Optional[str] = None
    produit_offert_json: Optional[dict] = None
    prix_unitaire_facture: Optional[float] = None
    prix_equivalent: Optional[float] = None
    tva: Optional[float] = None
    quantite_demandee: Optional[float] = None
    quantite_offerte: Optional[float] = None
    conforme: bool = False
    score: float = 0.0
    similarite: Optional[str] = None
    difference: List[str] = Field(default_factory=list)
    raison: Optional[str] = None


class SimpleProductOffers(BaseModel):
    product_id: int
    product_name: str
    requested_qty: Optional[float] = None
    requested_ocr_snippet: Optional[str] = None
    requested_product_json: Optional[dict] = None
    offers: List[SimpleProductOfferEntry] = Field(default_factory=list)
    best_offer: Optional[SimpleProductOfferEntry] = None


class SimpleProductOffersPage(BaseModel):
    items: List[SimpleProductOffers] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    has_more: bool = False
    cache_ready: bool = True


class OfferSummaryEntry(BaseModel):
    product_id: int
    product_name: str
    best_price: Optional[float] = None
    best_supplier: Optional[str] = None
    offered_qty: Optional[float] = None
    offers_count: int = 0


class EmailTemplateBase(BaseModel):
    name: str
    subject: str
    body_html: str


class EmailTemplateCreate(EmailTemplateBase):
    pass


class EmailTemplateUpdate(BaseModel):
    name: Optional[str] = None
    subject: Optional[str] = None
    body_html: Optional[str] = None


class EmailTemplateOut(EmailTemplateBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class ImportError(BaseModel):
    row: int
    error: str


class ImportResult(BaseModel):
    inserted: int
    errors: List[ImportError] = Field(default_factory=list)


class TenantCreate(BaseModel):
    name: str


class TenantOut(BaseModel):
    id: int
    name: str
    created_at: datetime

    class Config:
        orm_mode = True


class TenantStats(BaseModel):
    tenant_id: int
    tenant_name: str
    users_count: int
    campaigns_sent_count: int
    responses_count: int


class AdminUserCreate(BaseModel):
    tenant_id: int
    email: EmailStr
    password: str
    is_admin: bool = False


class AdminUserOut(BaseModel):
    id: int
    tenant_id: int
    email: str
    is_admin: bool
    created_at: datetime

    class Config:
        orm_mode = True


class AdminUserStats(BaseModel):
    user_id: int
    tenant_id: int
    email: str
    is_admin: bool
    created_at: datetime
    campaigns_sent_count: int
    emails_sent_count: int
    ocr_pages_estimated: int = 0
    ocr_documents_count: int = 0
    invoice_ocr_documents_count: int = 0
    attachment_ocr_documents_count: int = 0
    tech_sheet_ocr_documents_count: int = 0
    attachment_reextract_count: int = 0
    embedding_items_count: int = 0


class SchedulerStatus(BaseModel):
    task_name: str
    enabled: bool
    status: Optional[str] = None
    last_run_time: Optional[str] = None
    next_run_time: Optional[str] = None


class GmailAccountOut(BaseModel):
    id: int
    google_email: str
    enabled: bool
    last_sync_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class GmailConnectUrlOut(BaseModel):
    auth_url: str
    redirect_uri: str

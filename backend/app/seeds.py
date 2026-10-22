from sqlalchemy.orm import Session

from app.auth import get_password_hash
from app.db import SessionLocal
from app.models import (
    Campaign,
    CampaignItem,
    CampaignStatus,
    EmailTemplate,
    Group,
    GroupMember,
    Product,
    Supplier,
    SupplierType,
    Tenant,
    User,
)
from app.settings_service import ensure_defaults


def seed_data() -> None:
    db: Session = SessionLocal()
    try:
        tenant = db.query(Tenant).first()
        if not tenant:
            tenant = Tenant(id=1, name="Default Tenant")
            db.add(tenant)
            db.commit()
        tenant_id = tenant.id

        admin = db.query(User).filter(User.email == "admin@local").first()
        if not admin:
            admin = User(
                email="admin@local",
                password_hash=get_password_hash("admin123"),
                tenant_id=tenant_id,
                is_admin=True,
            )
            db.add(admin)
        elif not admin.is_admin:
            admin.is_admin = True
        db.flush()
        admin_id = admin.id

        if db.query(EmailTemplate).filter(EmailTemplate.tenant_id == tenant_id).count() == 0:
            template = EmailTemplate(
                name="Default Request",
                subject="Request for quotation",
                body_html="<p>Hello {{supplier_name}},</p><p>Please share your best offer.</p>",
                tenant_id=tenant_id,
                owner_user_id=admin_id,
            )
            db.add(template)

        if db.query(Supplier).filter(Supplier.tenant_id == tenant_id).count() == 0:
            suppliers = [
                Supplier(
                    name="Pharma One",
                    email="pharma1@example.com",
                    supplier_type=SupplierType.MEDICAMENT,
                    tenant_id=tenant_id,
                    owner_user_id=admin_id,
                ),
                Supplier(
                    name="Med Equip",
                    email="equip@example.com",
                    supplier_type=SupplierType.MATERIEL,
                    tenant_id=tenant_id,
                    owner_user_id=admin_id,
                ),
                Supplier(
                    name="Health Store",
                    email="health@example.com",
                    supplier_type=SupplierType.MEDICAMENT,
                    tenant_id=tenant_id,
                    owner_user_id=admin_id,
                ),
                Supplier(
                    name="Clinic Supplies",
                    email="clinic@example.com",
                    supplier_type=SupplierType.MATERIEL,
                    tenant_id=tenant_id,
                    owner_user_id=admin_id,
                ),
            ]
            db.add_all(suppliers)

        if db.query(Group).filter(Group.tenant_id == tenant_id).count() == 0:
            group_a = Group(
                name="Group A",
                description="Main suppliers",
                tenant_id=tenant_id,
                owner_user_id=admin_id,
            )
            group_b = Group(
                name="Group B",
                description="Backup suppliers",
                tenant_id=tenant_id,
                owner_user_id=admin_id,
            )
            db.add_all([group_a, group_b])
            db.flush()

            suppliers = db.query(Supplier).filter(Supplier.tenant_id == tenant_id).all()
            if suppliers:
                db.add_all([
                    GroupMember(group_id=group_a.id, supplier_id=suppliers[0].id, tenant_id=tenant_id),
                    GroupMember(group_id=group_a.id, supplier_id=suppliers[1].id, tenant_id=tenant_id),
                    GroupMember(group_id=group_b.id, supplier_id=suppliers[2].id, tenant_id=tenant_id),
                    GroupMember(group_id=group_b.id, supplier_id=suppliers[3].id, tenant_id=tenant_id),
                ])

        if db.query(Product).filter(Product.tenant_id == tenant_id).count() == 0:
            products = [
                Product(
                    name="Aspirin",
                    sku="MED-001",
                    unit="",
                    tenant_id=tenant_id,
                    owner_user_id=admin_id,
                ),
                Product(
                    name="Gloves",
                    sku="MAT-010",
                    unit="",
                    tenant_id=tenant_id,
                    owner_user_id=admin_id,
                ),
                Product(
                    name="Syringe",
                    sku="MAT-011",
                    unit="unit",
                    tenant_id=tenant_id,
                    owner_user_id=admin_id,
                ),
                Product(
                    name="Vitamin C",
                    sku="MED-002",
                    unit="",
                    tenant_id=tenant_id,
                    owner_user_id=admin_id,
                ),
                Product(
                    name="Bandage",
                    sku="MAT-012",
                    unit="roll",
                    tenant_id=tenant_id,
                    owner_user_id=admin_id,
                ),
            ]
            db.add_all(products)

        if db.query(Campaign).filter(Campaign.tenant_id == tenant_id).count() == 0:
            campaign = Campaign(
                title="Sample Campaign",
                subject="Local Trial Request",
                message_html="<p>Please send your best offer.</p>",
                status=CampaignStatus.DRAFT,
                tenant_id=tenant_id,
                owner_user_id=admin_id,
            )
            db.add(campaign)
            db.flush()
            products = db.query(Product).filter(Product.tenant_id == tenant_id).all()
            for product in products[:3]:
                db.add(
                    CampaignItem(
                        campaign_id=campaign.id,
                        product_id=product.id,
                        requested_qty=100,
                        tenant_id=tenant_id,
                    )
                )

        db.commit()
        ensure_defaults(db, tenant_id)
    finally:
        db.close()

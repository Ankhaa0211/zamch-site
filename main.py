import os
import json
import uuid
import re
import hashlib
import secrets
import asyncio
import subprocess
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Depends, Form, File, UploadFile, Query, HTTPException, Request, Response, Header
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import or_, text
from sqlmodel import SQLModel, Field, create_engine, Session, select

import qpay_client
import delivery_providers
import moderation
import sms_client
from db_url import normalize_database_url

# --- Database ---
DATABASE_URL = normalize_database_url(os.environ.get("DATABASE_URL", "sqlite:///./database.db"))
IS_SQLITE = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
    pool_pre_ping=not IS_SQLITE,
)

SESSION_SECRET = os.environ.get("SESSION_SECRET", "zamch-dev-secret-change-me")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").strip().lower()
AUTO_CREATE_SCHEMA = os.environ.get(
    "AUTO_CREATE_SCHEMA", "0" if ENVIRONMENT == "production" else "1"
) == "1"
TOKEN_TTL_DAYS = max(1, int(os.environ.get("TOKEN_TTL_DAYS", "30")))
ORDER_CONFIRM_MINUTES = max(5, int(os.environ.get("ORDER_CONFIRM_MINUTES", "30")))
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
ADMIN_PHONE = os.environ.get("ADMIN_PHONE", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
ADMIN_NAME = os.environ.get("ADMIN_NAME", "Админ").strip()

# --- Models ---
class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    phone: str = Field(index=True, unique=True)
    password_hash: str
    role: str = Field(default="buyer")  # buyer | seller | admin
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class Store(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: int = Field(index=True)
    name: str
    description: Optional[str] = None
    phone: str
    location: str = Field(default="Улаанбаатар")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    phone_verified: bool = Field(default=False)
    logo: Optional[str] = None
    is_active: bool = Field(default=True)
    is_approved: bool = Field(default=False)  # admin баталгаажуулалт
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class PhoneOtp(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    code_hash: str
    purpose: str = Field(default="store_phone", index=True)
    expires_at: str
    attempts: int = Field(default=0)
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


class Category(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(index=True, unique=True)
    icon: Optional[str] = None


class Product(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    store_id: int = Field(index=True)
    category_id: int = Field(index=True)
    title: str
    brand: Optional[str] = None
    condition: str = Field(default="Шинэ")
    pack_type: Optional[str] = None  # 4 хос, 2 хос, 1 ширхэг
    price: float
    stock: int = Field(default=1)
    description: Optional[str] = None
    images: str = Field(default="[]")
    video: Optional[str] = None
    # Tire / rim optional attrs
    width: Optional[int] = None
    ratio: Optional[int] = None
    diameter: Optional[int] = None
    tread_percent: Optional[int] = None
    bolt_pattern: Optional[str] = None
    wheel_type: Optional[str] = None
    car_make: Optional[str] = Field(default=None, index=True)  # машины марка
    listing_kind: Optional[str] = Field(default=None, index=True)  # dugui | obud | combo
    sku: Optional[str] = Field(default=None, index=True)
    barcode: Optional[str] = Field(default=None, index=True)
    publish_status: str = Field(default="draft", index=True)
    moderation_flags: str = Field(default="[]")
    rejection_reason: Optional[str] = None
    submitted_at: Optional[str] = None
    published_at: Optional[str] = None
    is_active: bool = Field(default=True)
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class ApiToken(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    token_hash: str = Field(index=True, unique=True)
    device_name: Optional[str] = None
    revoked_at: Optional[str] = None
    expires_at: Optional[str] = Field(default=None, index=True)
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class DeviceToken(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    expo_push_token: str = Field(index=True, unique=True)
    platform: Optional[str] = None
    device_id: Optional[str] = Field(default=None, index=True)
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class Warehouse(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    store_id: int = Field(index=True)
    name: str
    address: Optional[str] = None
    is_default: bool = Field(default=False)
    is_active: bool = Field(default=True)
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class InventoryBalance(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    warehouse_id: int = Field(index=True)
    product_id: int = Field(index=True)
    quantity: int = Field(default=0)
    low_stock_threshold: int = Field(default=0)
    updated_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class StockMovement(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    store_id: int = Field(index=True)
    warehouse_id: int = Field(index=True)
    product_id: int = Field(index=True)
    movement_type: str = Field(index=True)
    quantity_delta: int
    reason: Optional[str] = None
    reference_type: Optional[str] = None
    reference_id: Optional[int] = None
    actor_user_id: Optional[int] = None
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class ModerationJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)
    version: int = Field(default=1)
    status: str = Field(default="queued", index=True)
    decision: Optional[str] = None
    flags: str = Field(default="[]")
    last_error: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    finished_at: Optional[str] = None


class ModerationEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)
    actor_type: str = Field(default="system")
    actor_id: Optional[int] = None
    action: str
    from_status: Optional[str] = None
    to_status: str
    flags: str = Field(default="[]")
    note: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class CartItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, index=True)
    guest_token: Optional[str] = Field(default=None, index=True)
    product_id: int = Field(index=True)
    quantity: int = Field(default=1)


class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    store_id: int = Field(index=True)
    user_id: Optional[int] = Field(default=None, index=True)
    customer_name: str
    customer_phone: str
    delivery_type: str = Field(default="delivery")  # delivery | pickup
    payment_method: str = Field(default="cod")  # cod | bank_transfer | qpay
    payment_status: str = Field(default="unpaid")  # unpaid | paid
    address: Optional[str] = None
    note: Optional[str] = None
    status: str = Field(default="pending")  # pending|confirmed|preparing|out_for_delivery|completed|cancelled
    inventory_status: str = Field(default="reserved", index=True)  # reserved|committed|released
    confirmation_expires_at: Optional[str] = Field(default=None, index=True)
    confirmed_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    cancellation_reason: Optional[str] = None
    total: float = Field(default=0)
    payment_group_id: Optional[str] = Field(default=None, index=True)
    qpay_invoice_id: Optional[str] = Field(default=None, index=True)
    qpay_payment_id: Optional[str] = Field(default=None, index=True)
    # eBarimt 3.0
    ebarimt_receiver_type: Optional[str] = Field(default="CITIZEN")  # CITIZEN | COMPANY
    ebarimt_receiver: Optional[str] = None  # company TIN / optional citizen phone
    ebarimt_district_code: Optional[str] = None
    ebarimt_status: Optional[str] = Field(default=None)  # none|created|failed|cancelled|skipped
    ebarimt_lottery: Optional[str] = None
    ebarimt_qr: Optional[str] = None
    ebarimt_payload: Optional[str] = None  # JSON raw response
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


class OrderItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(index=True)
    product_id: int
    title: str
    price: float
    quantity: int


class DeliveryShipment(SQLModel, table=True):
    """Marketplace ↔ delivery provider handoff (own_app / partner / manual)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(index=True, unique=True)
    store_id: int = Field(index=True)
    provider: str = Field(default="manual", index=True)  # manual | own_app | partner
    external_ref: str = Field(default_factory=lambda: str(uuid.uuid4()), index=True)
    provider_job_id: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="pending")  # pending|queued|assigned|picked_up|in_transit|delivered|cancelled|failed
    pickup_name: Optional[str] = None
    pickup_phone: Optional[str] = None
    pickup_address: Optional[str] = None
    pickup_location: Optional[str] = None
    customer_name: str
    customer_phone: str
    dropoff_address: Optional[str] = None
    note: Optional[str] = None
    items_summary: Optional[str] = None
    order_total: float = Field(default=0)
    cod_amount: float = Field(default=0)  # unpaid COD still to collect
    provider_payload: Optional[str] = None  # JSON
    provider_response: Optional[str] = None  # JSON
    last_event_at: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


# v1: зөвхөн дугуй + обуд. Бусад ангиллыг дараа нэмнэ.
DEFAULT_CATEGORIES = [
    {"name": "Дугуй", "slug": "dugui", "icon": "bi-circle"},
    {"name": "Обуд", "slug": "obud", "icon": "bi-disc"},
]
PUBLIC_CATEGORY_SLUGS = {c["slug"] for c in DEFAULT_CATEGORIES}
LISTING_KINDS = {"dugui", "obud", "combo"}


def _normalize_listing_kind(
    listing_kind: Optional[str], category_slug: Optional[str]
) -> str:
    kind = (listing_kind or "").strip().lower()
    if kind in LISTING_KINDS:
        return kind
    if category_slug == "obud":
        return "obud"
    return "dugui"


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    _migrate_store_admin_columns()
    if IS_SQLITE:
        _migrate_order_payment_columns()
        _migrate_product_seller_columns()
        _migrate_api_token_columns()


def _store_existing_columns(conn) -> set[str]:
    if IS_SQLITE:
        rows = conn.execute(text('PRAGMA table_info("store")')).fetchall()
        return {r[1] for r in rows}
    rows = conn.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'store'
            """
        )
    ).fetchall()
    return {r[0] for r in rows}


def _migrate_store_admin_columns():
    cols = [
        ("is_approved", "BOOLEAN DEFAULT FALSE"),
        ("latitude", "DOUBLE PRECISION"),
        ("longitude", "DOUBLE PRECISION"),
        ("phone_verified", "BOOLEAN DEFAULT FALSE"),
    ]
    with engine.connect() as conn:
        existing = _store_existing_columns(conn)
        if not existing:
            return
        if "is_approved" not in existing:
            conn.execute(text('ALTER TABLE "store" ADD COLUMN is_approved BOOLEAN DEFAULT TRUE'))
            conn.execute(text('UPDATE "store" SET is_approved = TRUE WHERE is_approved IS NULL'))
        for name, coltype in cols:
            if name == "is_approved":
                continue
            if name not in existing:
                conn.execute(text(f'ALTER TABLE "store" ADD COLUMN {name} {coltype}'))
        conn.commit()

def _migrate_api_token_columns():
    with engine.connect() as conn:
        rows = conn.execute(text('PRAGMA table_info("apitoken")')).fetchall()
        if rows and "expires_at" not in {row[1] for row in rows}:
            conn.execute(text('ALTER TABLE "apitoken" ADD COLUMN expires_at VARCHAR'))
            conn.commit()


def _migrate_product_seller_columns():
    cols = [
        ("sku", "VARCHAR"),
        ("barcode", "VARCHAR"),
        ("publish_status", "VARCHAR DEFAULT 'published'"),
        ("moderation_flags", "TEXT DEFAULT '[]'"),
        ("rejection_reason", "TEXT"),
        ("submitted_at", "VARCHAR"),
        ("published_at", "VARCHAR"),
        ("video", "VARCHAR"),
        ("car_make", "VARCHAR"),
        ("listing_kind", "VARCHAR"),
    ]
    with engine.connect() as conn:
        rows = conn.execute(text('PRAGMA table_info("product")')).fetchall()
        if not rows:
            return
        existing = {r[1] for r in rows}
        for name, coltype in cols:
            if name not in existing:
                conn.execute(text(f'ALTER TABLE "product" ADD COLUMN {name} {coltype}'))
        conn.execute(
            text("UPDATE product SET publish_status = 'published' WHERE publish_status IS NULL")
        )
        conn.execute(
            text("UPDATE product SET moderation_flags = '[]' WHERE moderation_flags IS NULL")
        )
        # Backfill listing_kind from category slug when empty
        conn.execute(
            text(
                """
                UPDATE product
                SET listing_kind = (
                    SELECT CASE
                        WHEN category.slug = 'obud' THEN 'obud'
                        ELSE 'dugui'
                    END
                    FROM category
                    WHERE category.id = product.category_id
                )
                WHERE listing_kind IS NULL OR listing_kind = ''
                """
            )
        )
        conn.commit()


def _migrate_order_payment_columns():
    """SQLite create_all does not ALTER existing tables — add QPay/eBarimt columns if missing."""
    cols = [
        ("payment_group_id", "VARCHAR"),
        ("qpay_invoice_id", "VARCHAR"),
        ("qpay_payment_id", "VARCHAR"),
        ("ebarimt_receiver_type", "VARCHAR"),
        ("ebarimt_receiver", "VARCHAR"),
        ("ebarimt_district_code", "VARCHAR"),
        ("ebarimt_status", "VARCHAR"),
        ("ebarimt_lottery", "VARCHAR"),
        ("ebarimt_qr", "VARCHAR"),
        ("ebarimt_payload", "TEXT"),
        ("inventory_status", "VARCHAR DEFAULT 'reserved'"),
        ("confirmation_expires_at", "VARCHAR"),
        ("confirmed_at", "VARCHAR"),
        ("cancelled_at", "VARCHAR"),
        ("cancellation_reason", "TEXT"),
    ]
    with engine.connect() as conn:
        rows = conn.execute(text('PRAGMA table_info("order")')).fetchall()
        if not rows:
            return
        existing = {r[1] for r in rows}
        for name, coltype in cols:
            if name not in existing:
                conn.execute(text(f'ALTER TABLE "order" ADD COLUMN {name} {coltype}'))
        conn.execute(
            text(
                "UPDATE \"order\" SET inventory_status = "
                "CASE WHEN status = 'cancelled' THEN 'released' "
                "WHEN status IN ('confirmed','preparing','out_for_delivery','completed') THEN 'committed' "
                "ELSE 'reserved' END "
                "WHERE inventory_status IS NULL OR inventory_status = ''"
            )
        )
        conn.commit()


def seed_categories():
    with Session(engine) as session:
        by_slug = {c.slug: c for c in session.exec(select(Category)).all()}
        changed = False
        for item in DEFAULT_CATEGORIES:
            cat = by_slug.get(item["slug"])
            if not cat:
                session.add(Category(**item))
                changed = True
            elif cat.name != item["name"] or (cat.icon or "") != item["icon"]:
                cat.name = item["name"]
                cat.icon = item["icon"]
                session.add(cat)
                changed = True
        # Remove unused v1 categories only when no products reference them
        for slug, cat in list(by_slug.items()):
            if slug in PUBLIC_CATEGORY_SLUGS:
                continue
            has_products = session.exec(
                select(Product).where(Product.category_id == cat.id)
            ).first()
            if not has_products:
                session.delete(cat)
                changed = True
        if changed:
            session.commit()


def normalize_obud_spelling():
    """Rewrite legacy 'обод' spelling to 'обуд' in stored product fields."""
    with Session(engine) as session:
        products = session.exec(select(Product)).all()
        changed = False
        for p in products:
            row_changed = False
            for field in ("title", "wheel_type", "brand"):
                val = getattr(p, field, None)
                if not val:
                    continue
                new_val = (
                    val.replace("Ободны", "Обудны")
                    .replace("ободны", "обудны")
                    .replace("Обод", "Обуд")
                    .replace("обод", "обуд")
                    .replace("ОБОД", "ОБУД")
                )
                if new_val != val:
                    setattr(p, field, new_val)
                    row_changed = True
            if row_changed:
                session.add(p)
                changed = True
        if changed:
            session.commit()


def bootstrap_admin():
    """Create/promote admin from ADMIN_PHONE + ADMIN_PASSWORD env."""
    if not ADMIN_PHONE or not ADMIN_PASSWORD:
        return
    if not validate_phone(ADMIN_PHONE):
        return
    with Session(engine) as session:
        user = session.exec(select(User).where(User.phone == ADMIN_PHONE)).first()
        if user:
            if user.role != "admin":
                user.role = "admin"
                session.add(user)
                session.commit()
            return
        session.add(
            User(
                name=ADMIN_NAME or "Админ",
                phone=ADMIN_PHONE,
                password_hash=hash_password(ADMIN_PASSWORD),
                role="admin",
            )
        )
        session.commit()


def get_session():
    with Session(engine) as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    if ENVIRONMENT == "production" and SESSION_SECRET == "zamch-dev-secret-change-me":
        raise RuntimeError("Production дээр SESSION_SECRET заавал тохируулна")
    if ENVIRONMENT == "production" and "*" in CORS_ORIGINS:
        raise RuntimeError("Production дээр CORS_ORIGINS-ийг тодорхой тохируулна")
    if AUTO_CREATE_SCHEMA:
        create_db_and_tables()
    else:
        # Postgres production: keep critical store columns in sync even if a migration lags.
        _migrate_store_admin_columns()
    seed_categories()
    normalize_obud_spelling()
    bootstrap_admin()
    expiry_task = asyncio.create_task(_reservation_expiry_loop())
    try:
        yield
    finally:
        expiry_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="ЗАМЧ Marketplace", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("photos", exist_ok=True)
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app.mount("/photos", StaticFiles(directory="photos"), name="photos")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_FILES = 5
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
MAX_VIDEO_SIZE = 50 * 1024 * 1024


# --- Helpers ---
def validate_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"^[6-9]\d{7}$", phone.strip()))


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, stored = password_hash.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000)
        return secrets.compare_digest(digest.hex(), stored)
    except Exception:
        return False


def parse_images(raw: str) -> List[str]:
    try:
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return [raw] if raw else []


def product_to_dict(product: Product, store: Optional[Store] = None, category: Optional[Category] = None) -> Dict[str, Any]:
    data = product.model_dump() if hasattr(product, "model_dump") else product.dict()
    data["images"] = parse_images(product.images)
    if store:
        data["store_name"] = store.name
        data["store_location"] = store.location
    if category:
        data["category_name"] = category.name
        data["category_slug"] = category.slug
    return data


def get_current_user(request: Request, session: Session) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return session.get(User, user_id)


def require_user(request: Request, session: Session) -> User:
    user = get_current_user(request, session)
    if not user:
        raise HTTPException(status_code=401, detail="Нэвтэрнэ үү")
    return user


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_api_token(session: Session, user: User, device_name: Optional[str] = None) -> str:
    raw = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(days=TOKEN_TTL_DAYS)).isoformat(timespec="seconds")
    session.add(
        ApiToken(
            user_id=user.id,
            token_hash=_token_hash(raw),
            device_name=device_name,
            expires_at=expires_at,
        )
    )
    session.commit()
    return raw


def require_api_user(authorization: Optional[str], session: Session) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token шаардлагатай")
    raw = authorization.split(" ", 1)[1].strip()
    token = session.exec(
        select(ApiToken).where(
            ApiToken.token_hash == _token_hash(raw),
            ApiToken.revoked_at == None,
        )
    ).first()
    if token and token.expires_at:
        try:
            if datetime.fromisoformat(token.expires_at) <= datetime.now():
                token.revoked_at = datetime.now().isoformat(timespec="seconds")
                session.add(token)
                session.commit()
                token = None
        except ValueError:
            token = None
    user = session.get(User, token.user_id) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="Token хүчингүй эсвэл хугацаа дууссан")
    return user


def require_mobile_seller(
    authorization: Optional[str], session: Session
) -> tuple[User, Store]:
    user = require_api_user(authorization, session)
    if user.role not in ("seller", "admin"):
        raise HTTPException(status_code=403, detail="Худалдагчийн эрх шаардлагатай")
    store = get_user_store(session, user.id)
    if not store:
        raise HTTPException(status_code=400, detail="Дэлгүүрийн бүртгэл дутуу байна")
    return user, store


def require_admin(request: Request, session: Session) -> User:
    user = require_user(request, session)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Зөвхөн админ хандах боломжтой")
    return user


def ensure_guest_token(request: Request, response: Optional[Response] = None) -> str:
    token = request.session.get("guest_token")
    if not token:
        token = str(uuid.uuid4())
        request.session["guest_token"] = token
    return token


def cart_owner_filter(request: Request, session: Session):
    user = get_current_user(request, session)
    if user:
        return CartItem.user_id == user.id, user, None
    token = ensure_guest_token(request)
    return CartItem.guest_token == token, None, token


def _merge_guest_cart(request: Request, session: Session, user: User) -> None:
    token = request.session.get("guest_token")
    if not token:
        return
    guest_items = session.exec(select(CartItem).where(CartItem.guest_token == token)).all()
    for item in guest_items:
        existing = session.exec(
            select(CartItem).where(
                CartItem.user_id == user.id,
                CartItem.product_id == item.product_id,
            )
        ).first()
        if existing:
            existing.quantity += item.quantity
            session.add(existing)
            session.delete(item)
        else:
            item.user_id = user.id
            item.guest_token = None
            session.add(item)
    if guest_items:
        session.commit()


def _claim_guest_orders(session: Session, user: User) -> None:
    phone = (user.phone or "").strip()
    if not phone:
        return
    orders = session.exec(
        select(Order).where(Order.user_id == None, Order.customer_phone == phone)
    ).all()
    for order in orders:
        order.user_id = user.id
        session.add(order)
    if orders:
        session.commit()


def _attach_buyer_session(request: Request, session: Session, user: User) -> None:
    request.session["user_id"] = user.id
    _merge_guest_cart(request, session, user)
    _claim_guest_orders(session, user)


def _restore_inventory_for_order(session: Session, order: Order) -> None:
    """Release a reservation exactly once, restoring its original warehouses."""
    if order.inventory_status == "released":
        return
    items = session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()
    for item in items:
        product = session.get(Product, item.product_id)
        if not product:
            continue
        sale_movements = session.exec(
            select(StockMovement).where(
                StockMovement.product_id == product.id,
                StockMovement.reference_type == "order",
                StockMovement.reference_id == order.id,
                StockMovement.movement_type == "sale",
            )
        ).all()
        if sale_movements:
            for movement in sale_movements:
                restored = abs(int(movement.quantity_delta or 0))
                balance = session.exec(
                    select(InventoryBalance).where(
                        InventoryBalance.product_id == product.id,
                        InventoryBalance.warehouse_id == movement.warehouse_id,
                    )
                ).first()
                if not balance:
                    balance = InventoryBalance(
                        product_id=product.id,
                        warehouse_id=movement.warehouse_id,
                        quantity=0,
                    )
                balance.quantity += restored
                balance.updated_at = datetime.now().isoformat(timespec="seconds")
                session.add(balance)
                session.add(
                    StockMovement(
                        store_id=product.store_id,
                        warehouse_id=movement.warehouse_id,
                        product_id=product.id,
                        movement_type="return",
                        quantity_delta=restored,
                        reason="Захиалга цуцлагдсан",
                        reference_type="order_release",
                        reference_id=order.id,
                    )
                )
            _sync_product_stock(session, product.id)
        else:
            product.stock = int(product.stock or 0) + int(item.quantity or 0)
            session.add(product)
    order.inventory_status = "released"
    session.add(order)


def _confirm_order_reservation(order: Order) -> None:
    if order.inventory_status == "reserved":
        order.inventory_status = "committed"
    if not order.confirmed_at:
        order.confirmed_at = datetime.now().isoformat(timespec="seconds")


def _expire_pending_orders(session: Session) -> int:
    now = datetime.now()
    expired = 0
    candidates = session.exec(
        select(Order).where(
            Order.status == "pending",
            Order.inventory_status == "reserved",
            Order.confirmation_expires_at != None,
        )
    ).all()
    for order in candidates:
        try:
            deadline = datetime.fromisoformat(order.confirmation_expires_at or "")
        except ValueError:
            continue
        if deadline > now:
            continue
        _restore_inventory_for_order(session, order)
        order.status = "cancelled"
        order.cancelled_at = now.isoformat(timespec="seconds")
        order.cancellation_reason = "seller_confirmation_timeout"
        session.add(order)
        expired += 1
    if expired:
        session.commit()
    return expired


async def _reservation_expiry_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            with Session(engine) as session:
                _expire_pending_orders(session)
        except Exception:
            # The next cycle retries; request handling must not depend on the worker.
            pass


def _delete_orders_cascade(session: Session, order_ids: List[int]) -> None:
    for order_id in order_ids:
        items = session.exec(select(OrderItem).where(OrderItem.order_id == order_id)).all()
        for item in items:
            session.delete(item)
        order = session.get(Order, order_id)
        if order:
            session.delete(order)
    session.commit()


async def save_uploads(files: Optional[List[UploadFile]]) -> List[str]:
    saved = []
    if not files:
        return saved
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Хамгийн ихдээ {MAX_FILES} зураг")
    for file in files:
        if not file or not file.filename:
            continue
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Дэмжигдэхгүй формат: {ext}")
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="Зургийн хэмжээ 5MB-аас хэтэрсэн")
        name = f"{uuid.uuid4()}{ext}"
        path = os.path.join("photos", name)
        with open(path, "wb") as f:
            f.write(content)
        saved.append(f"/photos/{name}")
    return saved


async def save_silent_video(video: Optional[UploadFile]) -> Optional[str]:
    if not video or not video.filename:
        return None
    ext = os.path.splitext(video.filename)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Дэмжигдэхгүй бичлэгийн формат: {ext}")
    content = await video.read()
    if len(content) > MAX_VIDEO_SIZE:
        raise HTTPException(status_code=400, detail="Бичлэгийн хэмжээ 50MB-аас хэтэрсэн")

    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="Бичлэг боловсруулах service бэлэн биш байна") from exc

    upload_id = str(uuid.uuid4())
    input_path = os.path.join("photos", f".video-upload-{upload_id}{ext}")
    output_name = f"{upload_id}.mp4"
    output_path = os.path.join("photos", output_name)
    with open(input_path, "wb") as file:
        file.write(content)

    command = [
        ffmpeg,
        "-y",
        "-i", input_path,
        "-map", "0:v:0",
        "-t", "30",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "24",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-map_metadata", "-1",
        "-an",
        output_path,
    ]
    try:
        await asyncio.to_thread(
            subprocess.run,
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise HTTPException(status_code=400, detail="Бичлэгийг боловсруулж чадсангүй") from exc
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)
    return f"/photos/{output_name}"


def get_user_store(session: Session, user_id: int) -> Optional[Store]:
    return session.exec(select(Store).where(Store.owner_id == user_id)).first()


# --- Pages ---
@app.get("/")
def page_home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/shop")
def page_shop(request: Request):
    # Home is the primary browse/search surface; keep store deep-links working.
    store_id = request.query_params.get("store_id")
    if store_id:
        return RedirectResponse(url=f"/stores/{store_id}", status_code=302)
    intent = (request.query_params.get("intent") or "").strip().lower()
    if intent in LISTING_KINDS:
        return RedirectResponse(url=f"/?intent={intent}", status_code=302)
    return RedirectResponse(url="/", status_code=302)


@app.get("/stores")
def page_stores(request: Request):
    return templates.TemplateResponse("stores.html", {"request": request})


@app.get("/stores/{store_id}")
def page_store(request: Request, store_id: int):
    return templates.TemplateResponse(
        "store.html", {"request": request, "store_id": store_id}
    )


@app.get("/dugui")
def page_dugui(request: Request):
    return RedirectResponse(url="/?intent=dugui", status_code=302)


@app.get("/obud")
def page_obud(request: Request):
    return RedirectResponse(url="/?intent=obud", status_code=302)


@app.get("/product/{product_id}")
def page_product(request: Request, product_id: int):
    return templates.TemplateResponse("product.html", {"request": request, "product_id": product_id})


@app.get("/cart")
def page_cart(request: Request):
    return templates.TemplateResponse("cart.html", {"request": request})


@app.get("/order-success")
def page_order_success(request: Request):
    return templates.TemplateResponse("order-success.html", {"request": request})


@app.get("/my-orders")
def page_my_orders(request: Request):
    return templates.TemplateResponse("my-orders.html", {"request": request})


@app.get("/login")
def page_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/register")
def page_register(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.get("/forgot-password")
def page_forgot_password(request: Request):
    return templates.TemplateResponse("forgot-password.html", {"request": request})


@app.get("/privacy")
def page_privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/terms")
def page_terms(request: Request):
    return templates.TemplateResponse("terms.html", {"request": request})


@app.get("/for-sellers")
def page_for_sellers(request: Request):
    return templates.TemplateResponse("for-sellers.html", {"request": request})


@app.get("/how-it-works")
def page_how_it_works(request: Request):
    return templates.TemplateResponse("how-it-works.html", {"request": request})


@app.get("/seller")
@app.get("/seller/products/new")
@app.get("/seller/orders")
def page_seller_redirect():
    """Store management moved to ЗАМЧ Seller app — web shows info only."""
    return Response(status_code=302, headers={"Location": "/for-sellers"})


@app.get("/admin")
def page_admin(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})


# --- Auth API ---
@app.post("/api/auth/register")
def api_register(
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    if not validate_phone(phone):
        raise HTTPException(status_code=400, detail="Утасны дугаар буруу (жишээ: 99112233)")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Нууц үг дор хаяж 6 тэмдэгт байх ёстой")
    existing = session.exec(select(User).where(User.phone == phone.strip())).first()
    if existing:
        raise HTTPException(status_code=400, detail="Энэ утасны дугаар бүртгэлтэй байна")

    user = User(
        name=name.strip(),
        phone=phone.strip(),
        password_hash=hash_password(password),
        role="buyer",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    _attach_buyer_session(request, session, user)
    return {"id": user.id, "name": user.name, "phone": user.phone, "role": user.role}


@app.post("/api/auth/login")
def api_login(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    user = session.exec(select(User).where(User.phone == phone.strip())).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=400, detail="Утас эсвэл нууц үг буруу")
    _attach_buyer_session(request, session, user)
    return {"id": user.id, "name": user.name, "phone": user.phone, "role": user.role}


@app.post("/api/auth/logout")
def api_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.post("/api/auth/reset-password")
def api_reset_password(
    phone: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    """Reset password by verifying phone + registered name (no SMS yet)."""
    phone = phone.strip()
    name = name.strip()
    if not validate_phone(phone):
        raise HTTPException(status_code=400, detail="Утасны дугаар буруу (жишээ: 99112233)")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Нууц үг дор хаяж 6 тэмдэгт байх ёстой")
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Нэрээ оруулна уу")

    user = session.exec(select(User).where(User.phone == phone)).first()
    # Same message either way — avoid phone enumeration
    if not user or user.name.strip().casefold() != name.casefold():
        raise HTTPException(status_code=400, detail="Утас эсвэл нэр таарахгүй байна")

    user.password_hash = hash_password(password)
    session.add(user)
    session.commit()
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(request: Request, session: Session = Depends(get_session)):
    user = get_current_user(request, session)
    if not user:
        return {"user": None}
    store = get_user_store(session, user.id)
    return {
        "user": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "role": user.role,
            "is_admin": user.role == "admin",
            "store_id": store.id if store else None,
            "store_name": store.name if store else None,
            "store_approved": store.is_approved if store else None,
        }
    }


# --- Seller mobile API ---
@app.post("/api/mobile/auth/register")
def api_mobile_register(
    name: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    store_name: Optional[str] = Form(None),
    store_phone: Optional[str] = Form(None),
    location: str = Form("Улаанбаатар"),
    device_name: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    phone = phone.strip()
    person_name = name.strip()
    if not validate_phone(phone):
        raise HTTPException(status_code=400, detail="Утасны дугаар буруу")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Нууц үг дор хаяж 6 тэмдэгт байна")
    if len(person_name) < 2:
        raise HTTPException(status_code=400, detail="Нэрээ оруулна уу")
    if session.exec(select(User).where(User.phone == phone)).first():
        raise HTTPException(status_code=409, detail="Энэ утасны дугаар бүртгэлтэй байна")

    # Many sellers are langar/container operators without a formal shop name.
    resolved_store_name = (store_name or "").strip() or f"{person_name}-ийн лангуу"

    user = User(
        name=person_name,
        phone=phone,
        password_hash=hash_password(password),
        role="seller",
    )
    session.add(user)
    session.flush()
    store = Store(
        owner_id=user.id,
        name=resolved_store_name,
        phone=(store_phone or phone).strip(),
        location=location.strip() or "Улаанбаатар",
        is_active=True,
        is_approved=False,
    )
    session.add(store)
    session.flush()
    session.add(
        Warehouse(
            store_id=store.id,
            name="Лангуу",
            address=store.location,
            is_default=True,
        )
    )
    session.commit()
    token = issue_api_token(session, user, device_name)
    return {
        "token": token,
        "user": {"id": user.id, "name": user.name, "phone": user.phone, "role": user.role},
        "store": _store_mobile_payload(store),
    }


@app.post("/api/mobile/auth/login")
def api_mobile_login(
    phone: str = Form(...),
    password: str = Form(...),
    device_name: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    user = session.exec(select(User).where(User.phone == phone.strip())).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=400, detail="Утас эсвэл нууц үг буруу")
    if user.role not in ("seller", "admin"):
        raise HTTPException(status_code=403, detail="Seller апп ашиглах эрхгүй байна")
    token = issue_api_token(session, user, device_name)
    store = get_user_store(session, user.id)
    return {
        "token": token,
        "user": {"id": user.id, "name": user.name, "phone": user.phone, "role": user.role},
        "store": _store_mobile_payload(store) if store else None,
    }


@app.post("/api/mobile/auth/logout")
def api_mobile_logout(
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    require_api_user(authorization, session)
    raw = authorization.split(" ", 1)[1].strip()
    token = session.exec(
        select(ApiToken).where(ApiToken.token_hash == _token_hash(raw))
    ).first()
    if token:
        token.revoked_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        session.add(token)
        session.commit()
    return {"ok": True}


def _store_mobile_payload(store: Store) -> Dict[str, Any]:
    has_location = store.latitude is not None and store.longitude is not None
    return {
        "id": store.id,
        "name": store.name,
        "description": store.description,
        "phone": store.phone,
        "location": store.location,
        "latitude": store.latitude,
        "longitude": store.longitude,
        "phone_verified": bool(store.phone_verified),
        "location_verified": has_location,
        "store_verified": bool(store.phone_verified) and has_location,
        "logo": store.logo,
        "is_active": store.is_active,
        "is_approved": store.is_approved,
    }


def _otp_code_hash(phone: str, code: str) -> str:
    raw = f"{phone.strip()}:{code.strip()}:{SESSION_SECRET}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _create_phone_otp(session: Session, phone: str, purpose: str = "store_phone") -> tuple[str, PhoneOtp]:
    phone = phone.strip()
    now = datetime.now()
    recent = session.exec(
        select(PhoneOtp)
        .where(PhoneOtp.phone == phone, PhoneOtp.purpose == purpose)
        .order_by(PhoneOtp.id.desc())
    ).first()
    if recent:
        try:
            created = datetime.strptime(recent.created_at, "%Y-%m-%d %H:%M:%S")
            if now - created < timedelta(seconds=45):
                raise HTTPException(status_code=429, detail="Код дахин илгээхийн өмнө түр хүлээнэ үү")
        except ValueError:
            pass

    code = f"{secrets.randbelow(1_000_000):06d}"
    row = PhoneOtp(
        phone=phone,
        code_hash=_otp_code_hash(phone, code),
        purpose=purpose,
        expires_at=(now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
        attempts=0,
        created_at=now.strftime("%Y-%m-%d %H:%M:%S"),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return code, row


@app.get("/api/mobile/me")
def api_mobile_me(
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    user = require_api_user(authorization, session)
    store = get_user_store(session, user.id)
    return {
        "user": {"id": user.id, "name": user.name, "phone": user.phone, "role": user.role},
        "store": _store_mobile_payload(store) if store else None,
    }


@app.patch("/api/mobile/store")
async def api_mobile_update_store(
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    latitude: Optional[str] = Form(None),
    longitude: Optional[str] = Form(None),
    logo: Optional[UploadFile] = File(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    if name is not None and name.strip():
        store.name = name.strip()
    if description is not None:
        store.description = description.strip()
    if phone is not None:
        if not validate_phone(phone):
            raise HTTPException(status_code=400, detail="Утасны дугаар буруу")
        new_phone = phone.strip()
        if new_phone != store.phone:
            store.phone = new_phone
            store.phone_verified = False
    if location is not None and location.strip():
        store.location = location.strip()
    if latitude is not None and longitude is not None:
        try:
            lat = float(str(latitude).strip())
            lng = float(str(longitude).strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Байршлын координат буруу") from exc
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise HTTPException(status_code=400, detail="Байршлын координат буруу")
        store.latitude = lat
        store.longitude = lng
    if logo and logo.filename:
        saved = await save_uploads([logo])
        if saved:
            store.logo = saved[0]
    # Keep default warehouse address in sync with map/location text.
    if location is not None or (latitude is not None and longitude is not None):
        warehouse = session.exec(
            select(Warehouse).where(Warehouse.store_id == store.id, Warehouse.is_default == True)
        ).first()
        if warehouse:
            warehouse.address = store.location
            session.add(warehouse)
    session.add(store)
    session.commit()
    session.refresh(store)
    return {"store": _store_mobile_payload(store)}


@app.post("/api/mobile/otp/send")
def api_mobile_otp_send(
    phone: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    target = (phone or store.phone or "").strip()
    if not validate_phone(target):
        raise HTTPException(status_code=400, detail="Утасны дугаар буруу")
    code, _ = _create_phone_otp(session, target, purpose="store_phone")
    try:
        sms_client.send_sms(target, f"ЗАМЧ лангуу баталгаажуулах код: {code}")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    payload: Dict[str, Any] = {
        "ok": True,
        "phone": target,
        "expires_in": 300,
        "message": "Код илгээлээ",
    }
    if ENVIRONMENT != "production" and sms_client.SMS_PROVIDER in ("", "mock", "dev", "none"):
        payload["debug_code"] = code
    return payload


@app.post("/api/mobile/otp/verify")
def api_mobile_otp_verify(
    code: str = Form(...),
    phone: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    user, store = require_mobile_seller(authorization, session)
    target = (phone or store.phone or "").strip()
    if not validate_phone(target):
        raise HTTPException(status_code=400, detail="Утасны дугаар буруу")
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=400, detail="Код 6 оронтой байх ёстой")

    row = session.exec(
        select(PhoneOtp)
        .where(PhoneOtp.phone == target, PhoneOtp.purpose == "store_phone")
        .order_by(PhoneOtp.id.desc())
    ).first()
    if not row:
        raise HTTPException(status_code=400, detail="Код олдсонгүй. Дахин илгээнэ үү")
    try:
        expires = datetime.strptime(row.expires_at, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Код хүчингүй") from exc
    if datetime.now() > expires:
        raise HTTPException(status_code=400, detail="Кодын хугацаа дууссан")
    if row.attempts >= 5:
        raise HTTPException(status_code=400, detail="Олон удаа буруу оруулсан. Шинэ код авна уу")

    if not secrets.compare_digest(row.code_hash, _otp_code_hash(target, code)):
        row.attempts += 1
        session.add(row)
        session.commit()
        raise HTTPException(status_code=400, detail="Код буруу")

    row.attempts = 5  # consume
    store.phone = target
    store.phone_verified = True
    # Keep owner login phone aligned when verifying store contact phone that matches pattern.
    if user.phone == target or not store.phone:
        pass
    session.add(row)
    session.add(store)
    session.commit()
    session.refresh(store)
    return {"ok": True, "store": _store_mobile_payload(store)}


@app.post("/api/mobile/devices")
def api_mobile_device_token(
    expo_push_token: str = Form(...),
    platform: Optional[str] = Form(None),
    device_id: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    user = require_api_user(authorization, session)
    row = session.exec(
        select(DeviceToken).where(DeviceToken.expo_push_token == expo_push_token.strip())
    ).first()
    if row:
        row.user_id = user.id
        row.platform = platform
        row.device_id = device_id
    else:
        row = DeviceToken(
            user_id=user.id,
            expo_push_token=expo_push_token.strip(),
            platform=platform,
            device_id=device_id,
        )
    session.add(row)
    session.commit()
    return {"ok": True}


def _parse_order_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _order_revenue(order: Order) -> float:
    if order.status == "completed" or order.payment_status == "paid":
        return float(order.total or 0)
    return 0.0


def _notify_user(
    session: Session,
    user_id: int,
    title: str,
    body: str,
    data: Dict[str, Any],
) -> None:
    tokens = session.exec(
        select(DeviceToken).where(DeviceToken.user_id == user_id)
    ).all()
    if not tokens:
        return
    messages = [
        {
            "to": row.expo_push_token,
            "title": title,
            "body": body,
            "data": data,
            "sound": "default",
            "channelId": "orders",
        }
        for row in tokens
        if row.expo_push_token
    ]
    if not messages:
        return
    try:
        import httpx

        response = httpx.post(
            "https://exp.host/--/api/v2/push/send",
            json=messages,
            timeout=8.0,
        )
        response.raise_for_status()
        results = response.json().get("data") or []
        changed = False
        for token_row, result in zip(tokens, results):
            if (
                result.get("status") == "error"
                and result.get("details", {}).get("error") == "DeviceNotRegistered"
            ):
                session.delete(token_row)
                changed = True
        if changed:
            session.commit()
    except Exception:
        pass


def _notify_store_new_order(session: Session, store: Store, order: Order) -> None:
    _notify_user(
        session,
        store.owner_id,
        "Шинэ захиалга",
        f"#{order.id} · {float(order.total or 0):,.0f}₮",
        {"order_id": order.id, "type": "new_order"},
    )


def _notify_store_product(
    session: Session,
    product: Product,
    title: str,
    body: str,
    event_type: str,
) -> None:
    store = session.get(Store, product.store_id)
    if store:
        _notify_user(
            session,
            store.owner_id,
            title,
            body,
            {"product_id": product.id, "type": event_type},
        )


@app.get("/api/mobile/dashboard")
def api_mobile_dashboard(
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    products = session.exec(
        select(Product).where(Product.store_id == store.id, Product.is_active == True)
    ).all()
    orders = session.exec(select(Order).where(Order.store_id == store.id)).all()
    now = datetime.now()
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_7d = start_today - timedelta(days=6)
    start_month = start_today.replace(day=1)

    def revenue_since(start: datetime) -> float:
        total = 0.0
        for order in orders:
            created = _parse_order_dt(order.created_at)
            if created and created >= start:
                total += _order_revenue(order)
        return total

    low_stock_rows = [p for p in products if (p.stock or 0) <= 3 and p.is_active]
    return {
        "products": len(products),
        "published": sum(p.publish_status == "published" for p in products),
        "pending_review": sum(p.publish_status == "pending_review" for p in products),
        "low_stock": len(low_stock_rows),
        "pending_orders": sum(o.status in ("pending", "confirmed", "preparing") for o in orders),
        "revenue": sum(_order_revenue(o) for o in orders),
        "revenue_today": revenue_since(start_today),
        "revenue_7d": revenue_since(start_7d),
        "revenue_month": revenue_since(start_month),
        "orders_today": sum(
            1
            for o in orders
            if (created := _parse_order_dt(o.created_at)) and created >= start_today
        ),
        "low_stock_products": [
            {
                "id": p.id,
                "title": p.title,
                "stock": p.stock,
                "sku": p.sku,
            }
            for p in sorted(low_stock_rows, key=lambda x: x.stock)[:10]
        ],
    }


# Brand → model → aliases + common sizes (Mongolia market)
# Edit data/car_catalog.json to add brands/models/sizes; restart server to reload.
_CAR_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "data", "car_catalog.json")


def _load_car_catalog() -> Dict[str, Any]:
    try:
        with open(_CAR_CATALOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("car_catalog.json must be an object")
        return data
    except FileNotFoundError:
        print(f"WARNING: missing {_CAR_CATALOG_PATH}")
        return {}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"WARNING: bad car catalog: {e}")
        return {}


CAR_CATALOG = _load_car_catalog()


def _norm_text(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _fitment_key(width, ratio, diameter, bolt_pattern=None):
    return (width, ratio, diameter, (bolt_pattern or "").strip() or None)


def _find_catalog_entry(brand: str, model: str):
    brand_key = None
    brand_n = _norm_text(brand)
    for b in CAR_CATALOG:
        if _norm_text(b) == brand_n:
            brand_key = b
            break
    if not brand_key:
        return None, None, None

    model_n = _norm_text(model)
    for m, meta in CAR_CATALOG[brand_key].items():
        names = [_norm_text(m)] + [_norm_text(a) for a in meta.get("aliases", [])]
        if model_n in names:
            return brand_key, m, meta
    return brand_key, None, None


def _match_terms_for_vehicle(brand: Optional[str], model: Optional[str], car_make: Optional[str] = None):
    """Terms to match Product.car_make (sellers may write Prius / Land 200 / Toyota Prius)."""
    terms = []
    if brand and model:
        bkey, mkey, meta = _find_catalog_entry(brand, model)
        if bkey and mkey and meta:
            terms.extend([mkey, f"{bkey} {mkey}"])
            terms.extend(meta.get("aliases") or [])
        else:
            terms.extend([model, f"{brand} {model}".strip()])
    elif car_make and car_make.strip():
        raw = car_make.strip()
        terms.append(raw)
        # Try resolve free-text against aliases
        raw_n = _norm_text(raw)
        for b, models in CAR_CATALOG.items():
            for m, meta in models.items():
                names = [_norm_text(m), _norm_text(f"{b} {m}")] + [_norm_text(a) for a in meta.get("aliases", [])]
                if raw_n in names or any(raw_n in n or n in raw_n for n in names if len(n) >= 3):
                    terms.extend([m, f"{b} {m}"])
                    terms.extend(meta.get("aliases") or [])
                    break

    # unique preserve order
    out = []
    seen = set()
    for t in terms:
        t = (t or "").strip()
        if not t:
            continue
        k = _norm_text(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _approved_store_ids(session: Session):
    return [
        s.id
        for s in session.exec(
            select(Store).where(Store.is_active == True, Store.is_approved == True)
        ).all()
    ]


def _fitments_for_vehicle(brand: str, model: str, session: Session):
    brand_key, model_key, meta = _find_catalog_entry(brand, model)
    terms = _match_terms_for_vehicle(brand, model)

    found = []
    seen = set()
    store_ids = _approved_store_ids(session)
    guide_bolts = list((meta or {}).get("bolt_patterns") or [])

    if store_ids and terms:
        clauses = [Product.car_make.like(f"%{t}%") for t in terms]
        rows = session.exec(
            select(
                Product.width,
                Product.ratio,
                Product.diameter,
                Product.bolt_pattern,
            )
            .where(
                Product.is_active == True,
                Product.stock > 0,
                Product.publish_status == "published",
                Product.store_id.in_(store_ids),
                Product.car_make != None,
                or_(*clauses),
            )
        ).all()
        default_bolt = guide_bolts[0] if guide_bolts else None
        for width, ratio, diameter, bolt in rows:
            if not diameter and not width:
                continue
            bolt_val = (bolt or "").strip() or default_bolt
            key = _fitment_key(width, ratio, diameter, bolt_val)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "width": width,
                    "ratio": ratio,
                    "diameter": diameter,
                    "bolt_pattern": bolt_val,
                    "source": "catalog",
                }
            )

    if meta:
        default_bolt = guide_bolts[0] if guide_bolts else None
        for size in meta.get("sizes") or []:
            entry = dict(size)
            if not entry.get("bolt_pattern") and default_bolt:
                entry["bolt_pattern"] = default_bolt
            key = _fitment_key(
                entry.get("width"),
                entry.get("ratio"),
                entry.get("diameter"),
                entry.get("bolt_pattern"),
            )
            if key in seen:
                continue
            seen.add(key)
            found.append({**entry, "source": "guide"})

    def sort_key(item):
        return (
            item.get("diameter") or 0,
            item.get("width") or 0,
            item.get("ratio") or 0,
            item.get("bolt_pattern") or "",
        )

    # Unique bolt list: guide first, then any from product rows
    bolts_out = []
    bolts_seen = set()
    for b in guide_bolts + [f.get("bolt_pattern") for f in found]:
        b = (b or "").strip()
        if not b or b in bolts_seen:
            continue
        bolts_seen.add(b)
        bolts_out.append(b)

    return {
        "brand": brand_key or brand.strip(),
        "model": model_key or model.strip(),
        "aliases": (meta or {}).get("aliases", []),
        "bolt_patterns": bolts_out,
        "sizes": sorted(found, key=sort_key)[:12],
    }


@app.get("/api/categories")
def api_categories(session: Session = Depends(get_session)):
    cats = session.exec(select(Category).order_by(Category.id)).all()
    cats = [c for c in cats if c.slug in PUBLIC_CATEGORY_SLUGS]
    return {"data": [c.model_dump() if hasattr(c, "model_dump") else c.dict() for c in cats]}


@app.get("/api/car-brands")
def api_car_brands():
    return {"data": sorted(CAR_CATALOG.keys(), key=str.lower)}


@app.get("/api/car-models")
def api_car_models(brand: str = Query(...)):
    brand_n = _norm_text(brand)
    brand_key = next((b for b in CAR_CATALOG if _norm_text(b) == brand_n), None)
    if not brand_key:
        return {"data": [], "brand": brand.strip()}
    models = []
    for name, meta in CAR_CATALOG[brand_key].items():
        aliases = meta.get("aliases") or []
        # Prefer short Mongolian-friendly alias in hint, keep official name as value
        hint_aliases = [a for a in aliases if _norm_text(a) != _norm_text(name)]
        label = name
        if hint_aliases:
            label = f"{name} · {hint_aliases[0]}"
        models.append(
            {
                "name": name,
                "label": label,
                "aliases": aliases,
            }
        )
    models.sort(key=lambda m: m["name"].lower())
    return {"data": models, "brand": brand_key}


@app.get("/api/car-makes")
def api_car_makes(session: Session = Depends(get_session)):
    """Legacy flat list (brand + model) for older clients."""
    makes = {f"{brand} {model}" for brand, models in CAR_CATALOG.items() for model in models}
    store_ids = _approved_store_ids(session)
    if store_ids:
        rows = session.exec(
            select(Product.car_make)
            .where(
                Product.is_active == True,
                Product.stock > 0,
                Product.publish_status == "published",
                Product.store_id.in_(store_ids),
                Product.car_make != None,
                Product.car_make != "",
            )
            .distinct()
        ).all()
        makes.update((m or "").strip() for m in rows if (m or "").strip())
    return {"data": sorted(makes, key=str.lower)}


@app.get("/api/car-fitments")
def api_car_fitments(
    brand: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    make: Optional[str] = Query(None),  # legacy: "Toyota Prius"
    session: Session = Depends(get_session),
):
    if brand and model:
        result = _fitments_for_vehicle(brand, model, session)
        return {
            "data": result["sizes"],
            **{k: result[k] for k in ("brand", "model", "aliases", "bolt_patterns")},
        }

    # Legacy single-field make → try split "Brand Model"
    raw = (make or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail="brand+model or make required")
    raw_n = _norm_text(raw)
    for b, models in CAR_CATALOG.items():
        for m, meta in models.items():
            names = [_norm_text(m), _norm_text(f"{b} {m}")] + [_norm_text(a) for a in meta.get("aliases", [])]
            if raw_n in names:
                result = _fitments_for_vehicle(b, m, session)
                return {
                    "data": result["sizes"],
                    **{k: result[k] for k in ("brand", "model", "aliases", "bolt_patterns")},
                }
    # Fallback: treat whole string as model search term
    return {"data": [], "brand": None, "model": raw, "aliases": [raw], "bolt_patterns": []}


@app.get("/api/products")
def api_products(
    category: Optional[str] = Query(None),
    category_id: Optional[int] = Query(None),
    intent: Optional[str] = Query(None),
    car_make: Optional[str] = Query(None),
    car_brand: Optional[str] = Query(None),
    car_model: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    width: Optional[int] = Query(None),
    ratio: Optional[int] = Query(None),
    diameter: Optional[int] = Query(None),
    bolt_pattern: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    store_id: Optional[int] = Query(None),
    session: Session = Depends(get_session),
):
    approved_store_ids = [
        s.id
        for s in session.exec(
            select(Store).where(Store.is_active == True, Store.is_approved == True)
        ).all()
    ]
    if not approved_store_ids:
        return {"data": [], "is_exact_match": True}

    query = select(Product).where(
        Product.is_active == True,
        Product.stock > 0,
        Product.publish_status == "published",
        Product.store_id.in_(approved_store_ids),
    )

    public_cats = session.exec(
        select(Category).where(Category.slug.in_(list(PUBLIC_CATEGORY_SLUGS)))
    ).all()
    public_ids = [c.id for c in public_cats]
    cats_by_slug = {c.slug: c for c in public_cats}

    intent_norm = (intent or "").strip().lower()
    if intent_norm and intent_norm not in LISTING_KINDS:
        return {"data": [], "is_exact_match": True}

    cat = None
    if intent_norm == "combo":
        query = query.where(Product.listing_kind == "combo")
        dugui = cats_by_slug.get("dugui")
        if dugui:
            query = query.where(Product.category_id == dugui.id)
            cat = dugui
    elif intent_norm == "dugui":
        dugui = cats_by_slug.get("dugui")
        if not dugui:
            return {"data": [], "is_exact_match": True}
        cat = dugui
        query = query.where(Product.category_id == dugui.id)
        query = query.where(
            or_(Product.listing_kind == None, Product.listing_kind == "", Product.listing_kind == "dugui")
        )
    elif intent_norm == "obud":
        obud = cats_by_slug.get("obud")
        if not obud:
            return {"data": [], "is_exact_match": True}
        cat = obud
        query = query.where(Product.category_id == obud.id)
        query = query.where(
            or_(Product.listing_kind == None, Product.listing_kind == "", Product.listing_kind == "obud")
        )
    elif category:
        cat = next((c for c in public_cats if c.slug == category), None)
        if cat:
            query = query.where(Product.category_id == cat.id)
        else:
            return {"data": [], "is_exact_match": True}
    elif category_id:
        if category_id not in public_ids:
            return {"data": [], "is_exact_match": True}
        query = query.where(Product.category_id == category_id)
    elif public_ids:
        query = query.where(Product.category_id.in_(public_ids))

    if store_id:
        query = query.where(Product.store_id == store_id)
    if condition:
        query = query.where(Product.condition == condition)
    if width:
        query = query.where(Product.width == width)
    if ratio:
        query = query.where(Product.ratio == ratio)
    if diameter:
        query = query.where(Product.diameter == diameter)
    if bolt_pattern:
        query = query.where(Product.bolt_pattern == bolt_pattern)
    vehicle_terms = _match_terms_for_vehicle(car_brand, car_model, car_make)
    if vehicle_terms:
        query = query.where(or_(*[Product.car_make.like(f"%{t}%") for t in vehicle_terms]))
    if q:
        term = f"%{q.strip()}%"
        query = query.where(
            or_(
                Product.title.like(term),
                Product.brand.like(term),
                Product.car_make.like(term),
            )
        )

    query = query.order_by(Product.id.desc())
    products = session.exec(query).all()
    is_exact_match = True

    if not products and (width or ratio or diameter or bolt_pattern):
        fallback = select(Product).where(
            Product.is_active == True,
            Product.stock > 0,
            Product.publish_status == "published",
            Product.store_id.in_(approved_store_ids),
        )
        if intent_norm == "combo":
            fallback = fallback.where(Product.listing_kind == "combo")
            dugui = cats_by_slug.get("dugui")
            if dugui:
                fallback = fallback.where(Product.category_id == dugui.id)
        elif cat:
            fallback = fallback.where(Product.category_id == cat.id)
            if intent_norm == "dugui":
                fallback = fallback.where(
                    or_(Product.listing_kind == None, Product.listing_kind == "", Product.listing_kind == "dugui")
                )
            elif intent_norm == "obud":
                fallback = fallback.where(
                    or_(Product.listing_kind == None, Product.listing_kind == "", Product.listing_kind == "obud")
                )
        elif category_id:
            fallback = fallback.where(Product.category_id == category_id)
        elif public_ids:
            fallback = fallback.where(Product.category_id.in_(public_ids))
        if diameter:
            fallback = fallback.where(Product.diameter == diameter)
        if condition:
            fallback = fallback.where(Product.condition == condition)
        if vehicle_terms:
            fallback = fallback.where(or_(*[Product.car_make.like(f"%{t}%") for t in vehicle_terms]))
        products = session.exec(fallback.order_by(Product.id.desc())).all()
        if products:
            is_exact_match = False

    stores = {s.id: s for s in session.exec(select(Store)).all()}
    categories = {c.id: c for c in session.exec(select(Category)).all()}
    data = [
        product_to_dict(p, stores.get(p.store_id), categories.get(p.category_id))
        for p in products
    ]
    return {"data": data, "is_exact_match": is_exact_match}


@app.get("/api/products/{product_id}")
def api_product(product_id: int, session: Session = Depends(get_session)):
    product = session.get(Product, product_id)
    if not product or not product.is_active or product.publish_status != "published":
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    store = session.get(Store, product.store_id)
    if not store or not store.is_active or not store.is_approved:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    category = session.get(Category, product.category_id)
    return product_to_dict(product, store, category)


# --- Store / Seller ---
@app.get("/api/stores")
def api_public_stores(session: Session = Depends(get_session)):
    """Only approved + active stores, ranked by units sold."""
    stores = session.exec(
        select(Store).where(Store.is_active == True, Store.is_approved == True)
    ).all()
    sold_by_store: Dict[int, int] = {}
    product_count: Dict[int, int] = {}

    for p in session.exec(
        select(Product).where(
            Product.is_active == True,
            Product.stock > 0,
            Product.publish_status == "published",
        )
    ).all():
        product_count[p.store_id] = product_count.get(p.store_id, 0) + 1

    orders = session.exec(
        select(Order).where(
            or_(Order.status == "completed", Order.payment_status == "paid")
        )
    ).all()
    order_store = {o.id: o.store_id for o in orders}
    if order_store:
        items = session.exec(
            select(OrderItem).where(OrderItem.order_id.in_(list(order_store.keys())))
        ).all()
        for item in items:
            sid = order_store.get(item.order_id)
            if sid is None:
                continue
            sold_by_store[sid] = sold_by_store.get(sid, 0) + int(item.quantity or 0)

    ranked = sorted(
        stores,
        key=lambda s: (-sold_by_store.get(s.id or 0, 0), (s.name or "").lower()),
    )
    data = [
        {
            "id": s.id,
            "name": s.name,
            "logo": s.logo,
            "sold": sold_by_store.get(s.id or 0, 0),
            "product_count": product_count.get(s.id or 0, 0),
        }
        for s in ranked
    ]
    return {"count": len(data), "data": data}


@app.post("/api/stores")
def api_create_store():
    """Web no longer opens stores — use ЗАМЧ Seller app."""
    raise HTTPException(
        status_code=403,
        detail="Дэлгүүр нээх нь зөвхөн ЗАМЧ Seller аппаар хийгдэнэ. /for-sellers хуудаснаас мэдээлэл авна уу.",
    )


@app.get("/api/stores/mine")
def api_my_store(request: Request, session: Session = Depends(get_session)):
    user = require_user(request, session)
    store = get_user_store(session, user.id)
    if not store:
        return {"store": None}
    return {"store": store.model_dump() if hasattr(store, "model_dump") else store.dict()}


@app.get("/api/stores/{store_id}")
def api_public_store(store_id: int, session: Session = Depends(get_session)):
    store = session.get(Store, store_id)
    if not store or not store.is_active or not store.is_approved:
        raise HTTPException(status_code=404, detail="Дэлгүүр олдсонгүй")

    products = session.exec(
        select(Product).where(
            Product.store_id == store.id,
            Product.is_active == True,
            Product.stock > 0,
            Product.publish_status == "published",
        )
    ).all()
    sold_orders = session.exec(
        select(Order).where(
            Order.store_id == store.id,
            or_(Order.status == "completed", Order.payment_status == "paid"),
        )
    ).all()
    sold_order_ids = [o.id for o in sold_orders]
    sold = 0
    if sold_order_ids:
        sold = sum(
            int(item.quantity or 0)
            for item in session.exec(
                select(OrderItem).where(OrderItem.order_id.in_(sold_order_ids))
            ).all()
        )

    return {
        "id": store.id,
        "name": store.name,
        "description": store.description,
        "location": store.location,
        "logo": store.logo,
        "product_count": len(products),
        "sold": sold,
    }


@app.get("/api/seller/products")
def api_seller_products(request: Request, session: Session = Depends(get_session)):
    user = require_user(request, session)
    store = get_user_store(session, user.id)
    if not store:
        raise HTTPException(status_code=400, detail="Эхлээд дэлгүүр нээнэ үү")
    products = session.exec(
        select(Product).where(Product.store_id == store.id).order_by(Product.id.desc())
    ).all()
    categories = {c.id: c for c in session.exec(select(Category)).all()}
    return {
        "data": [product_to_dict(p, store, categories.get(p.category_id)) for p in products]
    }


@app.post("/api/seller/products")
async def api_seller_create_product(
    request: Request,
    category_id: int = Form(...),
    title: Optional[str] = Form(None),
    brand: Optional[str] = Form(None),
    condition: str = Form("Шинэ"),
    pack_type: Optional[str] = Form(None),
    price: float = Form(...),
    stock: int = Form(1),
    description: Optional[str] = Form(None),
    width: Optional[int] = Form(None),
    ratio: Optional[int] = Form(None),
    diameter: Optional[int] = Form(None),
    tread_percent: Optional[int] = Form(None),
    bolt_pattern: Optional[str] = Form(None),
    wheel_type: Optional[str] = Form(None),
    car_make: Optional[str] = Form(None),
    listing_kind: Optional[str] = Form(None),
    files: Optional[List[UploadFile]] = File(None),
    session: Session = Depends(get_session),
):
    user = require_user(request, session)
    store = get_user_store(session, user.id)
    if not store:
        raise HTTPException(status_code=400, detail="Эхлээд дэлгүүр нээнэ үү")
    if price <= 0:
        raise HTTPException(status_code=400, detail="Үнэ 0-ээс их байх ёстой")
    if stock < 0:
        raise HTTPException(status_code=400, detail="Нөөц буруу")

    category = session.get(Category, category_id)
    if not category:
        raise HTTPException(status_code=400, detail="Ангилал олдсонгүй")
    if category.slug not in PUBLIC_CATEGORY_SLUGS:
        raise HTTPException(status_code=400, detail="Зөвхөн дугуй, обуд оруулна")

    kind = _normalize_listing_kind(listing_kind, category.slug)
    if kind == "combo" and category.slug != "dugui":
        # Combo listings sit under dugui category
        dugui = session.exec(select(Category).where(Category.slug == "dugui")).first()
        if not dugui:
            raise HTTPException(status_code=400, detail="Дугуй ангилал олдсонгүй")
        category = dugui
        category_id = dugui.id

    images = await save_uploads(files)
    if not images:
        images = ["/static/placeholder.jpg"]

    final_title = (title or "").strip()
    if not final_title:
        if kind == "combo":
            dim = f" {width}/{ratio}" if width and ratio else ""
            final_title = f"{brand or 'Обудтай дугуй'}{dim} R{diameter or ''}".strip()
        elif category.slug == "dugui":
            dim = f" {width}/{ratio}" if width and ratio else ""
            final_title = f"{brand or 'Дугуй'}{dim} R{diameter or ''}".strip()
        elif category.slug == "obud":
            final_title = f"{brand or 'Обуд'} R{diameter or ''} {bolt_pattern or ''}".strip()
        else:
            final_title = brand or category.name

    product = Product(
        store_id=store.id,
        category_id=category_id,
        title=final_title,
        brand=brand,
        condition=condition,
        pack_type=pack_type,
        price=price,
        stock=stock,
        description=description,
        images=json.dumps(images),
        width=width,
        ratio=ratio,
        diameter=diameter,
        tread_percent=tread_percent,
        bolt_pattern=bolt_pattern,
        wheel_type=wheel_type,
        car_make=(car_make or "").strip() or None,
        listing_kind=kind,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product_to_dict(product, store, category)


@app.patch("/api/seller/products/{product_id}")
def api_seller_update_product(
    product_id: int,
    request: Request,
    price: Optional[float] = Form(None),
    stock: Optional[int] = Form(None),
    is_active: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    user = require_user(request, session)
    store = get_user_store(session, user.id)
    if not store:
        raise HTTPException(status_code=400, detail="Дэлгүүр олдсонгүй")
    product = session.get(Product, product_id)
    if not product or product.store_id != store.id:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")

    if price is not None:
        if price <= 0:
            raise HTTPException(status_code=400, detail="Үнэ буруу")
        product.price = price
    if stock is not None:
        if stock < 0:
            raise HTTPException(status_code=400, detail="Нөөц буруу")
        product.stock = stock
    if title is not None and title.strip():
        product.title = title.strip()
    if is_active is not None:
        product.is_active = is_active in ("1", "true", "on", "yes")

    session.add(product)
    session.commit()
    session.refresh(product)
    return product_to_dict(product, store)


# --- Mobile warehouses / inventory / products ---
def _warehouse_payload(warehouse: Warehouse) -> Dict[str, Any]:
    return warehouse.model_dump() if hasattr(warehouse, "model_dump") else warehouse.dict()


def _sync_product_stock(session: Session, product_id: int) -> int:
    balances = session.exec(
        select(InventoryBalance).where(InventoryBalance.product_id == product_id)
    ).all()
    total = sum(max(0, int(balance.quantity or 0)) for balance in balances)
    product = session.get(Product, product_id)
    if product:
        product.stock = total
        session.add(product)
    return total


def _deduct_inventory_for_sale(
    session: Session,
    product: Product,
    quantity: int,
    order_id: int,
    actor_user_id: Optional[int],
) -> bool:
    balances = session.exec(
        select(InventoryBalance)
        .where(
            InventoryBalance.product_id == product.id,
            InventoryBalance.quantity > 0,
        )
        .order_by(InventoryBalance.id)
    ).all()
    if not balances:
        return False
    if sum(row.quantity for row in balances) < quantity:
        raise HTTPException(status_code=409, detail=f"Агуулахын нөөц хүрэлцэхгүй: {product.title}")
    remaining = quantity
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for balance in balances:
        used = min(balance.quantity, remaining)
        if not used:
            continue
        balance.quantity -= used
        balance.updated_at = now
        session.add(balance)
        session.add(
            StockMovement(
                store_id=product.store_id,
                warehouse_id=balance.warehouse_id,
                product_id=product.id,
                movement_type="sale",
                quantity_delta=-used,
                reason="Marketplace захиалга",
                reference_type="order",
                reference_id=order_id,
                actor_user_id=actor_user_id,
            )
        )
        remaining -= used
        if remaining == 0:
            break
    product.stock = sum(row.quantity for row in balances)
    session.add(product)
    return True


@app.get("/api/mobile/warehouses")
def api_mobile_warehouses(
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    rows = session.exec(
        select(Warehouse)
        .where(Warehouse.store_id == store.id, Warehouse.is_active == True)
        .order_by(Warehouse.is_default.desc(), Warehouse.id)
    ).all()
    return {"data": [_warehouse_payload(row) for row in rows]}


@app.post("/api/mobile/warehouses")
def api_mobile_create_warehouse(
    name: str = Form(...),
    address: Optional[str] = Form(None),
    is_default: bool = Form(False),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    if len(name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Агуулахын нэр оруулна уу")
    existing = session.exec(
        select(Warehouse).where(
            Warehouse.store_id == store.id,
            Warehouse.name == name.strip(),
            Warehouse.is_active == True,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Ижил нэртэй агуулах байна")
    if is_default:
        for row in session.exec(
            select(Warehouse).where(Warehouse.store_id == store.id)
        ).all():
            row.is_default = False
            session.add(row)
    row = Warehouse(
        store_id=store.id,
        name=name.strip(),
        address=(address or "").strip() or None,
        is_default=is_default,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _warehouse_payload(row)


@app.patch("/api/mobile/warehouses/{warehouse_id}")
def api_mobile_update_warehouse(
    warehouse_id: int,
    name: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    is_default: Optional[bool] = Form(None),
    is_active: Optional[bool] = Form(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    row = session.get(Warehouse, warehouse_id)
    if not row or row.store_id != store.id:
        raise HTTPException(status_code=404, detail="Агуулах олдсонгүй")
    if name is not None and name.strip():
        row.name = name.strip()
    if address is not None:
        row.address = address.strip() or None
    if is_default:
        for other in session.exec(
            select(Warehouse).where(Warehouse.store_id == store.id)
        ).all():
            other.is_default = other.id == row.id
            session.add(other)
    if is_active is not None:
        if not is_active:
            nonzero = session.exec(
                select(InventoryBalance).where(
                    InventoryBalance.warehouse_id == row.id,
                    InventoryBalance.quantity > 0,
                )
            ).first()
            if nonzero:
                raise HTTPException(status_code=409, detail="Нөөцтэй агуулахыг хаах боломжгүй")
        row.is_active = is_active
    session.add(row)
    session.commit()
    session.refresh(row)
    return _warehouse_payload(row)


@app.get("/api/mobile/inventory")
def api_mobile_inventory(
    product_id: Optional[int] = Query(None),
    warehouse_id: Optional[int] = Query(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    products = {
        p.id: p
        for p in session.exec(select(Product).where(Product.store_id == store.id)).all()
    }
    warehouses = {
        w.id: w
        for w in session.exec(
            select(Warehouse).where(Warehouse.store_id == store.id)
        ).all()
    }
    query = select(InventoryBalance)
    if product_id:
        query = query.where(InventoryBalance.product_id == product_id)
    if warehouse_id:
        query = query.where(InventoryBalance.warehouse_id == warehouse_id)
    rows = session.exec(query.order_by(InventoryBalance.updated_at.desc())).all()
    data = []
    for row in rows:
        product = products.get(row.product_id)
        warehouse = warehouses.get(row.warehouse_id)
        if not product or not warehouse:
            continue
        data.append(
            {
                "id": row.id,
                "product_id": product.id,
                "product_title": product.title,
                "sku": product.sku,
                "warehouse_id": warehouse.id,
                "warehouse_name": warehouse.name,
                "quantity": row.quantity,
                "low_stock_threshold": row.low_stock_threshold,
                "is_low_stock": row.quantity <= row.low_stock_threshold,
                "updated_at": row.updated_at,
            }
        )
    return {"data": data}


@app.post("/api/mobile/inventory/adjust")
def api_mobile_inventory_adjust(
    product_id: int = Form(...),
    warehouse_id: int = Form(...),
    quantity_delta: int = Form(...),
    reason: Optional[str] = Form(None),
    low_stock_threshold: Optional[int] = Form(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    user, store = require_mobile_seller(authorization, session)
    product = session.get(Product, product_id)
    warehouse = session.get(Warehouse, warehouse_id)
    if not product or product.store_id != store.id:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    if not warehouse or warehouse.store_id != store.id or not warehouse.is_active:
        raise HTTPException(status_code=404, detail="Агуулах олдсонгүй")
    balance = session.exec(
        select(InventoryBalance).where(
            InventoryBalance.product_id == product.id,
            InventoryBalance.warehouse_id == warehouse.id,
        )
    ).first()
    if not balance:
        balance = InventoryBalance(
            product_id=product.id,
            warehouse_id=warehouse.id,
            quantity=0,
        )
    previous_quantity = int(balance.quantity or 0)
    previous_threshold = int(balance.low_stock_threshold or 0)
    was_low = previous_quantity <= previous_threshold
    new_quantity = balance.quantity + quantity_delta
    if new_quantity < 0:
        raise HTTPException(status_code=409, detail="Агуулахын нөөц хүрэлцэхгүй байна")
    balance.quantity = new_quantity
    if low_stock_threshold is not None:
        balance.low_stock_threshold = max(0, low_stock_threshold)
    balance.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    session.add(balance)
    session.add(
        StockMovement(
            store_id=store.id,
            warehouse_id=warehouse.id,
            product_id=product.id,
            movement_type="adjustment",
            quantity_delta=quantity_delta,
            reason=(reason or "").strip() or None,
            actor_user_id=user.id,
        )
    )
    session.flush()
    total = _sync_product_stock(session, product.id)
    session.commit()
    if not was_low and new_quantity <= balance.low_stock_threshold:
        _notify_user(
            session,
            store.owner_id,
            "Нөөц багаслаа",
            f"{product.title} · {warehouse.name}: {new_quantity} ширхэг",
            {
                "product_id": product.id,
                "warehouse_id": warehouse.id,
                "type": "low_stock",
            },
        )
    return {"ok": True, "warehouse_quantity": new_quantity, "total_stock": total}


@app.post("/api/mobile/inventory/transfer")
def api_mobile_inventory_transfer(
    product_id: int = Form(...),
    from_warehouse_id: int = Form(...),
    to_warehouse_id: int = Form(...),
    quantity: int = Form(...),
    reason: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    user, store = require_mobile_seller(authorization, session)
    if quantity <= 0 or from_warehouse_id == to_warehouse_id:
        raise HTTPException(status_code=400, detail="Шилжүүлгийн мэдээлэл буруу")
    product = session.get(Product, product_id)
    source = session.get(Warehouse, from_warehouse_id)
    target = session.get(Warehouse, to_warehouse_id)
    if not product or product.store_id != store.id:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    if (
        not source
        or not target
        or source.store_id != store.id
        or target.store_id != store.id
        or not source.is_active
        or not target.is_active
    ):
        raise HTTPException(status_code=404, detail="Агуулах олдсонгүй")
    source_balance = session.exec(
        select(InventoryBalance).where(
            InventoryBalance.product_id == product.id,
            InventoryBalance.warehouse_id == source.id,
        )
    ).first()
    if not source_balance or source_balance.quantity < quantity:
        raise HTTPException(status_code=409, detail="Шилжүүлэх нөөц хүрэлцэхгүй байна")
    target_balance = session.exec(
        select(InventoryBalance).where(
            InventoryBalance.product_id == product.id,
            InventoryBalance.warehouse_id == target.id,
        )
    ).first()
    if not target_balance:
        target_balance = InventoryBalance(
            product_id=product.id, warehouse_id=target.id, quantity=0
        )
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    source_balance.quantity -= quantity
    source_balance.updated_at = now
    target_balance.quantity += quantity
    target_balance.updated_at = now
    session.add(source_balance)
    session.add(target_balance)
    transfer_ref = secrets.randbelow(1_000_000_000)
    for warehouse, delta, movement_type in (
        (source, -quantity, "transfer_out"),
        (target, quantity, "transfer_in"),
    ):
        session.add(
            StockMovement(
                store_id=store.id,
                warehouse_id=warehouse.id,
                product_id=product.id,
                movement_type=movement_type,
                quantity_delta=delta,
                reason=(reason or "").strip() or None,
                reference_type="transfer",
                reference_id=transfer_ref,
                actor_user_id=user.id,
            )
        )
    total = _sync_product_stock(session, product.id)
    session.commit()
    return {"ok": True, "total_stock": total}


@app.get("/api/mobile/stock-movements")
def api_mobile_stock_movements(
    product_id: Optional[int] = Query(None),
    warehouse_id: Optional[int] = Query(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    query = select(StockMovement).where(StockMovement.store_id == store.id)
    if product_id:
        query = query.where(StockMovement.product_id == product_id)
    if warehouse_id:
        query = query.where(StockMovement.warehouse_id == warehouse_id)
    rows = session.exec(query.order_by(StockMovement.id.desc()).limit(200)).all()
    return {
        "data": [r.model_dump() if hasattr(r, "model_dump") else r.dict() for r in rows]
    }


@app.get("/api/mobile/products")
def api_mobile_products(
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    query = select(Product).where(Product.store_id == store.id, Product.is_active == True)
    if status:
        query = query.where(Product.publish_status == status)
    rows = session.exec(query.order_by(Product.id.desc())).all()
    if q and q.strip():
        needle = q.strip().lower()
        rows = [
            p
            for p in rows
            if needle in (p.title or "").lower()
            or needle in (p.sku or "").lower()
            or needle in (p.brand or "").lower()
        ]
    categories = {c.id: c for c in session.exec(select(Category)).all()}
    return {"data": [product_to_dict(p, store, categories.get(p.category_id)) for p in rows]}


@app.get("/api/mobile/products/{product_id}")
def api_mobile_product(
    product_id: int,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    product = session.get(Product, product_id)
    if not product or product.store_id != store.id:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    category = session.get(Category, product.category_id)
    return product_to_dict(product, store, category)


@app.patch("/api/mobile/products/{product_id}")
async def api_mobile_update_product(
    product_id: int,
    category_id: Optional[int] = Form(None),
    title: Optional[str] = Form(None),
    price: Optional[float] = Form(None),
    description: Optional[str] = Form(None),
    sku: Optional[str] = Form(None),
    brand: Optional[str] = Form(None),
    condition: Optional[str] = Form(None),
    pack_type: Optional[str] = Form(None),
    car_make: Optional[str] = Form(None),
    listing_kind: Optional[str] = Form(None),
    width: Optional[int] = Form(None),
    ratio: Optional[int] = Form(None),
    diameter: Optional[int] = Form(None),
    tread_percent: Optional[int] = Form(None),
    bolt_pattern: Optional[str] = Form(None),
    wheel_type: Optional[str] = Form(None),
    files: Optional[List[UploadFile]] = File(None),
    video: Optional[UploadFile] = File(None),
    clear_video: Optional[str] = Form(None),
    keep_images: Optional[str] = Form(None),
    stock: Optional[int] = Form(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    product = session.get(Product, product_id)
    if not product or product.store_id != store.id:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    if category_id is not None:
        category = session.get(Category, category_id)
        if not category:
            raise HTTPException(status_code=400, detail="Ангилал олдсонгүй")
        if category.slug not in PUBLIC_CATEGORY_SLUGS:
            raise HTTPException(status_code=400, detail="Зөвхөн дугуй, обуд оруулна")
        product.category_id = category.id
    if listing_kind is not None:
        cat = session.get(Category, product.category_id)
        kind = _normalize_listing_kind(listing_kind, cat.slug if cat else None)
        if kind == "combo":
            dugui = session.exec(select(Category).where(Category.slug == "dugui")).first()
            if dugui:
                product.category_id = dugui.id
        product.listing_kind = kind
    if car_make is not None:
        product.car_make = car_make.strip() or None
    if title is not None and title.strip():
        product.title = title.strip()
    if price is not None:
        if price <= 0:
            raise HTTPException(status_code=400, detail="Үнэ буруу")
        product.price = price
    if description is not None:
        product.description = description.strip() or None
    if sku is not None:
        normalized_sku = sku.strip() or None
        if normalized_sku:
            existing = session.exec(
                select(Product).where(
                    Product.store_id == store.id,
                    Product.sku == normalized_sku,
                    Product.id != product.id,
                )
            ).first()
            if existing:
                raise HTTPException(status_code=409, detail="Энэ SKU бүртгэлтэй байна")
        product.sku = normalized_sku
    if brand is not None:
        product.brand = brand.strip() or None
    if condition is not None and condition.strip():
        product.condition = condition.strip()
    if pack_type is not None:
        product.pack_type = pack_type.strip() or None
    if width is not None:
        product.width = width
    if ratio is not None:
        product.ratio = ratio
    if diameter is not None:
        product.diameter = diameter
    if tread_percent is not None:
        product.tread_percent = tread_percent
    if bolt_pattern is not None:
        product.bolt_pattern = bolt_pattern.strip() or None
    if wheel_type is not None:
        product.wheel_type = wheel_type.strip() or None
    if files:
        uploaded = await save_uploads(files)
        kept: List[str] = []
        if keep_images is not None:
            try:
                parsed = json.loads(keep_images) if keep_images.strip() else []
                if isinstance(parsed, list):
                    kept = [str(item) for item in parsed if str(item).startswith("/photos/")]
            except json.JSONDecodeError:
                kept = []
        elif keep_images is None:
            kept = parse_images(product.images)
        merged = (kept + uploaded)[:5]
        product.images = json.dumps(merged)
    elif keep_images is not None:
        try:
            parsed = json.loads(keep_images) if keep_images.strip() else []
            if not isinstance(parsed, list):
                parsed = []
        except json.JSONDecodeError:
            parsed = []
        kept = [str(item) for item in parsed if str(item).startswith("/photos/")]
        product.images = json.dumps(kept[:5])
    if clear_video in ("1", "true", "True"):
        product.video = None
    if video and video.filename:
        product.video = await save_silent_video(video)
    if stock is not None:
        if stock < 0:
            raise HTTPException(status_code=400, detail="Тоо буруу")
        warehouse = session.exec(
            select(Warehouse).where(Warehouse.store_id == store.id, Warehouse.is_default == True)
        ).first() or session.exec(
            select(Warehouse).where(Warehouse.store_id == store.id, Warehouse.is_active == True)
        ).first()
        if warehouse:
            balance = session.exec(
                select(InventoryBalance).where(
                    InventoryBalance.product_id == product.id,
                    InventoryBalance.warehouse_id == warehouse.id,
                )
            ).first()
            if not balance:
                balance = InventoryBalance(
                    product_id=product.id,
                    warehouse_id=warehouse.id,
                    quantity=0,
                )
            previous = int(balance.quantity or 0)
            delta = int(stock) - previous
            balance.quantity = int(stock)
            balance.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            session.add(balance)
            if delta != 0:
                session.add(
                    StockMovement(
                        store_id=store.id,
                        warehouse_id=warehouse.id,
                        product_id=product.id,
                        movement_type="adjustment",
                        quantity_delta=delta,
                        reason="seller_app_edit",
                        actor_user_id=store.owner_id,
                    )
                )
            session.flush()
            _sync_product_stock(session, product.id)
        else:
            product.stock = int(stock)
    if product.publish_status == "published":
        product.publish_status = "draft"
        product.rejection_reason = None
    session.add(product)
    session.commit()
    session.refresh(product)
    category = session.get(Category, product.category_id)
    return product_to_dict(product, store, category)


@app.delete("/api/mobile/products/{product_id}")
def api_mobile_delete_product(
    product_id: int,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    product = session.get(Product, product_id)
    if not product or product.store_id != store.id:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    product.is_active = False
    product.publish_status = "suspended"
    session.add(product)
    session.commit()
    return {"ok": True, "product_id": product.id}


@app.post("/api/mobile/products")
async def api_mobile_create_product(
    category_id: int = Form(...),
    title: Optional[str] = Form(None),
    price: float = Form(...),
    description: Optional[str] = Form(None),
    sku: Optional[str] = Form(None),
    barcode: Optional[str] = Form(None),
    brand: Optional[str] = Form(None),
    condition: str = Form("Шинэ"),
    pack_type: Optional[str] = Form(None),
    warehouse_id: Optional[int] = Form(None),
    initial_stock: int = Form(0),
    width: Optional[int] = Form(None),
    ratio: Optional[int] = Form(None),
    diameter: Optional[int] = Form(None),
    tread_percent: Optional[int] = Form(None),
    bolt_pattern: Optional[str] = Form(None),
    wheel_type: Optional[str] = Form(None),
    car_make: Optional[str] = Form(None),
    listing_kind: Optional[str] = Form(None),
    files: Optional[List[UploadFile]] = File(None),
    video: Optional[UploadFile] = File(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    user, store = require_mobile_seller(authorization, session)
    if price <= 0 or initial_stock < 0:
        raise HTTPException(status_code=400, detail="Үнэ эсвэл нөөц буруу")
    category = session.get(Category, category_id)
    if not category:
        raise HTTPException(status_code=400, detail="Ангилал олдсонгүй")
    if category.slug not in PUBLIC_CATEGORY_SLUGS:
        raise HTTPException(status_code=400, detail="Зөвхөн дугуй, обуд оруулна")
    kind = _normalize_listing_kind(listing_kind, category.slug)
    if kind == "combo" and category.slug != "dugui":
        dugui = session.exec(select(Category).where(Category.slug == "dugui")).first()
        if not dugui:
            raise HTTPException(status_code=400, detail="Дугуй ангилал олдсонгүй")
        category = dugui
    normalized_sku = (sku or "").strip() or None
    if normalized_sku and session.exec(
        select(Product).where(
            Product.store_id == store.id, Product.sku == normalized_sku
        )
    ).first():
        raise HTTPException(status_code=409, detail="Энэ SKU бүртгэлтэй байна")
    images = await save_uploads(files)
    silent_video = await save_silent_video(video)
    final_title = (title or "").strip()
    if not final_title:
        if kind == "combo":
            dim = f" {width}/{ratio}" if width and ratio else ""
            final_title = f"{(brand or '').strip() or 'Обудтай дугуй'}{dim} R{diameter or ''}".strip()
        elif kind == "obud" or category.slug == "obud":
            final_title = f"{(brand or '').strip() or 'Обуд'} R{diameter or ''} {bolt_pattern or ''}".strip()
        else:
            dim = f" {width}/{ratio}" if width and ratio else ""
            final_title = f"{(brand or '').strip() or 'Дугуй'}{dim} R{diameter or ''}".strip()
    final_description = (description or "").strip() or None
    if not final_description:
        bits = [final_title]
        if condition:
            bits.append(condition)
        if (brand or "").strip():
            bits.append(brand.strip())
        final_description = " · ".join(bits)
    product = Product(
        store_id=store.id,
        category_id=category.id,
        title=final_title,
        price=price,
        stock=0,
        description=final_description,
        images=json.dumps(images),
        video=silent_video,
        sku=normalized_sku,
        barcode=(barcode or "").strip() or None,
        brand=(brand or "").strip() or None,
        condition=condition,
        pack_type=pack_type,
        width=width,
        ratio=ratio,
        diameter=diameter,
        tread_percent=tread_percent,
        bolt_pattern=bolt_pattern,
        wheel_type=wheel_type,
        car_make=(car_make or "").strip() or None,
        listing_kind=kind,
        publish_status="draft",
        is_active=True,
    )
    session.add(product)
    session.flush()
    if initial_stock:
        warehouse = session.get(Warehouse, warehouse_id) if warehouse_id else session.exec(
            select(Warehouse).where(
                Warehouse.store_id == store.id,
                Warehouse.is_default == True,
                Warehouse.is_active == True,
            )
        ).first()
        if not warehouse or warehouse.store_id != store.id:
            raise HTTPException(status_code=400, detail="Агуулах сонгоно уу")
        session.add(
            InventoryBalance(
                warehouse_id=warehouse.id,
                product_id=product.id,
                quantity=initial_stock,
            )
        )
        session.add(
            StockMovement(
                store_id=store.id,
                warehouse_id=warehouse.id,
                product_id=product.id,
                movement_type="inbound",
                quantity_delta=initial_stock,
                reason="Бараа шинээр бүртгэв",
                actor_user_id=user.id,
            )
        )
        product.stock = initial_stock
        session.add(product)
    session.commit()
    session.refresh(product)
    return product_to_dict(product, store, category)


def _run_product_moderation(
    session: Session,
    product: Product,
    store: Store,
    action: str = "automatic_check",
):
    category = session.get(Category, product.category_id)
    previous = product.publish_status
    now = datetime.now().isoformat(timespec="seconds")
    seller_ready = bool(store.phone_verified) and store.latitude is not None and store.longitude is not None
    result = moderation.evaluate_product(
        title=product.title or "",
        description=product.description or "",
        price=float(product.price or 0),
        stock=int(product.stock or 0),
        category=category.name if category else "",
        image_count=len(parse_images(product.images)),
        # Self-verify (утса + газрын зураг) is enough for marketplace publish;
        # admin is_approved remains for web/admin store listing gates.
        store_is_approved=bool(store.is_approved) or seller_ready,
    )
    product.publish_status = result.decision
    product.moderation_flags = json.dumps(result.flags, ensure_ascii=False)
    product.rejection_reason = result.reason
    product.submitted_at = now
    product.published_at = now if result.decision == "published" else None
    job = ModerationJob(
        product_id=product.id,
        status="finished",
        decision=result.decision,
        flags=product.moderation_flags,
        finished_at=now,
    )
    session.add(product)
    session.add(job)
    session.add(
        ModerationEvent(
            product_id=product.id,
            action=action,
            from_status=previous,
            to_status=result.decision,
            flags=product.moderation_flags,
            note=result.reason,
        )
    )
    return result


@app.post("/api/mobile/products/{product_id}/submit")
def api_mobile_submit_product(
    product_id: int,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    product = session.get(Product, product_id)
    if not product or product.store_id != store.id:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    has_location = store.latitude is not None and store.longitude is not None
    if not store.phone_verified or not has_location:
        raise HTTPException(
            status_code=400,
            detail="Вэб дээр зарахаас өмнө лангууны байршил, утсаа баталгаажуулна уу",
        )
    if not (product.description or "").strip():
        bits = [product.title or "Бараа"]
        if product.condition:
            bits.append(product.condition)
        if product.brand:
            bits.append(product.brand)
        product.description = " · ".join(bits)
        session.add(product)
    result = _run_product_moderation(session, product, store)
    session.commit()
    return {
        "product_id": product.id,
        "status": result.decision,
        "flags": result.flags,
        "reason": result.reason,
    }


def _order_payload(session: Session, order: Order) -> Dict[str, Any]:
    items = session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()
    store = session.get(Store, order.store_id)
    data = order.model_dump() if hasattr(order, "model_dump") else order.dict()
    data["store_name"] = store.name if store else ""
    data["items"] = [
        (i.model_dump() if hasattr(i, "model_dump") else i.dict()) for i in items
    ]
    # Don't dump huge payload to clients by default size — keep, but ok for now
    return data


def _seller_order_payload(session: Session, order: Order) -> Dict[str, Any]:
    """Seller mobile payload — includes buyer contact for fulfillment."""
    data = _order_payload(session, order)
    data["customer_label"] = (order.customer_phone or "").strip() or f"Захиалагч #{order.id}"
    data["has_delivery_address"] = bool((order.address or "").strip())
    return data


ORDER_STATUS_TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"preparing", "cancelled"},
    "preparing": {"out_for_delivery", "cancelled"},
    "out_for_delivery": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def _apply_order_status(
    session: Session,
    order: Order,
    status: str,
    cancellation_reason: Optional[str] = None,
) -> None:
    if status == order.status:
        return
    if status not in ORDER_STATUS_TRANSITIONS.get(order.status, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Захиалгыг {order.status}-с {status} төлөвт шилжүүлэх боломжгүй",
        )
    now = datetime.now().isoformat(timespec="seconds")
    if status == "confirmed":
        deadline = _parse_order_dt(order.confirmation_expires_at)
        if deadline and deadline <= datetime.now():
            _restore_inventory_for_order(session, order)
            order.status = "cancelled"
            order.cancelled_at = now
            order.cancellation_reason = "seller_confirmation_timeout"
            session.add(order)
            session.commit()
            raise HTTPException(status_code=409, detail="Баталгаажуулах хугацаа дууссан")
        _confirm_order_reservation(order)
    elif status == "cancelled":
        _restore_inventory_for_order(session, order)
        order.cancelled_at = now
        order.cancellation_reason = (
            (cancellation_reason or "").strip() or "seller_cancelled"
        )
    order.status = status
    session.add(order)


@app.get("/api/mobile/orders")
def api_mobile_orders(
    status: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    _expire_pending_orders(session)
    query = select(Order).where(Order.store_id == store.id)
    if status:
        query = query.where(Order.status == status)
    orders = session.exec(query.order_by(Order.id.desc())).all()
    return {"data": [_seller_order_payload(session, order) for order in orders]}


@app.get("/api/mobile/orders/{order_id}")
def api_mobile_order_detail(
    order_id: int,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    _expire_pending_orders(session)
    order = session.get(Order, order_id)
    if not order or order.store_id != store.id:
        raise HTTPException(status_code=404, detail="Захиалга олдсонгүй")
    return _seller_order_payload(session, order)


@app.patch("/api/mobile/orders/{order_id}/status")
def api_mobile_order_status(
    order_id: int,
    status: str = Form(...),
    cancellation_reason: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    _, store = require_mobile_seller(authorization, session)
    allowed = {"confirmed", "preparing", "out_for_delivery", "completed", "cancelled"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Захиалгын төлөв буруу")
    order = session.get(Order, order_id)
    if not order or order.store_id != store.id:
        raise HTTPException(status_code=404, detail="Захиалга олдсонгүй")
    _apply_order_status(session, order, status, cancellation_reason)
    session.commit()
    return _seller_order_payload(session, order)


# --- Cart ---
@app.get("/api/cart")
def api_cart(request: Request, session: Session = Depends(get_session)):
    filt, _, _ = cart_owner_filter(request, session)
    items = session.exec(select(CartItem).where(filt)).all()
    result = []
    total = 0.0
    for item in items:
        product = session.get(Product, item.product_id)
        if not product or not product.is_active:
            continue
        store = session.get(Store, product.store_id)
        line = {
            "id": item.id,
            "product_id": product.id,
            "quantity": item.quantity,
            "title": product.title,
            "price": product.price,
            "stock": product.stock,
            "images": parse_images(product.images),
            "store_id": product.store_id,
            "store_name": store.name if store else "",
            "line_total": product.price * item.quantity,
        }
        total += line["line_total"]
        result.append(line)
    return {"items": result, "total": total}


@app.post("/api/cart")
def api_cart_add(
    request: Request,
    product_id: int = Form(...),
    quantity: int = Form(1),
    session: Session = Depends(get_session),
):
    if quantity < 1:
        raise HTTPException(status_code=400, detail="Тоо ширхэг буруу")
    product = session.get(Product, product_id)
    if not product or not product.is_active:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    if product.stock < quantity:
        raise HTTPException(status_code=400, detail="Нөөц хүрэлцэхгүй байна")

    filt, user, token = cart_owner_filter(request, session)
    existing = session.exec(
        select(CartItem).where(filt, CartItem.product_id == product_id)
    ).first()
    if existing:
        existing.quantity += quantity
        if existing.quantity > product.stock:
            raise HTTPException(status_code=400, detail="Нөөц хүрэлцэхгүй байна")
        session.add(existing)
    else:
        session.add(
            CartItem(
                user_id=user.id if user else None,
                guest_token=None if user else token,
                product_id=product_id,
                quantity=quantity,
            )
        )
    session.commit()
    return {"ok": True}


@app.post("/api/cart/update")
def api_cart_update(
    request: Request,
    item_id: int = Form(...),
    quantity: int = Form(...),
    session: Session = Depends(get_session),
):
    filt, _, _ = cart_owner_filter(request, session)
    item = session.exec(select(CartItem).where(CartItem.id == item_id).where(filt)).first()
    if not item:
        raise HTTPException(status_code=404, detail="Сагсны мөр олдсонгүй")
    if quantity <= 0:
        session.delete(item)
    else:
        product = session.get(Product, item.product_id)
        if product and quantity > product.stock:
            raise HTTPException(status_code=400, detail="Нөөц хүрэлцэхгүй байна")
        item.quantity = quantity
        session.add(item)
    session.commit()
    return {"ok": True}


@app.post("/api/cart/remove")
def api_cart_remove(
    request: Request,
    item_id: int = Form(...),
    session: Session = Depends(get_session),
):
    filt, _, _ = cart_owner_filter(request, session)
    item = session.exec(select(CartItem).where(CartItem.id == item_id).where(filt)).first()
    if item:
        session.delete(item)
        session.commit()
    return {"ok": True}


# --- Orders ---
@app.post("/api/orders")
def api_create_order(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    delivery_type: str = Form("delivery"),
    payment_method: str = Form("cod"),
    address: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    ebarimt_receiver_type: str = Form("CITIZEN"),
    ebarimt_receiver: Optional[str] = Form(None),
    ebarimt_district_code: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    if not validate_phone(customer_phone):
        raise HTTPException(status_code=400, detail="Утасны дугаар буруу")
    if delivery_type not in ("delivery", "pickup"):
        raise HTTPException(status_code=400, detail="Хүргэлтийн төрөл буруу")
    if payment_method not in ("cod", "qpay"):
        raise HTTPException(status_code=400, detail="Төлбөрийн хэлбэр буруу")
    if delivery_type == "delivery" and not (address or "").strip():
        raise HTTPException(status_code=400, detail="Хүргэлтийн хаяг оруулна уу")

    receiver_type = qpay_client.normalize_receiver_type(ebarimt_receiver_type)
    receiver_value = (ebarimt_receiver or "").strip()
    if payment_method == "qpay" and receiver_type == "COMPANY" and not receiver_value:
        raise HTTPException(status_code=400, detail="Байгууллагын регистр / ТТД оруулна уу")

    district = (ebarimt_district_code or qpay_client.EBARIMT_DISTRICT_CODE or "").strip() or None

    filt, user, _ = cart_owner_filter(request, session)
    cart_items = session.exec(select(CartItem).where(filt)).all()
    if not cart_items:
        raise HTTPException(status_code=400, detail="Сагс хоосон байна")

    by_store: Dict[int, List[CartItem]] = {}
    line_snapshots: List[Dict[str, Any]] = []
    for item in cart_items:
        product = session.get(Product, item.product_id)
        if not product or not product.is_active or product.stock < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Нөөц хүрэлцэхгүй: {product.title if product else item.product_id}",
            )
        by_store.setdefault(product.store_id, []).append(item)
        line_snapshots.append(
            {
                "tax_product_code": qpay_client.EBARIMT_TAX_PRODUCT_CODE or "00000000",
                "line_description": (product.title or "Бараа")[:100],
                "line_quantity": str(item.quantity),
                "line_unit_price": str(int(round(product.price))),
                "classification_code": qpay_client.EBARIMT_CLASSIFICATION_CODE or "",
            }
        )

    payment_group_id = str(uuid.uuid4()) if payment_method == "qpay" else None
    confirmation_expires_at = (
        datetime.now() + timedelta(minutes=ORDER_CONFIRM_MINUTES)
    ).isoformat(timespec="seconds")
    created_orders: List[int] = []
    grand_total = 0.0
    product_order_map: Dict[int, int] = {}  # product_id -> order_id

    try:
        for store_id, items in by_store.items():
            total = 0.0
            order = Order(
                store_id=store_id,
                user_id=user.id if user else None,
                customer_name=customer_name.strip(),
                customer_phone=customer_phone.strip(),
                delivery_type=delivery_type,
                payment_method=payment_method,
                payment_status="unpaid",
                address=(address or "").strip() or None,
                note=(note or "").strip() or None,
                status="pending",
                inventory_status="reserved",
                confirmation_expires_at=confirmation_expires_at,
                total=0,
                payment_group_id=payment_group_id,
                ebarimt_receiver_type=receiver_type if payment_method == "qpay" else None,
                ebarimt_receiver=(receiver_value or None) if payment_method == "qpay" else None,
                ebarimt_district_code=district if payment_method == "qpay" else None,
                ebarimt_status=None,
            )
            session.add(order)
            session.commit()
            session.refresh(order)

            for item in items:
                product = session.get(Product, item.product_id)
                line_total = product.price * item.quantity
                total += line_total
                session.add(
                    OrderItem(
                        order_id=order.id,
                        product_id=product.id,
                        title=product.title,
                        price=product.price,
                        quantity=item.quantity,
                    )
                )
                product_order_map[product.id] = order.id
                if payment_method == "cod":
                    if not _deduct_inventory_for_sale(
                        session,
                        product,
                        item.quantity,
                        order.id,
                        user.id if user else None,
                    ):
                        product.stock -= item.quantity
                        session.add(product)
                    session.delete(item)

            order.total = total
            grand_total += total
            session.add(order)
            session.commit()
            session.refresh(order)
            created_orders.append(order.id)
            if payment_method == "cod":
                store = session.get(Store, store_id)
                if store:
                    _notify_store_new_order(session, store, order)

        payment_info = None
        if payment_method == "qpay" and payment_group_id:
            try:
                invoice = qpay_client.create_invoice(
                    sender_invoice_no=payment_group_id,
                    amount=grand_total,
                    description=f"ЗАМЧ захиалга #{','.join(map(str, created_orders))}",
                    callback_url=qpay_client.callback_url_for(payment_group_id),
                    lines=line_snapshots,
                    district_code=district,
                )
            except Exception as exc:
                _delete_orders_cascade(session, created_orders)
                raise HTTPException(status_code=502, detail=f"QPay алдаа: {exc}") from exc

            invoice_id = invoice.get("invoice_id")
            orders = session.exec(
                select(Order).where(Order.payment_group_id == payment_group_id)
            ).all()
            for order in orders:
                order.qpay_invoice_id = invoice_id
                session.add(order)

            # Invoice OK — reserve stock and clear cart
            for item in list(cart_items):
                product = session.get(Product, item.product_id)
                order_id = product_order_map.get(item.product_id) or (
                    orders[0].id if orders else 0
                )
                if product:
                    if not _deduct_inventory_for_sale(
                        session,
                        product,
                        item.quantity,
                        order_id,
                        user.id if user else None,
                    ):
                        product.stock -= item.quantity
                        session.add(product)
                session.delete(item)
            session.commit()

            for order in orders:
                store = session.get(Store, order.store_id)
                if store:
                    _notify_store_new_order(session, store, order)

            payment_info = {
                "payment_group_id": payment_group_id,
                "invoice_id": invoice_id,
                "amount": grand_total,
                "qr_image": invoice.get("qr_image"),
                "qr_text": invoice.get("qr_text"),
                "short_url": invoice.get("qPay_shortUrl")
                or f"/payment/{payment_group_id}",
                "urls": invoice.get("urls") or [],
                "mock": bool(invoice.get("mock")),
                "ebarimt_receiver_type": receiver_type,
            }
    except HTTPException:
        raise
    except Exception:
        if created_orders and payment_method == "qpay":
            _delete_orders_cascade(session, created_orders)
        raise

    request.session["last_checkout"] = {
        "order_ids": created_orders,
        "payment_method": payment_method,
        "payment_group_id": payment_group_id,
        "total": grand_total,
        "store_count": len(by_store),
        "delivery_type": delivery_type,
        "payment": payment_info,
    }

    return {
        "ok": True,
        "order_ids": created_orders,
        "payment_method": payment_method,
        "store_count": len(by_store),
        "total": grand_total,
        "payment": payment_info,
    }


@app.get("/api/orders/last-checkout")
def api_last_checkout(request: Request, session: Session = Depends(get_session)):
    data = request.session.get("last_checkout")
    if not data:
        return {"ok": False, "data": None}
    group_id = data.get("payment_group_id")
    if group_id and data.get("payment_method") == "qpay":
        orders = session.exec(select(Order).where(Order.payment_group_id == group_id)).all()
        if orders and all(o.payment_status == "paid" for o in orders):
            data = {**data, "payment_status": "paid"}
            request.session["last_checkout"] = data
        elif orders:
            data = {**data, "payment_status": "unpaid"}
    return {"ok": True, "data": data}


@app.get("/api/qpay/resume/{payment_group_id}")
def api_qpay_resume(
    payment_group_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    orders = session.exec(
        select(Order).where(Order.payment_group_id == payment_group_id)
    ).all()
    if not orders:
        raise HTTPException(status_code=404, detail="Төлбөрийн бүлэг олдсонгүй")
    paid = all(o.payment_status == "paid" for o in orders)
    amount = sum(float(o.total or 0) for o in orders)
    last = request.session.get("last_checkout") or {}
    payment = None
    if last.get("payment_group_id") == payment_group_id and last.get("payment"):
        payment = last["payment"]
    if not payment:
        payment = {
            "payment_group_id": payment_group_id,
            "amount": amount,
            "qr_image": None,
            "qr_text": None,
            "short_url": f"/payment/{payment_group_id}",
            "urls": [],
            "mock": any(str(o.qpay_invoice_id or "").startswith("mock-") for o in orders),
        }
    return {
        "ok": True,
        "paid": paid,
        "amount": amount,
        "order_ids": [o.id for o in orders],
        "payment": payment,
    }


@app.get("/api/orders/mine")
def api_my_orders(request: Request, session: Session = Depends(get_session)):
    user = require_user(request, session)
    orders = session.exec(
        select(Order).where(Order.user_id == user.id).order_by(Order.id.desc())
    ).all()
    return {"data": [_order_payload(session, o) for o in orders]}


@app.get("/api/seller/orders")
def api_seller_orders(request: Request, session: Session = Depends(get_session)):
    user = require_user(request, session)
    store = get_user_store(session, user.id)
    if not store:
        raise HTTPException(status_code=400, detail="Дэлгүүр олдсонгүй")
    orders = session.exec(
        select(Order).where(Order.store_id == store.id).order_by(Order.id.desc())
    ).all()
    return {"data": [_order_payload(session, o) for o in orders]}


@app.post("/api/seller/orders/{order_id}/status")
def api_seller_order_status(
    order_id: int,
    request: Request,
    status: str = Form(...),
    cancellation_reason: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    allowed = {"pending", "confirmed", "preparing", "out_for_delivery", "completed", "cancelled"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Статус буруу")
    user = require_user(request, session)
    store = get_user_store(session, user.id)
    if not store:
        raise HTTPException(status_code=400, detail="Дэлгүүр олдсонгүй")
    order = session.get(Order, order_id)
    if not order or order.store_id != store.id:
        raise HTTPException(status_code=404, detail="Захиалга олдсонгүй")
    _apply_order_status(session, order, status, cancellation_reason)
    session.commit()

    # Хүргэлттэй захиалга confirmed/preparing болоход delivery job бэлтгэнэ (app биш — зөвхөн холболт)
    if order.delivery_type == "delivery" and status in ("confirmed", "preparing"):
        try:
            _ensure_delivery_shipment(session, order)
        except Exception:
            pass

    return _order_payload(session, order)


def _issue_ebarimt_for_group(
    session: Session,
    payment_group_id: str,
    payment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create eBarimt once per payment group after paid."""
    orders = session.exec(
        select(Order).where(Order.payment_group_id == payment_group_id)
    ).all()
    if not orders:
        return {"ok": False, "detail": "orders missing"}

    # Already created
    if any(o.ebarimt_status == "created" for o in orders):
        first = next(o for o in orders if o.ebarimt_status == "created")
        return {
            "ok": True,
            "status": "created",
            "lottery": first.ebarimt_lottery,
            "qr": first.ebarimt_qr,
            "payment_id": first.qpay_payment_id,
        }

    pay_id = payment_id or orders[0].qpay_payment_id
    if not pay_id:
        # mock payment id when simulating
        if qpay_client.is_mock_mode() or str(orders[0].qpay_invoice_id or "").startswith("mock-"):
            pay_id = f"mock-pay-{payment_group_id[:8]}"
        else:
            return {"ok": False, "detail": "payment_id missing"}

    receiver_type = orders[0].ebarimt_receiver_type or "CITIZEN"
    receiver = orders[0].ebarimt_receiver or ""
    district = orders[0].ebarimt_district_code or qpay_client.EBARIMT_DISTRICT_CODE

    try:
        receipt = qpay_client.create_ebarimt(
            payment_id=pay_id,
            receiver_type=receiver_type,
            receiver=receiver,
            district_code=district,
        )
    except Exception as exc:
        for order in orders:
            order.qpay_payment_id = pay_id
            order.ebarimt_status = "failed"
            order.ebarimt_payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
            session.add(order)
        session.commit()
        return {"ok": False, "status": "failed", "detail": str(exc)}

    if receipt.get("skipped"):
        for order in orders:
            order.qpay_payment_id = pay_id
            order.ebarimt_status = "skipped"
            order.ebarimt_payload = json.dumps(receipt, ensure_ascii=False)
            session.add(order)
        session.commit()
        return {"ok": True, "status": "skipped"}

    lottery = receipt.get("ebarimt_lottery") or receipt.get("lottery")
    qr = receipt.get("ebarimt_qr_data") or receipt.get("ebarimt_qr") or receipt.get("qr_data")
    payload = json.dumps(receipt, ensure_ascii=False)

    for order in orders:
        order.qpay_payment_id = pay_id
        order.ebarimt_status = "created"
        order.ebarimt_lottery = lottery
        order.ebarimt_qr = qr
        order.ebarimt_payload = payload
        session.add(order)
    session.commit()

    return {
        "ok": True,
        "status": "created",
        "lottery": lottery,
        "qr": qr,
        "payment_id": pay_id,
        "mock": bool(receipt.get("mock")),
    }


def _mark_payment_group_paid(
    session: Session,
    payment_group_id: str,
    payment_id: Optional[str] = None,
) -> Dict[str, Any]:
    orders = session.exec(
        select(Order).where(Order.payment_group_id == payment_group_id)
    ).all()
    for order in orders:
        if order.payment_status != "paid":
            order.payment_status = "paid"
            if order.status == "pending":
                order.status = "confirmed"
        if payment_id:
            order.qpay_payment_id = payment_id
        session.add(order)
    session.commit()

    ebarimt = _issue_ebarimt_for_group(session, payment_group_id, payment_id=payment_id)

    # QPay төлөгдсөн хүргэлттэй захиалгыг delivery layer рүү бэлтгэнэ
    for order in orders:
        if order.delivery_type == "delivery":
            try:
                _ensure_delivery_shipment(session, order)
            except Exception:
                pass

    return {"orders": orders, "ebarimt": ebarimt}


# --- QPay / eBarimt ---
@app.get("/api/qpay/status")
def api_qpay_status():
    live = qpay_client.qpay_configured()
    return {
        "configured": live,
        "mock": qpay_client.is_mock_mode(),
        "ready": live or qpay_client.is_mock_mode(),
        "mode": "live" if live else "mock",
        "has_credentials": bool(
            qpay_client.QPAY_USERNAME
            and qpay_client.QPAY_PASSWORD
            and qpay_client.QPAY_INVOICE_CODE
        ),
        "ebarimt_enabled": qpay_client.EBARIMT_ENABLED,
        "district_code": qpay_client.EBARIMT_DISTRICT_CODE,
        "base_url": qpay_client.QPAY_BASE_URL,
        "hint": (
            None
            if live
            else "QPAY_USERNAME / QPAY_PASSWORD / QPAY_INVOICE_CODE бөглөөд сервер restart хийнэ"
        ),
    }


@app.get("/api/qpay/check/{payment_group_id}")
def api_qpay_check(payment_group_id: str, session: Session = Depends(get_session)):
    orders = session.exec(
        select(Order).where(Order.payment_group_id == payment_group_id)
    ).all()
    if not orders:
        raise HTTPException(status_code=404, detail="Төлбөрийн бүлэг олдсонгүй")

    if all(o.payment_status == "paid" for o in orders):
        return {
            "paid": True,
            "payment_status": "paid",
            "order_ids": [o.id for o in orders],
            "amount": sum(o.total for o in orders),
            "ebarimt_status": orders[0].ebarimt_status,
            "ebarimt_lottery": orders[0].ebarimt_lottery,
        }

    invoice_id = orders[0].qpay_invoice_id
    if not invoice_id:
        raise HTTPException(status_code=400, detail="QPay invoice байхгүй")

    try:
        result = qpay_client.check_invoice_paid(invoice_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"QPay шалгалт амжилтгүй: {exc}") from exc

    if result.get("paid"):
        marked = _mark_payment_group_paid(
            session, payment_group_id, payment_id=result.get("payment_id")
        )
        ebarimt = marked.get("ebarimt") or {}
        return {
            "paid": True,
            "payment_status": "paid",
            "order_ids": [o.id for o in orders],
            "amount": sum(o.total for o in orders),
            "paid_amount": result.get("paid_amount"),
            "ebarimt_status": ebarimt.get("status"),
            "ebarimt_lottery": ebarimt.get("lottery"),
        }

    return {
        "paid": False,
        "payment_status": "unpaid",
        "order_ids": [o.id for o in orders],
        "amount": sum(o.total for o in orders),
        "invoice_id": invoice_id,
        "mock": result.get("mock", False),
    }


@app.api_route("/api/qpay/callback", methods=["GET", "POST"])
async def api_qpay_callback(
    request: Request,
    payment_group_id: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    """QPay webhook — check payment then create eBarimt (no cron polling)."""
    if not payment_group_id:
        try:
            body = await request.json()
            payment_group_id = (
                body.get("payment_group_id")
                or body.get("sender_invoice_no")
                or body.get("object_id")
            )
        except Exception:
            payment_group_id = None

    if not payment_group_id:
        raise HTTPException(status_code=400, detail="payment_group_id шаардлагатай")

    orders = session.exec(
        select(Order).where(Order.payment_group_id == payment_group_id)
    ).all()
    if not orders:
        raise HTTPException(status_code=404, detail="Захиалга олдсонгүй")

    invoice_id = orders[0].qpay_invoice_id
    if invoice_id and not str(invoice_id).startswith("mock-"):
        try:
            result = qpay_client.check_invoice_paid(invoice_id)
            if result.get("paid"):
                _mark_payment_group_paid(
                    session, payment_group_id, payment_id=result.get("payment_id")
                )
        except Exception:
            pass

    return {"ok": True}


@app.post("/api/qpay/mock-pay/{payment_group_id}")
def api_qpay_mock_pay(payment_group_id: str, session: Session = Depends(get_session)):
    """Dev-only: simulate successful QPay payment + eBarimt."""
    orders = session.exec(
        select(Order).where(Order.payment_group_id == payment_group_id)
    ).all()
    if not orders:
        raise HTTPException(status_code=404, detail="Төлбөрийн бүлэг олдсонгүй")
    invoice_id = orders[0].qpay_invoice_id or ""
    allow_mock = qpay_client.QPAY_MOCK or str(invoice_id).startswith("mock-") or qpay_client.is_mock_mode()
    if not allow_mock:
        raise HTTPException(status_code=403, detail="Зөвхөн mock горимд боломжтой")
    marked = _mark_payment_group_paid(
        session,
        payment_group_id,
        payment_id=f"mock-pay-{payment_group_id[:8]}",
    )
    return {
        "ok": True,
        "paid": True,
        "order_ids": [o.id for o in orders],
        "ebarimt": marked.get("ebarimt"),
    }


@app.post("/api/ebarimt/cancel/{payment_group_id}")
def api_ebarimt_cancel(
    payment_group_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """Cancel eBarimt for a paid group (refund flow). Seller/admin use."""
    user = require_user(request, session)
    orders = session.exec(
        select(Order).where(Order.payment_group_id == payment_group_id)
    ).all()
    if not orders:
        raise HTTPException(status_code=404, detail="Захиалга олдсонгүй")

    # Allow store owner of any order in group, or the buyer
    store = get_user_store(session, user.id)
    allowed = False
    if store and any(o.store_id == store.id for o in orders):
        allowed = True
    if any(o.user_id == user.id for o in orders):
        allowed = True
    if not allowed:
        raise HTTPException(status_code=403, detail="Эрх хүрэхгүй")

    payment_id = orders[0].qpay_payment_id
    if not payment_id:
        raise HTTPException(status_code=400, detail="payment_id байхгүй")

    try:
        qpay_client.cancel_ebarimt(payment_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"eBarimt цуцлах амжилтгүй: {exc}") from exc

    for order in orders:
        order.ebarimt_status = "cancelled"
        session.add(order)
    session.commit()
    return {"ok": True, "ebarimt_status": "cancelled"}


@app.get("/payment/{payment_group_id}")
def page_payment(request: Request, payment_group_id: str):
    return templates.TemplateResponse(
        "payment.html",
        {"request": request, "payment_group_id": payment_group_id},
    )


# --- Delivery integration (prep only: own_app / partner / manual are separate) ---
def _shipment_dict(shipment: DeliveryShipment) -> Dict[str, Any]:
    data = shipment.model_dump() if hasattr(shipment, "model_dump") else shipment.dict()
    return data


def _ensure_delivery_shipment(
    session: Session,
    order: Order,
    provider_name: Optional[str] = None,
    force_dispatch: bool = False,
) -> DeliveryShipment:
    existing = session.exec(
        select(DeliveryShipment).where(DeliveryShipment.order_id == order.id)
    ).first()
    if existing and not force_dispatch:
        return existing

    store = session.get(Store, order.store_id)
    items = session.exec(select(OrderItem).where(OrderItem.order_id == order.id)).all()
    summary = ", ".join(f"{i.title}×{i.quantity}" for i in items)[:240]
    cod_amount = order.total if (order.payment_method == "cod" and order.payment_status != "paid") else 0

    provider_key = provider_name or delivery_providers.active_provider_name()
    shipment = existing or DeliveryShipment(
        order_id=order.id,
        store_id=order.store_id,
        provider=provider_key,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone,
        dropoff_address=order.address,
        note=order.note,
        items_summary=summary,
        order_total=order.total,
        cod_amount=cod_amount,
        pickup_name=store.name if store else None,
        pickup_phone=store.phone if store else None,
        pickup_location=store.location if store else None,
        pickup_address=store.location if store else None,
        status="pending",
    )
    if existing:
        shipment.provider = provider_key
        shipment.items_summary = summary
        shipment.cod_amount = cod_amount
        shipment.dropoff_address = order.address

    session.add(shipment)
    session.commit()
    session.refresh(shipment)

    # Push/queue to selected provider (separate adapters)
    provider = delivery_providers.get_provider(provider_key)
    ship_data = _shipment_dict(shipment)
    shipment.provider_payload = json.dumps(
        delivery_providers.build_job_payload(ship_data), ensure_ascii=False
    )
    try:
        result = provider.create_job(ship_data)
        shipment.provider_job_id = result.get("provider_job_id")
        shipment.provider_response = json.dumps(result, ensure_ascii=False)
        mode = result.get("mode")
        if mode == "manual":
            shipment.status = "pending"
        elif mode == "queued_local":
            shipment.status = "queued"
        elif mode == "pushed":
            shipment.status = "assigned"
        else:
            shipment.status = "queued"
        shipment.last_event_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    except Exception as exc:
        shipment.status = "failed"
        shipment.provider_response = json.dumps({"error": str(exc)}, ensure_ascii=False)
        shipment.last_event_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    session.add(shipment)
    session.commit()
    session.refresh(shipment)

    # Mirror coarse status onto order for UI
    if shipment.status in ("assigned", "queued") and order.status in ("pending", "confirmed", "preparing"):
        if shipment.status == "assigned":
            order.status = "out_for_delivery"
            session.add(order)
            session.commit()

    return shipment


@app.get("/api/delivery/status")
def api_delivery_status():
    return delivery_providers.provider_status()


@app.get("/api/delivery/shipments/{order_id}")
def api_delivery_get(order_id: int, session: Session = Depends(get_session)):
    shipment = session.exec(
        select(DeliveryShipment).where(DeliveryShipment.order_id == order_id)
    ).first()
    if not shipment:
        raise HTTPException(status_code=404, detail="Хүргэлтийн бүртгэл олдсонгүй")
    return _shipment_dict(shipment)


@app.post("/api/seller/orders/{order_id}/dispatch")
def api_seller_dispatch(
    order_id: int,
    request: Request,
    provider: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    """Дэлгүүр захиалгыг хүргэлтийн layer рүү илгээнэ (own_app / partner / manual)."""
    user = require_user(request, session)
    store = get_user_store(session, user.id)
    if not store:
        raise HTTPException(status_code=400, detail="Дэлгүүр олдсонгүй")
    order = session.get(Order, order_id)
    if not order or order.store_id != store.id:
        raise HTTPException(status_code=404, detail="Захиалга олдсонгүй")
    if order.delivery_type != "delivery":
        raise HTTPException(status_code=400, detail="Энэ захиалга pickup — хүргэлт биш")

    if provider and provider not in ("manual", "own_app", "partner"):
        raise HTTPException(status_code=400, detail="Provider буруу (manual|own_app|partner)")

    shipment = _ensure_delivery_shipment(
        session, order, provider_name=provider, force_dispatch=True
    )
    return _shipment_dict(shipment)


@app.post("/api/delivery/webhook")
async def api_delivery_webhook(request: Request, session: Session = Depends(get_session)):
    """
    Own app эсвэл partner-аас ирэх статус шинэчлэлт.
    Header: X-Delivery-Secret
    Body JSON: { external_ref|provider_job_id|order_id, status, provider? }
    """
    secret = request.headers.get("X-Delivery-Secret") or request.headers.get("x-delivery-secret")
    if secret != delivery_providers.DELIVERY_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Webhook secret буруу")

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON body шаардлагатай") from exc

    status = str(body.get("status") or "").strip().lower()
    allowed = {
        "pending", "queued", "assigned", "picked_up", "in_transit",
        "delivered", "cancelled", "failed",
    }
    if status not in allowed:
        raise HTTPException(status_code=400, detail="status буруу")

    shipment = None
    if body.get("external_ref"):
        shipment = session.exec(
            select(DeliveryShipment).where(DeliveryShipment.external_ref == body["external_ref"])
        ).first()
    if not shipment and body.get("provider_job_id"):
        shipment = session.exec(
            select(DeliveryShipment).where(
                DeliveryShipment.provider_job_id == str(body["provider_job_id"])
            )
        ).first()
    if not shipment and body.get("order_id"):
        shipment = session.exec(
            select(DeliveryShipment).where(DeliveryShipment.order_id == int(body["order_id"]))
        ).first()
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment олдсонгүй")

    shipment.status = status
    shipment.last_event_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    if body.get("provider_job_id"):
        shipment.provider_job_id = str(body["provider_job_id"])
    session.add(shipment)

    order = session.get(Order, shipment.order_id)
    if order:
        if status in ("assigned", "picked_up", "in_transit"):
            order.status = "out_for_delivery"
        elif status == "delivered":
            order.status = "completed"
            if order.payment_method == "cod":
                order.payment_status = "paid"
        elif status == "cancelled":
            order.status = "cancelled"
        session.add(order)

    session.commit()
    return {"ok": True, "shipment_id": shipment.id, "status": shipment.status}


@app.get("/api/seller/shipments")
def api_seller_shipments(request: Request, session: Session = Depends(get_session)):
    user = require_user(request, session)
    store = get_user_store(session, user.id)
    if not store:
        raise HTTPException(status_code=400, detail="Дэлгүүр олдсонгүй")
    rows = session.exec(
        select(DeliveryShipment)
        .where(DeliveryShipment.store_id == store.id)
        .order_by(DeliveryShipment.id.desc())
    ).all()
    return {"data": [_shipment_dict(s) for s in rows]}


# --- Admin ---
def _slugify(text: str) -> str:
    raw = re.sub(r"\s+", "-", (text or "").strip().lower())
    raw = re.sub(r"[^a-z0-9\-а-яөүё]", "", raw, flags=re.IGNORECASE)
    return raw[:60] or f"cat-{uuid.uuid4().hex[:6]}"


@app.get("/api/admin/stats")
def api_admin_stats(request: Request, session: Session = Depends(get_session)):
    require_admin(request, session)
    users = session.exec(select(User)).all()
    stores = session.exec(select(Store)).all()
    products = session.exec(select(Product)).all()
    orders = session.exec(select(Order)).all()
    shipments = session.exec(select(DeliveryShipment)).all()
    revenue_paid = sum(o.total for o in orders if o.payment_status == "paid")
    qpay_live = qpay_client.qpay_configured()
    return {
        "users": len(users),
        "buyers": sum(1 for u in users if u.role == "buyer"),
        "sellers": sum(1 for u in users if u.role in ("seller", "admin")),
        "stores": len(stores),
        "stores_pending": sum(1 for s in stores if not s.is_approved),
        "stores_active": sum(1 for s in stores if s.is_approved and s.is_active),
        "products": len(products),
        "products_active": sum(1 for p in products if p.is_active),
        "orders": len(orders),
        "orders_pending": sum(1 for o in orders if o.status == "pending"),
        "revenue_paid": revenue_paid,
        "shipments": len(shipments),
        "delivery": delivery_providers.provider_status(),
        "qpay_mock": qpay_client.is_mock_mode(),
        "qpay": {
            "mode": "live" if qpay_live else "mock",
            "configured": qpay_live,
            "ebarimt_enabled": qpay_client.EBARIMT_ENABLED,
        },
    }


@app.get("/api/admin/stores")
def api_admin_stores(request: Request, session: Session = Depends(get_session)):
    require_admin(request, session)
    stores = session.exec(select(Store).order_by(Store.id.desc())).all()
    owners = {u.id: u for u in session.exec(select(User)).all()}
    data = []
    for s in stores:
        row = s.model_dump() if hasattr(s, "model_dump") else s.dict()
        owner = owners.get(s.owner_id)
        row["owner_name"] = owner.name if owner else None
        row["owner_phone"] = owner.phone if owner else None
        data.append(row)
    return {"data": data}


@app.post("/api/admin/stores/{store_id}/approve")
def api_admin_approve_store(
    store_id: int,
    request: Request,
    approved: str = Form("1"),
    session: Session = Depends(get_session),
):
    require_admin(request, session)
    store = session.get(Store, store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Дэлгүүр олдсонгүй")
    store.is_approved = approved in ("1", "true", "on", "yes")
    session.add(store)
    newly_published: List[Product] = []
    if store.is_approved:
        pending_products = session.exec(
            select(Product).where(
                Product.store_id == store.id,
                Product.publish_status == "pending_review",
            )
        ).all()
        for product in pending_products:
            result = _run_product_moderation(
                session, product, store, action="store_approval_recheck"
            )
            if result.decision == "published":
                newly_published.append(product)
    session.commit()
    if store.is_approved:
        _notify_user(
            session,
            store.owner_id,
            "Дэлгүүр баталгаажлаа",
            f"{store.name} marketplace дээр ажиллахад бэлэн боллоо.",
            {"store_id": store.id, "type": "store_approved"},
        )
        for product in newly_published:
            _notify_store_product(
                session,
                product,
                "Бараа нийтлэгдлээ",
                f"{product.title} ЗАМЧ marketplace дээр гарлаа.",
                "product_published",
            )
    return store.model_dump() if hasattr(store, "model_dump") else store.dict()


@app.post("/api/admin/stores/{store_id}/active")
def api_admin_store_active(
    store_id: int,
    request: Request,
    active: str = Form("1"),
    session: Session = Depends(get_session),
):
    require_admin(request, session)
    store = session.get(Store, store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Дэлгүүр олдсонгүй")
    store.is_active = active in ("1", "true", "on", "yes")
    session.add(store)
    session.commit()
    return store.model_dump() if hasattr(store, "model_dump") else store.dict()


@app.get("/api/admin/categories")
def api_admin_categories(request: Request, session: Session = Depends(get_session)):
    require_admin(request, session)
    cats = session.exec(select(Category).order_by(Category.id)).all()
    return {"data": [c.model_dump() if hasattr(c, "model_dump") else c.dict() for c in cats]}


@app.post("/api/admin/categories")
def api_admin_create_category(
    request: Request,
    name: str = Form(...),
    slug: Optional[str] = Form(None),
    icon: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    require_admin(request, session)
    final_slug = (slug or "").strip() or _slugify(name)
    exists = session.exec(select(Category).where(Category.slug == final_slug)).first()
    if exists:
        raise HTTPException(status_code=400, detail="Slug давхцаж байна")
    cat = Category(name=name.strip(), slug=final_slug, icon=(icon or "bi-grid").strip())
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat.model_dump() if hasattr(cat, "model_dump") else cat.dict()


@app.post("/api/admin/categories/{category_id}")
def api_admin_update_category(
    category_id: int,
    request: Request,
    name: Optional[str] = Form(None),
    slug: Optional[str] = Form(None),
    icon: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    require_admin(request, session)
    cat = session.get(Category, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Ангилал олдсонгүй")
    if name and name.strip():
        cat.name = name.strip()
    if slug and slug.strip():
        cat.slug = slug.strip()
    if icon is not None:
        cat.icon = icon.strip() or None
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat.model_dump() if hasattr(cat, "model_dump") else cat.dict()


@app.post("/api/admin/categories/{category_id}/delete")
def api_admin_delete_category(
    category_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    require_admin(request, session)
    cat = session.get(Category, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Ангилал олдсонгүй")
    used = session.exec(select(Product).where(Product.category_id == category_id)).first()
    if used:
        raise HTTPException(status_code=400, detail="Энэ ангилалд бараа байгаа тул устгах боломжгүй")
    session.delete(cat)
    session.commit()
    return {"ok": True}


@app.get("/api/admin/orders")
def api_admin_orders(request: Request, session: Session = Depends(get_session)):
    require_admin(request, session)
    orders = session.exec(select(Order).order_by(Order.id.desc()).limit(200)).all()
    return {"data": [_order_payload(session, o) for o in orders]}


@app.post("/api/admin/orders/{order_id}/status")
def api_admin_order_status(
    order_id: int,
    request: Request,
    status: str = Form(...),
    cancellation_reason: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    require_admin(request, session)
    allowed = {"pending", "confirmed", "preparing", "out_for_delivery", "completed", "cancelled"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Статус буруу")
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Захиалга олдсонгүй")
    _apply_order_status(session, order, status, cancellation_reason)
    session.commit()
    return _order_payload(session, order)


@app.get("/api/admin/products")
def api_admin_products(request: Request, session: Session = Depends(get_session)):
    require_admin(request, session)
    products = session.exec(select(Product).order_by(Product.id.desc()).limit(200)).all()
    stores = {s.id: s for s in session.exec(select(Store)).all()}
    cats = {c.id: c for c in session.exec(select(Category)).all()}
    return {
        "data": [
            product_to_dict(p, stores.get(p.store_id), cats.get(p.category_id))
            for p in products
        ]
    }


@app.post("/api/admin/products")
async def api_admin_create_product(
    request: Request,
    store_id: int = Form(...),
    category_id: int = Form(...),
    title: str = Form(...),
    price: float = Form(...),
    stock: int = Form(1),
    brand: Optional[str] = Form(None),
    condition: str = Form("Шинэ"),
    pack_type: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    listing_kind: Optional[str] = Form(None),
    width: Optional[int] = Form(None),
    ratio: Optional[int] = Form(None),
    diameter: Optional[int] = Form(None),
    tread_percent: Optional[int] = Form(None),
    bolt_pattern: Optional[str] = Form(None),
    wheel_type: Optional[str] = Form(None),
    car_make: Optional[str] = Form(None),
    publish: str = Form("1"),
    files: Optional[List[UploadFile]] = File(None),
    session: Session = Depends(get_session),
):
    """Admin can stock marketplace without seller app (temporary ops path)."""
    require_admin(request, session)
    store = session.get(Store, store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Дэлгүүр олдсонгүй")
    category = session.get(Category, category_id)
    if not category or category.slug not in PUBLIC_CATEGORY_SLUGS:
        raise HTTPException(status_code=400, detail="Зөвхөн дугуй / обуд ангилал")
    if price <= 0:
        raise HTTPException(status_code=400, detail="Үнэ буруу")
    kind = _normalize_listing_kind(listing_kind, category.slug)

    image_paths: List[str] = []
    for f in files or []:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        name = f"{uuid.uuid4().hex}{ext}"
        dest = os.path.join("photos", name)
        with open(dest, "wb") as out:
            out.write(await f.read())
        image_paths.append(f"/photos/{name}")
    if not image_paths:
        image_paths = ["/static/placeholder.jpg"]

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    do_publish = publish in ("1", "true", "on", "yes")
    product = Product(
        store_id=store.id,
        category_id=category.id,
        title=title.strip(),
        brand=(brand or "").strip() or None,
        condition=condition.strip() or "Шинэ",
        pack_type=(pack_type or "").strip() or None,
        price=float(price),
        stock=max(0, int(stock)),
        description=(description or "").strip() or None,
        images=json.dumps(image_paths, ensure_ascii=False),
        width=width,
        ratio=ratio,
        diameter=diameter,
        tread_percent=tread_percent,
        bolt_pattern=(bolt_pattern or "").strip() or None,
        wheel_type=(wheel_type or "").strip() or None,
        car_make=(car_make or "").strip() or None,
        listing_kind=kind,
        publish_status="published" if do_publish else "draft",
        published_at=now if do_publish else None,
        submitted_at=now if do_publish else None,
        is_active=True,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product_to_dict(product, store, category)


@app.post("/api/admin/products/{product_id}/active")
def api_admin_product_active(
    product_id: int,
    request: Request,
    active: str = Form("1"),
    session: Session = Depends(get_session),
):
    require_admin(request, session)
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    product.is_active = active in ("1", "true", "on", "yes")
    session.add(product)
    session.commit()
    return product_to_dict(product)


@app.get("/api/admin/moderation")
def api_admin_moderation_queue(
    request: Request,
    status: str = Query("pending_review"),
    session: Session = Depends(get_session),
):
    require_admin(request, session)
    products = session.exec(
        select(Product)
        .where(Product.publish_status == status)
        .order_by(Product.submitted_at, Product.id)
    ).all()
    stores = {s.id: s for s in session.exec(select(Store)).all()}
    categories = {c.id: c for c in session.exec(select(Category)).all()}
    return {
        "data": [
            product_to_dict(p, stores.get(p.store_id), categories.get(p.category_id))
            for p in products
        ]
    }


@app.post("/api/admin/moderation/{product_id}/decision")
def api_admin_moderation_decision(
    product_id: int,
    request: Request,
    decision: str = Form(...),
    note: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    admin = require_admin(request, session)
    if decision not in ("published", "rejected", "suspended"):
        raise HTTPException(status_code=400, detail="Moderation шийдвэр буруу")
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Бүтээгдэхүүн олдсонгүй")
    previous = product.publish_status
    product.publish_status = decision
    product.rejection_reason = (note or "").strip() or None
    product.published_at = (
        datetime.now().strftime("%Y-%m-%d %H:%M")
        if decision == "published"
        else None
    )
    session.add(product)
    session.add(
        ModerationEvent(
            product_id=product.id,
            actor_type="admin",
            actor_id=admin.id,
            action="manual_decision",
            from_status=previous,
            to_status=decision,
            flags=product.moderation_flags,
            note=product.rejection_reason,
        )
    )
    session.commit()
    if decision == "published":
        _notify_store_product(
            session,
            product,
            "Бараа нийтлэгдлээ",
            f"{product.title} ЗАМЧ marketplace дээр гарлаа.",
            "product_published",
        )
    else:
        _notify_store_product(
            session,
            product,
            "Барааны хяналтын хариу",
            product.rejection_reason or f"{product.title} нийтлэгдээгүй.",
            "product_rejected",
        )
    return product_to_dict(product)


@app.get("/api/admin/users")
def api_admin_users(request: Request, session: Session = Depends(get_session)):
    require_admin(request, session)
    users = session.exec(select(User).order_by(User.id.desc())).all()
    data = []
    for u in users:
        data.append(
            {
                "id": u.id,
                "name": u.name,
                "phone": u.phone,
                "role": u.role,
                "created_at": u.created_at,
            }
        )
    return {"data": data}


@app.post("/api/admin/users/{user_id}/role")
def api_admin_user_role(
    user_id: int,
    request: Request,
    role: str = Form(...),
    session: Session = Depends(get_session),
):
    require_admin(request, session)
    if role not in ("buyer", "seller", "admin"):
        raise HTTPException(status_code=400, detail="Role буруу")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Хэрэглэгч олдсонгүй")
    user.role = role
    session.add(user)
    session.commit()
    return {"id": user.id, "role": user.role}


@app.get("/api/admin/shipments")
def api_admin_shipments(request: Request, session: Session = Depends(get_session)):
    require_admin(request, session)
    rows = session.exec(select(DeliveryShipment).order_by(DeliveryShipment.id.desc()).limit(200)).all()
    return {"data": [_shipment_dict(s) for s in rows]}

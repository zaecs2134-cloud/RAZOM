"""Volunteer coordination platform — educational FastAPI MVP.

Run: uvicorn main:app --reload
API docs: http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import logging
import os
import secrets
import hashlib
from collections import defaultdict, deque
from threading import Lock
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated
from urllib import request as urlrequest
import json

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text,
    create_engine, func, select, update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.exc import IntegrityError

load_dotenv()
logger = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./volunteer.db")
JWT_SECRET = os.getenv("JWT_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
if len(JWT_SECRET) < 32:
    raise RuntimeError("Set JWT_SECRET in .env to a random secret with at least 32 characters.")

sqlite_options = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=sqlite_options, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
password_hasher = PasswordHasher()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


class Base(DeclarativeBase):
    pass


class Role(str, Enum):
    applicant = "applicant"
    volunteer = "volunteer"
    admin = "admin"


class Category(str, Enum):
    food = "food"
    medicine = "medicine"
    evacuation = "evacuation"
    other = "other"


class RequestStatus(str, Enum):
    new = "new"
    assigned = "assigned"
    completed = "completed"
    cancelled = "cancelled"


class ResourceCategory(str, Enum):
    food = "food"
    medicine = "medicine"
    transport = "transport"
    other = "other"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), index=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


class HelpRequest(Base):
    __tablename__ = "help_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_volunteer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(String(100), index=True)
    address: Mapped[str] = mapped_column(String(250))  # Restricted to involved parties.
    phone: Mapped[str] = mapped_column(String(30))    # Restricted to involved parties.
    category: Mapped[str] = mapped_column(String(20), index=True)
    urgency: Mapped[int] = mapped_column(Integer)  # Self-reported, not medical triage.
    people_count: Mapped[int] = mapped_column(Integer)
    vulnerable_people: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default=RequestStatus.new.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        CheckConstraint("urgency BETWEEN 1 AND 5"),
        CheckConstraint("people_count BETWEEN 1 AND 100"),
    )


class GuestReceipt(Base):
    """A separate claim receipt; no schema change is needed for existing HelpRequest rows."""
    __tablename__ = "guest_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("help_requests.id"), unique=True, index=True)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Resource(Base):
    __tablename__ = "resources"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    category: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(100), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit: Mapped[str] = mapped_column(String(30))
    __table_args__ = (CheckConstraint("quantity >= 0"),)


class Allocation(Base):
    __tablename__ = "allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("help_requests.id"))
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id"))
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (CheckConstraint("quantity > 0"),)


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    role: Role

    @field_validator("role")
    @classmethod
    def no_admin_self_registration(cls, value: Role) -> Role:
        if value == Role.admin:
            raise ValueError("Admin accounts cannot be self-registered")
        return value


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    full_name: str
    email: EmailStr
    role: Role
    is_verified: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class HelpCreate(BaseModel):
    title: str = Field(min_length=5, max_length=120)
    description: str = Field(min_length=10, max_length=2000)
    city: str = Field(min_length=2, max_length=100)
    address: str = Field(min_length=4, max_length=250)
    phone: str = Field(min_length=7, max_length=30)
    category: Category
    urgency: int = Field(ge=1, le=5)
    people_count: int = Field(ge=1, le=100)
    vulnerable_people: bool = False


class GuestSubmit(BaseModel):
    """Simpler guest form. No street address collected at the initial stage."""
    category: Category
    city: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=10, max_length=2000)
    phone: str = Field(min_length=7, max_length=30, pattern=r"^[+0-9() .\-]+$")
    people_count: int = Field(ge=1, le=100)
    urgency: int = Field(default=3, ge=1, le=5)
    vulnerable_people: bool = False
    consent: bool
    website: str = Field(default="", max_length=100)  # Honeypot for obvious bots.

    @field_validator("consent")
    @classmethod
    def explicit_consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Consent is required to submit a demonstration request")
        return value


class GuestCreated(BaseModel):
    request_id: int
    tracking_code: str
    message: str = "Збережіть цей код. Він показується лише один раз."


class GuestLookup(BaseModel):
    request_id: int = Field(ge=1)
    tracking_code: str = Field(min_length=25, max_length=128)


class GuestStatus(BaseModel):
    request_id: int
    category: Category
    city: str
    status: RequestStatus
    needs_review: bool
    created_at: datetime


class RequestPublic(BaseModel):
    id: int
    title: str
    city: str
    category: Category
    urgency: int
    people_count: int
    vulnerable_people: bool
    status: RequestStatus
    priority_score: int
    created_at: datetime


class RequestPrivate(RequestPublic):
    description: str
    address: str
    phone: str
    applicant_id: int
    assigned_volunteer_id: int | None


class ResourceIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    category: ResourceCategory
    city: str = Field(min_length=2, max_length=100)
    quantity: int = Field(ge=1, le=1_000_000)
    unit: str = Field(min_length=1, max_length=30)


class ResourceOut(ResourceIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_id: int
    quantity: int = Field(ge=0)


class AllocationIn(BaseModel):
    request_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=1_000_000)


class TelegramLinkIn(BaseModel):
    # DEMO ONLY: real deployment must confirm chat ownership through Telegram.
    chat_id: str = Field(min_length=1, max_length=40, pattern=r"^-?\d+$")


class Suggestion(BaseModel):
    request: RequestPublic
    compatible_resources: list[ResourceOut]


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)  # For a prototype; use Alembic migrations in production.
    yield


app = FastAPI(
    title="Разом — Volunteer Coordination API",
    version="0.1.0",
    description="Демонстраційний API для координації кризової допомоги. Не є службою екстреного реагування.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)


def get_db():
    with SessionLocal() as db:
        yield db


Db = Annotated[Session, Depends(get_db)]


def unauthorized():
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(token: Annotated[str, Depends(oauth2_scheme)], db: Db) -> User:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        unauthorized()
    user = db.get(User, user_id)
    if user is None:
        unauthorized()
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require(user: User, *roles: Role, verified: bool = False) -> None:
    if user.role not in [r.value for r in roles]:
        raise HTTPException(403, "Insufficient permissions")
    if verified and user.role == Role.volunteer.value and not user.is_verified:
        raise HTTPException(403, "Volunteer verification is required")


def score(item: HelpRequest) -> int:
    """Transparent queue suggestion only. Never an autonomous dispatch decision."""
    created = item.created_at
    if created.tzinfo is None:  # SQLite stores naive timestamps.
        created = created.replace(tzinfo=timezone.utc)
    age_hours = max(0, (datetime.now(timezone.utc) - created).total_seconds() / 3600)
    category_bonus = {"evacuation": 12, "medicine": 8, "food": 3, "other": 0}
    return (
        item.urgency * 15
        + category_bonus.get(item.category, 0)
        + (10 if item.vulnerable_people else 0)
        + min(item.people_count, 10) * 2
        + min(int(age_hours // 6) * 2, 24)
    )


def public_request(item: HelpRequest) -> RequestPublic:
    return RequestPublic(
        id=item.id, title=item.title, city=item.city, category=item.category,
        urgency=item.urgency, people_count=item.people_count,
        vulnerable_people=item.vulnerable_people, status=item.status,
        priority_score=score(item), created_at=item.created_at,
    )


def private_request(item: HelpRequest) -> RequestPrivate:
    return RequestPrivate(**public_request(item).model_dump(),
                          description=item.description, address=item.address,
                          phone=item.phone, applicant_id=item.applicant_id,
                          assigned_volunteer_id=item.assigned_volunteer_id)


def send_telegram(chat_id: str, message: str) -> None:
    """Do not put addresses/phone numbers in Telegram messages."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    req = urlrequest.Request(endpoint, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=4) as response:
            response.read()
    except Exception:
        logger.exception("Telegram notification failed")


@app.get("/", include_in_schema=False)
def frontend():
    return FileResponse(Path(__file__).parent / "web" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


# Classroom-only anti-spam. Replace with distributed limiter + CAPTCHA or verified
# contact before any public launch; these counters reset on server restart.
_guest_rates: dict[str, deque] = defaultdict(deque)
_guest_rate_lock = Lock()
_SYSTEM_GUEST_EMAIL = "system-guest@razom.invalid"


def _limit_anonymous(request: Request, kind: str, max_calls: int, seconds: int):
    # Trust only ASGI request.client, not user-supplied forwarding headers.
    ip = request.client.host if request.client else "unknown"
    key = f"{kind}:{ip}"
    now = datetime.now(timezone.utc).timestamp()
    with _guest_rate_lock:
        history = _guest_rates[key]
        while history and now - history[0] > seconds:
            history.popleft()
        if len(history) >= max_calls:
            raise HTTPException(429, "Спробуйте ще раз пізніше")
        history.append(now)


def _system_guest(db: Session) -> User:
    user = db.scalar(select(User).where(User.email == _SYSTEM_GUEST_EMAIL))
    if user:
        return user
    # Unusable random password. Guest users can never log in to this account.
    user = User(full_name="Системні гостьові заявки", email=_SYSTEM_GUEST_EMAIL,
                password_hash=password_hasher.hash(secrets.token_urlsafe(48)),
                role=Role.applicant.value, is_verified=False)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        user = db.scalar(select(User).where(User.email == _SYSTEM_GUEST_EMAIL))
        if user is None:
            raise
    return user


@app.post("/public/requests", response_model=GuestCreated, status_code=201)
def submit_guest_request(payload: GuestSubmit, request: Request, db: Db, background_tasks: BackgroundTasks):
    """Anonymous *demo* intake: no authenticated user and no verified phone yet."""
    if payload.website.strip():
        raise HTTPException(422, "Заявка не пройшла перевірку")
    _limit_anonymous(request, "submit", max_calls=4, seconds=3600)
    guest_user = _system_guest(db)
    titles = {"food": "Потрібні продукти", "medicine": "Потрібні медикаменти",
              "evacuation": "Потрібна евакуація", "other": "Потрібна допомога"}
    item = HelpRequest(
        applicant_id=guest_user.id, title=titles[payload.category.value],
        description=payload.description.strip(), city=payload.city.strip(),
        address="Уточнюється після зв'язку", phone=payload.phone.strip(),
        category=payload.category.value, urgency=payload.urgency,
        people_count=payload.people_count, vulnerable_people=payload.vulnerable_people,
        status=RequestStatus.new.value,
    )
    token = secrets.token_urlsafe(32)
    db.add(item)
    db.flush()
    db.add(GuestReceipt(request_id=item.id,
                        token_sha256=hashlib.sha256(token.encode()).hexdigest()))
    db.commit()
    db.refresh(item)
    admins = db.scalars(select(User).where(
        User.role == Role.admin.value, User.telegram_chat_id.is_not(None)
    )).all()
    for admin in admins:
        background_tasks.add_task(send_telegram, admin.telegram_chat_id,
                                  f"New guest request #{item.id} in {item.city}; awaiting review.")
    return GuestCreated(request_id=item.id, tracking_code=token)


@app.post("/public/requests/status", response_model=GuestStatus)
def guest_request_status(payload: GuestLookup, request: Request, db: Db):
    _limit_anonymous(request, "status", max_calls=30, seconds=600)
    digest = hashlib.sha256(payload.tracking_code.strip().encode()).hexdigest()
    receipt = db.scalar(select(GuestReceipt).where(
        GuestReceipt.request_id == payload.request_id,
        GuestReceipt.token_sha256 == digest,
    ))
    if receipt is None:
        raise HTTPException(404, "Номер заявки або секретний код неправильний")
    item = db.get(HelpRequest, receipt.request_id)
    if item is None:
        raise HTTPException(404, "Заявку не знайдено")
    return GuestStatus(request_id=item.id, category=item.category, city=item.city,
                       status=item.status, needs_review=receipt.reviewed_at is None,
                       created_at=item.created_at)


@app.get("/admin/guest-requests/pending", response_model=list[RequestPublic])
def pending_guest_requests(user: CurrentUser, db: Db):
    require(user, Role.admin)
    pending_ids = select(GuestReceipt.request_id).where(GuestReceipt.reviewed_at.is_(None))
    items = db.scalars(select(HelpRequest).where(HelpRequest.id.in_(pending_ids))
                       .order_by(HelpRequest.created_at).limit(200)).all()
    return [public_request(item) for item in items]


@app.patch("/admin/guest-requests/{request_id}/review")
def review_guest_request(request_id: int, user: CurrentUser, db: Db):
    require(user, Role.admin)
    receipt = db.scalar(select(GuestReceipt).where(GuestReceipt.request_id == request_id))
    if not receipt:
        raise HTTPException(404, "Гостьову заявку не знайдено")
    if receipt.reviewed_at is not None:
        raise HTTPException(409, "Заявку вже перевірено")
    receipt.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return {"request_id": request_id, "reviewed": True}


@app.post("/auth/register", response_model=UserOut, status_code=201)
def register(payload: RegisterIn, db: Db):
    email = str(payload.email).lower()
    if email == _SYSTEM_GUEST_EMAIL:
        raise HTTPException(422, "Reserved address")
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    user = User(full_name=payload.full_name.strip(), email=email,
                password_hash=password_hasher.hash(payload.password),
                role=payload.role.value, is_verified=(payload.role == Role.applicant))
    db.add(user)
    try:
        db.commit()
    except Exception:  # Unique index also guards concurrent registrations.
        db.rollback()
        raise HTTPException(409, "Unable to register: email may already be used")
    db.refresh(user)
    return user


@app.post("/auth/login", response_model=TokenOut)
def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: Db):
    user = db.scalar(select(User).where(User.email == form.username.strip().lower()))
    if not user:
        # Do not distinguish missing accounts from wrong passwords.
        raise HTTPException(401, "Incorrect email or password")
    try:
        valid = password_hasher.verify(user.password_hash, form.password)
    except (VerifyMismatchError, InvalidHashError):
        valid = False
    if not valid:
        raise HTTPException(401, "Incorrect email or password")
    expiry = datetime.now(timezone.utc) + timedelta(minutes=45)
    token = jwt.encode({"sub": str(user.id), "exp": expiry}, JWT_SECRET, algorithm="HS256")
    return TokenOut(access_token=token)


@app.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@app.post("/me/telegram")
def link_telegram(payload: TelegramLinkIn, user: CurrentUser, db: Db):
    user.telegram_chat_id = payload.chat_id
    db.commit()
    return {"message": "Demo chat ID saved; production must confirm chat ownership"}


@app.post("/requests", response_model=RequestPrivate, status_code=201)
def create_help_request(payload: HelpCreate, user: CurrentUser, db: Db,
                        background_tasks: BackgroundTasks):
    require(user, Role.applicant)
    item = HelpRequest(applicant_id=user.id, **payload.model_dump())
    item.category = payload.category.value
    db.add(item)
    db.commit()
    db.refresh(item)
    admins = db.scalars(select(User).where(User.role == Role.admin.value,
                                           User.telegram_chat_id.is_not(None))).all()
    for admin in admins:
        background_tasks.add_task(
            send_telegram, admin.telegram_chat_id,
            f"New request #{item.id}: {item.category} in {item.city}. Review in dashboard.",
        )
    return private_request(item)


@app.get("/requests", response_model=list[RequestPublic])
def list_requests(user: CurrentUser, db: Db, city: str | None = Query(None, max_length=100),
                  category: Category | None = None,
                  status_filter: RequestStatus | None = Query(None, alias="status")):
    stmt = select(HelpRequest)
    if user.role == Role.volunteer.value:
        # Newly submitted guest requests are admin-only until a human review.
        unreviewed = select(GuestReceipt.request_id).where(GuestReceipt.reviewed_at.is_(None))
        stmt = stmt.where(HelpRequest.id.not_in(unreviewed))
    if user.role == Role.applicant.value:
        stmt = stmt.where(HelpRequest.applicant_id == user.id)
    if city:
        stmt = stmt.where(func.lower(HelpRequest.city) == city.strip().lower())
    if category:
        stmt = stmt.where(HelpRequest.category == category.value)
    if status_filter:
        stmt = stmt.where(HelpRequest.status == status_filter.value)
    elif user.role == Role.volunteer.value:
        stmt = stmt.where(
            (HelpRequest.status == RequestStatus.new.value) |
            (HelpRequest.assigned_volunteer_id == user.id)
        )
    requests = db.scalars(stmt.limit(500)).all()
    return [public_request(item) for item in sorted(requests, key=score, reverse=True)]


@app.get("/requests/{request_id}", response_model=RequestPrivate)
def request_details(request_id: int, user: CurrentUser, db: Db):
    item = db.get(HelpRequest, request_id)
    if not item:
        raise HTTPException(404, "Request not found")
    if not (user.role == Role.admin.value or user.id == item.applicant_id or
            (user.role == Role.volunteer.value and user.is_verified and
             user.id == item.assigned_volunteer_id)):
        raise HTTPException(403, "Private details are only available to involved parties")
    return private_request(item)


@app.post("/requests/{request_id}/claim", response_model=RequestPrivate)
def claim_request(request_id: int, user: CurrentUser, db: Db,
                  background_tasks: BackgroundTasks):
    require(user, Role.volunteer, verified=True)
    unreviewed = select(GuestReceipt.request_id).where(GuestReceipt.reviewed_at.is_(None))
    # Conditional UPDATE prevents duplicate claims and excludes unreviewed guests.
    result = db.execute(
        update(HelpRequest)
        .where(HelpRequest.id == request_id, HelpRequest.status == RequestStatus.new.value,
               HelpRequest.assigned_volunteer_id.is_(None),
               HelpRequest.id.not_in(unreviewed))
        .values(status=RequestStatus.assigned.value, assigned_volunteer_id=user.id)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Request not found or no longer available")
    item = db.get(HelpRequest, request_id)
    db.commit()
    db.refresh(item)
    applicant = db.get(User, item.applicant_id)
    if applicant and applicant.telegram_chat_id:
        background_tasks.add_task(
            send_telegram, applicant.telegram_chat_id,
            f"Your request #{item.id} was accepted by a verified volunteer. See your dashboard.",
        )
    return private_request(item)


@app.post("/requests/{request_id}/complete", response_model=RequestPublic)
def complete_request(request_id: int, user: CurrentUser, db: Db,
                     background_tasks: BackgroundTasks):
    item = db.get(HelpRequest, request_id)
    if not item:
        raise HTTPException(404, "Request not found")
    authorized = (user.role == Role.admin.value or
                  (user.role == Role.volunteer.value and user.is_verified and
                   item.assigned_volunteer_id == user.id))
    if not authorized:
        raise HTTPException(403, "Only the assigned volunteer or admin can close a request")
    result = db.execute(update(HelpRequest).where(
        HelpRequest.id == request_id, HelpRequest.status == RequestStatus.assigned.value
    ).values(status=RequestStatus.completed.value))
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Request is not assigned")
    db.commit()
    db.refresh(item)
    applicant = db.get(User, item.applicant_id)
    if applicant and applicant.telegram_chat_id:
        background_tasks.add_task(send_telegram, applicant.telegram_chat_id,
                                  f"Request #{item.id} is marked completed.")
    return public_request(item)


@app.post("/requests/{request_id}/cancel", response_model=RequestPublic)
def cancel_request(request_id: int, user: CurrentUser, db: Db):
    item = db.get(HelpRequest, request_id)
    if not item:
        raise HTTPException(404, "Request not found")
    if user.role != Role.admin.value and item.applicant_id != user.id:
        raise HTTPException(403, "Not your request")
    # Assigned requests need coordination, so only new requests can be self-cancelled.
    allowed_statuses = ([RequestStatus.new.value, RequestStatus.assigned.value]
                        if user.role == Role.admin.value else [RequestStatus.new.value])
    result = db.execute(update(HelpRequest).where(
        HelpRequest.id == request_id, HelpRequest.status.in_(allowed_statuses)
    ).values(status=RequestStatus.cancelled.value))
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "This request cannot be cancelled here")
    db.commit()
    db.refresh(item)
    return public_request(item)


@app.post("/resources", response_model=ResourceOut, status_code=201)
def create_resource(payload: ResourceIn, user: CurrentUser, db: Db):
    require(user, Role.volunteer, Role.admin, verified=True)
    item = Resource(owner_id=user.id, **payload.model_dump())
    item.category = payload.category.value
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.get("/resources", response_model=list[ResourceOut])
def list_resources(user: CurrentUser, db: Db):
    require(user, Role.volunteer, Role.admin, verified=True)
    stmt = select(Resource)
    if user.role != Role.admin.value:
        stmt = stmt.where(Resource.owner_id == user.id)
    return db.scalars(stmt.order_by(Resource.id.desc()).limit(500)).all()


@app.get("/suggestions", response_model=list[Suggestion])
def suggestions(user: CurrentUser, db: Db, city: str | None = Query(None, max_length=100)):
    require(user, Role.volunteer, verified=True)
    resources = db.scalars(select(Resource).where(
        Resource.owner_id == user.id, Resource.quantity > 0
    )).all()
    unreviewed = select(GuestReceipt.request_id).where(GuestReceipt.reviewed_at.is_(None))
    stmt = select(HelpRequest).where(HelpRequest.status == RequestStatus.new.value,
                                    HelpRequest.id.not_in(unreviewed))
    if city:
        stmt = stmt.where(func.lower(HelpRequest.city) == city.strip().lower())
    items = db.scalars(stmt.limit(500)).all()
    compatible_type = {"food": "food", "medicine": "medicine",
                       "evacuation": "transport", "other": "other"}
    output = []
    for item in items:
        matches = [resource for resource in resources
                   if resource.category == compatible_type[item.category] and
                   resource.city.strip().casefold() == item.city.strip().casefold()]
        if matches:
            output.append(Suggestion(request=public_request(item), compatible_resources=matches))
    return sorted(output, key=lambda suggestion: suggestion.request.priority_score, reverse=True)


@app.post("/resources/{resource_id}/allocate", status_code=201)
def allocate_resource(resource_id: int, payload: AllocationIn, user: CurrentUser, db: Db):
    require(user, Role.volunteer, Role.admin, verified=True)
    item = db.get(HelpRequest, payload.request_id)
    resource = db.get(Resource, resource_id)
    if not item or not resource:
        raise HTTPException(404, "Request or resource not found")
    if item.status != RequestStatus.assigned.value:
        raise HTTPException(409, "Assign the request before allocating resources")
    if user.role != Role.admin.value and (
        item.assigned_volunteer_id != user.id or resource.owner_id != user.id
    ):
        raise HTTPException(403, "Only an assigned volunteer can allocate their inventory")
    compatible = {"food": "food", "medicine": "medicine",
                  "evacuation": "transport", "other": "other"}
    if resource.category != compatible[item.category] or resource.city.casefold() != item.city.casefold():
        raise HTTPException(422, "Resource category and city must match the request")
    # Atomic guarded UPDATE ensures inventory never goes negative under concurrency.
    result = db.execute(update(Resource).where(
        Resource.id == resource_id, Resource.quantity >= payload.quantity
    ).values(quantity=Resource.quantity - payload.quantity))
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Insufficient inventory")
    allocation = Allocation(request_id=item.id, resource_id=resource_id,
                            volunteer_id=user.id, quantity=payload.quantity)
    db.add(allocation)
    db.commit()
    db.refresh(allocation)
    return {"allocation_id": allocation.id, "quantity": allocation.quantity,
            "request_id": item.id, "resource_id": resource.id}


@app.patch("/admin/users/{user_id}/verify", response_model=UserOut)
def verify_volunteer(user_id: int, user: CurrentUser, db: Db):
    require(user, Role.admin)
    volunteer = db.get(User, user_id)
    if not volunteer or volunteer.role != Role.volunteer.value:
        raise HTTPException(404, "Volunteer not found")
    volunteer.is_verified = True
    db.commit()
    db.refresh(volunteer)
    return volunteer


@app.get("/admin/volunteers", response_model=list[UserOut])
def pending_volunteers(user: CurrentUser, db: Db):
    require(user, Role.admin)
    return db.scalars(select(User).where(
        User.role == Role.volunteer.value, User.is_verified.is_(False)
    ).order_by(User.id.desc())).all()


@app.get("/admin/analytics")
def analytics(user: CurrentUser, db: Db):
    require(user, Role.admin)
    by_status = db.execute(select(HelpRequest.status, func.count(HelpRequest.id))
                           .group_by(HelpRequest.status)).all()
    by_category = db.execute(select(HelpRequest.category, func.count(HelpRequest.id))
                             .group_by(HelpRequest.category)).all()
    by_city = db.execute(select(HelpRequest.city, func.count(HelpRequest.id))
                         .group_by(HelpRequest.city)).all()
    # City counts are visible only to admins because small samples may be identifying.
    return {
        "requests_total": db.scalar(select(func.count(HelpRequest.id))) or 0,
        "verified_volunteers": db.scalar(select(func.count(User.id)).where(
            User.role == Role.volunteer.value, User.is_verified.is_(True))) or 0,
        "allocated_units": db.scalar(select(func.coalesce(func.sum(Allocation.quantity), 0))) or 0,
        "by_status": dict(by_status),
        "by_category": dict(by_category),
        "by_city": dict(by_city),
    }

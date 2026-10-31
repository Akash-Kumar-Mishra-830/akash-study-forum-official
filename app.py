import logging
import os
import hashlib
import hmac
import json
import smtplib
import re
import secrets
import sqlite3
import ssl
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ROOT_DIR = Path(__file__).resolve().parent
MAX_HISTORY_MESSAGES = 12
MAX_MESSAGE_LENGTH = 3000
MAX_TOTAL_CHARS = 12000
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60
STUDY_DB_PATH = Path(os.getenv("STUDY_DB_PATH", str(ROOT_DIR / "asf_study.sqlite3")))
ADMIN_SESSION_COOKIE = "asf_admin_session"
CRM_SESSION_COOKIE = "asf_crm_admin_session"
ADMIN_SESSION_SECONDS = 8 * 60 * 60

logger = logging.getLogger(__name__)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_rate_limit_lock = threading.Lock()
_rate_limits: dict[str, tuple[float, int]] = {}


SYSTEM_PROMPT = """
You are ASF AI Tutor for Indian students in Classes 9–12.

TEACHING RULES:
- Adapt to the student's class and language.
- Use simple Hinglish when the student uses Hindi or Hinglish.
- Solve easy questions in 2–4 clear steps.
- Explain difficult questions in depth, including concepts,
  formulas, reasoning, calculations and checks.
- Explain WHY a method works when useful.
- Verify calculations before answering.
- Never invent facts, formulas or syllabus details.
- If a question is ambiguous, ask a brief clarification.
- Avoid unnecessary introductions and repeated explanations.
- Use age-appropriate, encouraging language.

Start directly with the solution.
End with a clear final answer.
Match the explanation length to the difficulty.
"""


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(
        min_length=1,
        max_length=MAX_HISTORY_MESSAGES,
    )


class AdminLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=256)


class CrmAccountFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=12, max_length=256)

    @field_validator("name")
    @classmethod
    def require_account_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Name must contain at least two non-space characters.")
        return value

    @field_validator("email")
    @classmethod
    def normalize_account_email(cls, value: str) -> str:
        value = value.strip().casefold()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class CrmOwnerSetup(CrmAccountFields):
    bootstrap_password: str = Field(min_length=1, max_length=256, alias="bootstrapPassword")


class CrmLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_login_email(cls, value: str) -> str:
        value = value.strip().casefold()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class CrmForgotPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=5, max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_recovery_email(cls, value: str) -> str:
        value = value.strip().casefold()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class CrmResetPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=32, max_length=128)
    password: str = Field(min_length=12, max_length=256)


class CrmLeadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    student_name: str = Field(min_length=1, max_length=100, alias="studentName")
    parent_name: str = Field(min_length=1, max_length=100, alias="parentName")
    mobile: str = Field(pattern=r"^[0-9]{10}$")
    target_class: str = Field(min_length=1, max_length=80, alias="targetClass")
    target_course: str = Field(min_length=1, max_length=120, alias="targetCourse")
    source: str = Field(min_length=1, max_length=80)
    status: Literal["NEW", "CONTACTED", "DEMO SCHEDULED", "VISITED", "ADMITTED", "NOT INTERESTED"]
    notes: str = Field(default="", max_length=3000)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("student_name", "parent_name", "target_class", "target_course", "source")
    @classmethod
    def require_crm_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value


class PublicLeadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_name: str = Field(min_length=1, max_length=100, alias="studentName")
    parent_name: str = Field(min_length=1, max_length=100, alias="parentName")
    mobile: str = Field(pattern=r"^[0-9]{10}$")
    target_class: str = Field(min_length=1, max_length=80, alias="targetClass")
    target_course: str = Field(min_length=1, max_length=120, alias="targetCourse")
    source: Literal[
        "HERO_QUICK_FORM",
        "DEMO_MODAL",
        "ADMISSION_PAGE_FULL",
        "EXIT_INTENT_POPUP",
        "5_STEP_ADMISSION_FUNNEL",
    ]
    notes: str = Field(default="", max_length=1000)

    @field_validator("student_name", "parent_name", "target_class", "target_course")
    @classmethod
    def require_public_lead_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value


class CrmLeadImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leads: list[CrmLeadPayload] = Field(max_length=1000)


class CrmLeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["NEW", "CONTACTED", "DEMO SCHEDULED", "VISITED", "ADMITTED", "NOT INTERESTED"] | None = None
    notes: str | None = Field(default=None, max_length=3000)


class FeePlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    board: str = Field(min_length=1, max_length=60)
    class_name: str = Field(min_length=1, max_length=60, alias="className")
    course: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=120)
    subjects: list[str] = Field(min_length=1, max_length=12)
    period: Literal["monthly", "term", "yearly"]
    regular_amount: int = Field(gt=0, le=1000000, alias="regularAmount")
    offer_amount: int | None = Field(default=None, gt=0, le=1000000, alias="offerAmount")
    discount_label: str = Field(default="", max_length=100, alias="discountLabel")
    recommended: bool = False
    active: bool = True

    @field_validator("board", "class_name", "course", "title")
    @classmethod
    def require_fee_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value

    @field_validator("subjects")
    @classmethod
    def validate_subjects(cls, values: list[str]) -> list[str]:
        subjects = [value.strip() for value in values]
        if any(not value for value in subjects):
            raise ValueError("Subjects cannot be blank.")
        if len({value.casefold() for value in subjects}) != len(subjects):
            raise ValueError("Subjects must not be repeated.")
        return subjects

    @field_validator("discount_label")
    @classmethod
    def trim_discount_label(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def offer_must_be_less_than_regular(self) -> "FeePlanPayload":
        if self.offer_amount is not None and self.offer_amount >= self.regular_amount:
            raise ValueError("Offer amount must be less than regular amount.")
        if self.offer_amount is None and self.discount_label:
            raise ValueError("A discount label requires an offer amount.")
        if self.period == "monthly" and not 1000 <= self.regular_amount <= 5000:
            raise ValueError("Monthly fee plans must be between ₹1,000 and ₹5,000.")
        if self.period == "monthly" and self.offer_amount is not None and not 1000 <= self.offer_amount <= 5000:
            raise ValueError("Monthly offer amounts must be between ₹1,000 and ₹5,000.")
        return self


class CrmVideoPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=60)
    youtube_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{11}$", alias="youtubeId")
    desc: str = Field(default="", max_length=1000)

    @field_validator("title", "category")
    @classmethod
    def require_video_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value


class CrmVideoImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videos: list[CrmVideoPayload] = Field(max_length=1000)


class StudyTestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=120)
    class_name: str = Field(min_length=1, max_length=40, alias="className")
    subject: str = Field(min_length=1, max_length=40)
    price: int = Field(ge=9, le=99)
    description: str = Field(default="", max_length=500)
    test_url: str = Field(min_length=1, max_length=2048, alias="testUrl")

    @field_validator("title", "class_name", "subject")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value


class StudyOrderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$", alias="testId")
    student_name: str = Field(min_length=2, max_length=100, alias="studentName")
    phone: str = Field(pattern=r"^[6-9][0-9]{9}$")

    @field_validator("student_name")
    @classmethod
    def require_student_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Student name must contain at least two non-space characters.")
        return value


class StudyPaymentProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(pattern=r"^[6-9][0-9]{9}$")
    reference: str = Field(min_length=6, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")


class StudyRedeemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=10, max_length=40, pattern=r"^[a-zA-Z0-9-]+$")
    test_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$", alias="testId")


@contextmanager
def get_study_db():
    STUDY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STUDY_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def initialize_study_db() -> None:
    with get_study_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS study_tests (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                class_name TEXT NOT NULL,
                subject TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price BETWEEN 9 AND 99),
                description TEXT NOT NULL DEFAULT '',
                test_url TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS study_orders (
                id TEXT PRIMARY KEY,
                test_id TEXT NOT NULL REFERENCES study_tests(id),
                student_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                amount_due_paise INTEGER NOT NULL DEFAULT 0,
                payment_reference TEXT,
                status TEXT NOT NULL CHECK (status IN ('awaiting_payment', 'awaiting_review', 'approved')),
                created_at TEXT NOT NULL,
                approved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS study_access_codes (
                code_hash TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE REFERENCES study_orders(id),
                redeemed_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS study_orders_payment_reference_unique
            ON study_orders(payment_reference) WHERE payment_reference IS NOT NULL;
            CREATE TABLE IF NOT EXISTS crm_leads (
                id TEXT PRIMARY KEY,
                student_name TEXT NOT NULL,
                parent_name TEXT NOT NULL,
                mobile TEXT NOT NULL,
                target_class TEXT NOT NULL,
                target_course TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                lead_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_videos (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                youtube_id TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_fee_plans (
                id TEXT PRIMARY KEY,
                board TEXT NOT NULL,
                class_name TEXT NOT NULL,
                course TEXT NOT NULL,
                title TEXT NOT NULL,
                subjects TEXT NOT NULL,
                period TEXT NOT NULL CHECK (period IN ('monthly', 'term', 'yearly')),
                regular_amount INTEGER NOT NULL CHECK (regular_amount > 0),
                offer_amount INTEGER CHECK (offer_amount IS NULL OR (offer_amount > 0 AND offer_amount < regular_amount)),
                discount_label TEXT NOT NULL DEFAULT '',
                recommended INTEGER NOT NULL DEFAULT 0 CHECK (recommended IN (0, 1)),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_users (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('owner', 'staff')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_auth_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                session_secret TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crm_password_resets (
                token_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL REFERENCES crm_users(email) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO crm_auth_config (id, session_secret) VALUES (1, ?)",
            (secrets.token_urlsafe(48),),
        )
        order_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(study_orders)").fetchall()
        }
        if "amount_due_paise" not in order_columns:
            connection.execute(
                "ALTER TABLE study_orders ADD COLUMN amount_due_paise INTEGER NOT NULL DEFAULT 0"
            )


def study_admin_password() -> str:
    password = os.getenv("STUDY_ADMIN_PASSWORD", "")
    if not password:
        raise HTTPException(
            status_code=503,
            detail="Study Hub admin login is not configured. Set STUDY_ADMIN_PASSWORD on the server.",
        )
    return password


def create_admin_session(password: str, expires_at: int) -> str:
    payload = f"{expires_at}:{secrets.token_urlsafe(24)}"
    signature = hmac.new(password.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def require_study_admin(request: Request) -> None:
    require_admin_session(request, study_admin_password(), ADMIN_SESSION_COOKIE)


def crm_admin_password() -> str:
    with get_study_db() as connection:
        has_accounts = connection.execute("SELECT 1 FROM crm_users LIMIT 1").fetchone()
        if has_accounts:
            config = connection.execute(
                "SELECT session_secret FROM crm_auth_config WHERE id = 1"
            ).fetchone()
            if config:
                return config["session_secret"]

    password = os.getenv("CRM_ADMIN_PASSWORD", "")
    if len(password) < 12 or not password.strip():
        raise HTTPException(
            status_code=503,
            detail="CRM owner setup needs a CRM_ADMIN_PASSWORD with at least 12 characters set on the server.",
        )
    return password


def hash_crm_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_crm_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt_hex, expected_hex = stored_hash.split("$", maxsplit=2)
        if algorithm != "scrypt":
            return False
        actual = hash_crm_password(password, bytes.fromhex(salt_hex)).split("$", maxsplit=2)[2]
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected_hex)


def require_admin_session(request: Request, password: str, cookie_name: str) -> sqlite3.Row | None:
    token = request.cookies.get(cookie_name, "")
    try:
        parts = token.split(":", maxsplit=3)
        if cookie_name == CRM_SESSION_COOKIE:
            if len(parts) != 4:
                raise ValueError
            expires_text, email, nonce, signature = parts
            payload = f"{expires_text}:{email}:{nonce}"
        else:
            if len(parts) != 3:
                raise ValueError
            expires_text, nonce, signature = parts
            payload = f"{expires_text}:{nonce}"
        expires_at = int(expires_text)
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Staff login required.") from None
    expected = hmac.new(password.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if expires_at < int(time.time()) or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Staff login expired. Please sign in again.")
    if cookie_name == CRM_SESSION_COOKIE:
        with get_study_db() as connection:
            user = connection.execute(
                "SELECT email, name, role FROM crm_users WHERE email = ?",
                (email,),
            ).fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="CRM account is no longer active.")
        return user
    return None


def create_crm_session(session_secret: str, email: str, expires_at: int) -> str:
    nonce = secrets.token_urlsafe(24)
    payload = f"{expires_at}:{email}:{nonce}"
    signature = hmac.new(session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def crm_email_settings() -> tuple[str, int, str, str, str, str]:
    host = os.getenv("CRM_SMTP_HOST", "").strip()
    sender = os.getenv("CRM_EMAIL_FROM", "").strip()
    public_url = os.getenv("CRM_PUBLIC_URL", "").strip().rstrip("/")
    if not host or not sender or not public_url:
        raise HTTPException(
            status_code=503,
            detail="CRM password recovery email is not configured. Contact the ASF CRM owner.",
        )
    try:
        port = int(os.getenv("CRM_SMTP_PORT", "587"))
    except ValueError:
        raise HTTPException(status_code=503, detail="CRM_SMTP_PORT must be a valid port.") from None
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=503, detail="CRM_SMTP_PORT must be between 1 and 65535.")
    parsed_url = urlsplit(public_url)
    if not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise HTTPException(status_code=503, detail="CRM_PUBLIC_URL must be a valid HTTPS site URL.")
    if parsed_url.scheme != "https" and parsed_url.hostname not in {"localhost", "127.0.0.1"}:
        raise HTTPException(status_code=503, detail="CRM_PUBLIC_URL must use HTTPS.")
    return (
        host,
        port,
        os.getenv("CRM_SMTP_USER", "").strip(),
        os.getenv("CRM_SMTP_PASSWORD", ""),
        sender,
        public_url,
    )


def send_crm_password_reset(email: str, token: str, public_url: str) -> None:
    host, port, username, password, sender, _ = crm_email_settings()
    reset_link = f"{public_url}/?crm-reset={quote(token)}#admin"
    message = EmailMessage()
    message["Subject"] = "ASF CRM password reset"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "A password reset was requested for your ASF CRM account.\n\n"
        f"Use this one-time link within 30 minutes to set a new password:\n{reset_link}\n\n"
        "If you did not request this, you can ignore this email."
    )
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        logger.exception("Unable to send CRM password reset email")
        raise HTTPException(
            status_code=502,
            detail="Unable to send the password reset email. Contact the ASF CRM owner.",
        ) from error


def row_to_public_study_test(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "title": row["title"],
        "className": row["class_name"],
        "subject": row["subject"],
        "type": "paid",
        "price": row["price"],
        "description": row["description"],
    }


def hash_access_code(code: str) -> str:
    return hashlib.sha256(code.upper().encode()).hexdigest()


initialize_study_db()


def enforce_rate_limit(client_ip: str, max_requests: int = RATE_LIMIT_REQUESTS) -> None:
    now = time.monotonic()
    with _rate_limit_lock:
        window_start, request_count = _rate_limits.get(client_ip, (now, 0))
        if now - window_start >= RATE_LIMIT_WINDOW_SECONDS:
            window_start, request_count = now, 0
        if request_count >= max_requests:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please wait a minute before trying again.",
                headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
            )
        _rate_limits[client_ip] = (window_start, request_count + 1)

        if len(_rate_limits) > 5000:
            expired = [
                address
                for address, (started, _) in _rate_limits.items()
                if now - started >= RATE_LIMIT_WINDOW_SECONDS
            ]
            for address in expired:
                _rate_limits.pop(address, None)


def get_provider_settings() -> tuple[str, str, str]:
    base_url = os.getenv("AI_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("AI_API_KEY", "").strip()
    model = os.getenv("AI_MODEL", "").strip()

    if not base_url or not api_key or not model:
        raise HTTPException(
            status_code=503,
            detail="The AI tutor is not configured. Set AI_BASE_URL, AI_MODEL, and AI_API_KEY on the server.",
        )
    try:
        parsed_url = urlsplit(base_url)
        hostname = parsed_url.hostname
    except ValueError:
        hostname = None
        parsed_url = None
    if (
        parsed_url is None
        or not hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise HTTPException(
            status_code=503,
            detail="AI_BASE_URL must be a valid HTTPS endpoint.",
        )
    is_local_http = parsed_url.scheme == "http" and parsed_url.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if parsed_url.scheme != "https" and not is_local_http:
        raise HTTPException(
            status_code=503,
            detail="AI_BASE_URL must use HTTPS (HTTP is allowed only for localhost).",
        )
    return base_url, api_key, model


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    return FileResponse(ROOT_DIR / "index.html", media_type="text/html")


@app.get("/{asset_name}", include_in_schema=False)
async def site_asset(asset_name: str) -> FileResponse:
    allowed_assets = {"asf-logo.png", "amit-sir.jpg"}
    if asset_name not in allowed_assets:
        raise HTTPException(status_code=404, detail="Not found.")
    asset_path = ROOT_DIR / asset_name
    if not asset_path.is_file():
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(asset_path)


@app.post("/api/crm/admin/login")
async def crm_admin_login(payload: CrmLogin, request: Request, response: Response) -> dict[str, str]:
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(f"crm-login:{client_ip}", max_requests=5)
    with get_study_db() as connection:
        user = connection.execute(
            "SELECT email, password_hash FROM crm_users WHERE email = ?",
            (payload.email,),
        ).fetchone()
        has_accounts = connection.execute("SELECT 1 FROM crm_users LIMIT 1").fetchone()
        config = connection.execute(
            "SELECT session_secret FROM crm_auth_config WHERE id = 1"
        ).fetchone()
    if not has_accounts:
        raise HTTPException(status_code=409, detail="Set up the first CRM owner account before signing in.")
    if not user or not verify_crm_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect CRM email or password.")
    if not config:
        raise HTTPException(status_code=503, detail="CRM session signing is not configured.")
    expires_at = int(time.time()) + ADMIN_SESSION_SECONDS
    response.set_cookie(
        CRM_SESSION_COOKIE,
        create_crm_session(config["session_secret"], user["email"], expires_at),
        max_age=ADMIN_SESSION_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/api/crm/admin",
    )
    return {"status": "authenticated"}


@app.get("/api/crm/auth/status")
async def crm_auth_status() -> dict[str, bool]:
    with get_study_db() as connection:
        has_accounts = connection.execute("SELECT 1 FROM crm_users LIMIT 1").fetchone() is not None
    bootstrap_password = os.getenv("CRM_ADMIN_PASSWORD", "")
    return {
        "ownerSetupRequired": not has_accounts,
        "ownerSetupAvailable": not has_accounts and len(bootstrap_password) >= 12 and bool(bootstrap_password.strip()),
    }


@app.post("/api/crm/auth/setup")
async def setup_crm_owner(payload: CrmOwnerSetup, request: Request, response: Response) -> dict[str, str]:
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(f"crm-setup:{client_ip}", max_requests=3)
    bootstrap_password = os.getenv("CRM_ADMIN_PASSWORD", "")
    if len(bootstrap_password) < 12 or not bootstrap_password.strip():
        raise HTTPException(
            status_code=503,
            detail="Set CRM_ADMIN_PASSWORD on the server before creating the first CRM owner account.",
        )
    if not hmac.compare_digest(payload.bootstrap_password, bootstrap_password):
        raise HTTPException(status_code=401, detail="Incorrect CRM setup password.")

    password_hash = hash_crm_password(payload.password)
    now = datetime.now(timezone.utc).isoformat()
    with get_study_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM crm_users LIMIT 1").fetchone():
            raise HTTPException(status_code=409, detail="The CRM owner account has already been set up.")
        connection.execute(
            "INSERT INTO crm_users (email, name, password_hash, role, created_at) VALUES (?, ?, ?, 'owner', ?)",
            (payload.email, payload.name, password_hash, now),
        )
        session_config = connection.execute(
            "SELECT session_secret FROM crm_auth_config WHERE id = 1"
        ).fetchone()

    if not session_config:
        raise HTTPException(status_code=503, detail="CRM session signing is not configured.")
    expires_at = int(time.time()) + ADMIN_SESSION_SECONDS
    response.set_cookie(
        CRM_SESSION_COOKIE,
        create_crm_session(session_config["session_secret"], payload.email, expires_at),
        max_age=ADMIN_SESSION_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/api/crm/admin",
    )
    return {"status": "owner_created"}


@app.get("/api/crm/admin/session")
async def crm_admin_session(request: Request) -> dict[str, str]:
    user = require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    if not user:
        raise HTTPException(status_code=401, detail="CRM account is not available.")
    return {"status": "authenticated", "name": user["name"], "email": user["email"], "role": user["role"]}


@app.post("/api/crm/auth/forgot-password")
async def crm_forgot_password(payload: CrmForgotPassword, request: Request) -> dict[str, str]:
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(f"crm-reset:{client_ip}", max_requests=5)
    _, _, _, _, _, public_url = crm_email_settings()
    with get_study_db() as connection:
        user = connection.execute(
            "SELECT email FROM crm_users WHERE email = ?",
            (payload.email,),
        ).fetchone()
    if user:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        with get_study_db() as connection:
            connection.execute("DELETE FROM crm_password_resets WHERE email = ?", (payload.email,))
            connection.execute(
                "INSERT INTO crm_password_resets (token_hash, email, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (
                    token_hash,
                    payload.email,
                    int((now + timedelta(minutes=30)).timestamp()),
                    now.isoformat(),
                ),
            )
        try:
            send_crm_password_reset(payload.email, token, public_url)
        except HTTPException:
            with get_study_db() as connection:
                connection.execute("DELETE FROM crm_password_resets WHERE token_hash = ?", (token_hash,))
            raise
    return {"status": "If that CRM email exists, a password reset link has been sent."}


@app.post("/api/crm/auth/reset-password")
async def crm_reset_password(payload: CrmResetPassword) -> dict[str, str]:
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    now = int(time.time())
    with get_study_db() as connection:
        reset = connection.execute(
            "SELECT email FROM crm_password_resets WHERE token_hash = ? AND expires_at >= ?",
            (token_hash, now),
        ).fetchone()
        if not reset:
            raise HTTPException(status_code=400, detail="This reset link is invalid or has expired.")
        connection.execute(
            "UPDATE crm_users SET password_hash = ? WHERE email = ?",
            (hash_crm_password(payload.password), reset["email"]),
        )
        connection.execute("DELETE FROM crm_password_resets")
        connection.execute(
            "UPDATE crm_auth_config SET session_secret = ? WHERE id = 1",
            (secrets.token_urlsafe(48),),
        )
    return {"status": "Password updated. Sign in with your new password."}


@app.get("/api/crm/admin/accounts")
async def list_crm_accounts(request: Request) -> list[dict[str, str]]:
    current_user = require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    if not current_user or current_user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Only the CRM owner can manage staff accounts.")
    with get_study_db() as connection:
        users = connection.execute(
            "SELECT email, name, role, created_at FROM crm_users ORDER BY role, name"
        ).fetchall()
    return [dict(user) for user in users]


@app.post("/api/crm/admin/accounts")
async def create_crm_staff_account(
    payload: CrmAccountFields,
    request: Request,
) -> dict[str, str]:
    current_user = require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    if not current_user or current_user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Only the CRM owner can add staff accounts.")
    try:
        with get_study_db() as connection:
            connection.execute(
                "INSERT INTO crm_users (email, name, password_hash, role, created_at) VALUES (?, ?, ?, 'staff', ?)",
                (
                    payload.email,
                    payload.name,
                    hash_crm_password(payload.password),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="A CRM account with this email already exists.") from error
    return {"status": "staff_created"}


@app.delete("/api/crm/admin/accounts/{email}")
async def delete_crm_staff_account(email: str, request: Request) -> dict[str, str]:
    current_user = require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    if not current_user or current_user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Only the CRM owner can remove staff accounts.")
    normalized_email = email.strip().casefold()
    if normalized_email == current_user["email"]:
        raise HTTPException(status_code=400, detail="The active owner account cannot remove itself.")
    with get_study_db() as connection:
        user = connection.execute(
            "SELECT role FROM crm_users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="CRM account not found.")
        if user["role"] != "staff":
            raise HTTPException(status_code=400, detail="The owner account cannot be removed here.")
        connection.execute("DELETE FROM crm_users WHERE email = ?", (normalized_email,))
    return {"status": "staff_removed"}


@app.post("/api/crm/admin/logout")
async def crm_admin_logout(request: Request, response: Response) -> dict[str, str]:
    response.delete_cookie(
        CRM_SESSION_COOKIE,
        path="/api/crm/admin",
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    return {"status": "logged_out"}


def crm_lead_dict(row: sqlite3.Row) -> dict[str, str]:
    return {
        "id": row["id"],
        "studentName": row["student_name"],
        "parentName": row["parent_name"],
        "mobile": row["mobile"],
        "targetClass": row["target_class"],
        "targetCourse": row["target_course"],
        "source": row["source"],
        "status": row["status"],
        "notes": row["notes"],
        "date": row["lead_date"],
    }


def crm_video_dict(row: sqlite3.Row) -> dict[str, str]:
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"],
        "youtubeId": row["youtube_id"],
        "desc": row["description"],
    }


def crm_fee_plan_dict(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "board": row["board"],
        "className": row["class_name"],
        "course": row["course"],
        "title": row["title"],
        "subjects": json.loads(row["subjects"]),
        "period": row["period"],
        "regularAmount": row["regular_amount"],
        "offerAmount": row["offer_amount"],
        "discountLabel": row["discount_label"],
        "recommended": bool(row["recommended"]),
        "active": bool(row["active"]),
    }


def insert_crm_leads(connection: sqlite3.Connection, leads: list[CrmLeadPayload]) -> int:
    before = connection.total_changes
    connection.executemany(
        """
        INSERT OR IGNORE INTO crm_leads
            (id, student_name, parent_name, mobile, target_class, target_course,
             source, status, notes, lead_date, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                lead.id,
                lead.student_name.strip(),
                lead.parent_name.strip(),
                lead.mobile,
                lead.target_class.strip(),
                lead.target_course.strip(),
                lead.source,
                lead.status,
                lead.notes,
                lead.date,
                datetime.now(timezone.utc).isoformat(),
            )
            for lead in leads
        ],
    )
    return connection.total_changes - before


def insert_crm_videos(connection: sqlite3.Connection, videos: list[CrmVideoPayload]) -> int:
    before = connection.total_changes
    connection.executemany(
        """
        INSERT OR IGNORE INTO crm_videos (id, title, category, youtube_id, description, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                video.id,
                video.title.strip(),
                video.category.strip(),
                video.youtube_id,
                video.desc.strip(),
                datetime.now(timezone.utc).isoformat(),
            )
            for video in videos
        ],
    )
    return connection.total_changes - before


@app.post("/api/leads")
async def create_public_crm_lead(payload: PublicLeadPayload, request: Request) -> dict[str, str]:
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(f"public-lead:{client_ip}", max_requests=10)
    lead_id = f"ASF-{uuid.uuid4().hex[:12].upper()}"
    status = "DEMO SCHEDULED" if payload.source in {"DEMO_MODAL", "5_STEP_ADMISSION_FUNNEL"} else "NEW"
    now = datetime.now(timezone.utc)
    with get_study_db() as connection:
        connection.execute(
            """
            INSERT INTO crm_leads
                (id, student_name, parent_name, mobile, target_class, target_course,
                 source, status, notes, lead_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                payload.student_name.strip(),
                payload.parent_name.strip(),
                payload.mobile,
                payload.target_class.strip(),
                payload.target_course.strip(),
                payload.source,
                status,
                payload.notes.strip(),
                now.date().isoformat(),
                now.isoformat(),
            ),
        )
    return {"id": lead_id}


@app.get("/api/leads/count")
async def public_crm_lead_count() -> dict[str, int]:
    with get_study_db() as connection:
        count = connection.execute("SELECT COUNT(*) FROM crm_leads").fetchone()[0]
    return {"count": count}


@app.get("/api/videos")
async def list_public_crm_videos() -> list[dict[str, str]]:
    with get_study_db() as connection:
        rows = connection.execute("SELECT * FROM crm_videos ORDER BY created_at DESC").fetchall()
    return [crm_video_dict(row) for row in rows]


@app.get("/api/crm/admin/leads")
async def list_crm_leads(request: Request) -> list[dict[str, str]]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        rows = connection.execute("SELECT * FROM crm_leads ORDER BY created_at DESC").fetchall()
    return [crm_lead_dict(row) for row in rows]


@app.post("/api/crm/admin/leads")
async def create_crm_walkin(payload: CrmLeadPayload, request: Request) -> dict[str, str]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        inserted = insert_crm_leads(connection, [payload])
    if not inserted:
        raise HTTPException(status_code=409, detail="A lead with this ID already exists.")
    return {"id": payload.id}


@app.post("/api/crm/admin/leads/import")
async def import_crm_leads(payload: CrmLeadImport, request: Request) -> dict[str, int]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        imported = insert_crm_leads(connection, payload.leads)
    return {"imported": imported}


@app.patch("/api/crm/admin/leads/{lead_id}")
async def update_crm_lead(lead_id: str, payload: CrmLeadUpdate, request: Request) -> dict[str, str]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    changes = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not changes:
        raise HTTPException(status_code=422, detail="Provide a status or notes update.")
    with get_study_db() as connection:
        if "status" in changes:
            connection.execute("UPDATE crm_leads SET status = ? WHERE id = ?", (changes["status"], lead_id))
        if "notes" in changes:
            connection.execute("UPDATE crm_leads SET notes = ? WHERE id = ?", (changes["notes"], lead_id))
        if not connection.execute("SELECT 1 FROM crm_leads WHERE id = ?", (lead_id,)).fetchone():
            raise HTTPException(status_code=404, detail="CRM lead not found.")
    return {"status": "updated"}


@app.delete("/api/crm/admin/leads/{lead_id}")
async def delete_crm_lead(lead_id: str, request: Request) -> dict[str, str]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        deleted = connection.execute("DELETE FROM crm_leads WHERE id = ?", (lead_id,))
        if deleted.rowcount == 0:
            raise HTTPException(status_code=404, detail="CRM lead not found.")
    return {"status": "deleted"}


@app.delete("/api/crm/admin/leads")
async def clear_crm_leads(request: Request) -> dict[str, int]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        deleted = connection.execute("SELECT COUNT(*) FROM crm_leads").fetchone()[0]
        connection.execute("DELETE FROM crm_leads")
    return {"deleted": deleted}


@app.get("/api/fees")
async def list_public_fee_plans() -> list[dict[str, object]]:
    with get_study_db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM crm_fee_plans
            WHERE active = 1
            ORDER BY recommended DESC, board, class_name, course, regular_amount
            """
        ).fetchall()
    return [crm_fee_plan_dict(row) for row in rows]


@app.get("/api/crm/admin/fee-plans")
async def list_crm_fee_plans(request: Request) -> list[dict[str, object]]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM crm_fee_plans
            ORDER BY board, class_name, course, period, regular_amount
            """
        ).fetchall()
    return [crm_fee_plan_dict(row) for row in rows]


@app.post("/api/crm/admin/fee-plans")
async def create_crm_fee_plan(payload: FeePlanPayload, request: Request) -> dict[str, str]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_study_db() as connection:
            connection.execute(
                """
                INSERT INTO crm_fee_plans
                    (id, board, class_name, course, title, subjects, period,
                     regular_amount, offer_amount, discount_label, recommended,
                     active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.id,
                    payload.board,
                    payload.class_name,
                    payload.course,
                    payload.title,
                    json.dumps(payload.subjects, ensure_ascii=False),
                    payload.period,
                    payload.regular_amount,
                    payload.offer_amount,
                    payload.discount_label,
                    int(payload.recommended),
                    int(payload.active),
                    now,
                    now,
                ),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="A fee plan with this ID already exists.") from error
    return {"id": payload.id}


@app.put("/api/crm/admin/fee-plans/{plan_id}")
async def update_crm_fee_plan(
    plan_id: str,
    payload: FeePlanPayload,
    request: Request,
) -> dict[str, str]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    if plan_id != payload.id:
        raise HTTPException(status_code=422, detail="Fee plan ID cannot be changed.")
    with get_study_db() as connection:
        updated = connection.execute(
            """
            UPDATE crm_fee_plans
            SET board = ?, class_name = ?, course = ?, title = ?, subjects = ?,
                period = ?, regular_amount = ?, offer_amount = ?, discount_label = ?,
                recommended = ?, active = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                payload.board,
                payload.class_name,
                payload.course,
                payload.title,
                json.dumps(payload.subjects, ensure_ascii=False),
                payload.period,
                payload.regular_amount,
                payload.offer_amount,
                payload.discount_label,
                int(payload.recommended),
                int(payload.active),
                datetime.now(timezone.utc).isoformat(),
                plan_id,
            ),
        )
        if updated.rowcount == 0:
            raise HTTPException(status_code=404, detail="Fee plan not found.")
    return {"status": "updated"}


@app.delete("/api/crm/admin/fee-plans/{plan_id}")
async def delete_crm_fee_plan(plan_id: str, request: Request) -> dict[str, str]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        deleted = connection.execute("DELETE FROM crm_fee_plans WHERE id = ?", (plan_id,))
        if deleted.rowcount == 0:
            raise HTTPException(status_code=404, detail="Fee plan not found.")
    return {"status": "deleted"}


@app.post("/api/crm/admin/leads/import-videos")
async def import_crm_videos(payload: CrmVideoImport, request: Request) -> dict[str, int]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        imported = insert_crm_videos(connection, payload.videos)
    return {"imported": imported}


@app.post("/api/crm/admin/videos")
async def create_crm_video(payload: CrmVideoPayload, request: Request) -> dict[str, str]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        inserted = insert_crm_videos(connection, [payload])
    if not inserted:
        raise HTTPException(status_code=409, detail="A video with this ID already exists.")
    return {"id": payload.id}


@app.delete("/api/crm/admin/videos/{video_id}")
async def delete_crm_video(video_id: str, request: Request) -> dict[str, str]:
    require_admin_session(request, crm_admin_password(), CRM_SESSION_COOKIE)
    with get_study_db() as connection:
        deleted = connection.execute("DELETE FROM crm_videos WHERE id = ?", (video_id,)).rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="CRM video not found.")
    return {"status": "deleted"}


@app.post("/api/study/admin/login")
async def study_admin_login(payload: AdminLogin, request: Request, response: Response) -> dict[str, str]:
    configured_password = study_admin_password()
    if not hmac.compare_digest(payload.password, configured_password):
        raise HTTPException(status_code=401, detail="Incorrect staff password.")
    expires_at = int(time.time()) + ADMIN_SESSION_SECONDS
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        create_admin_session(configured_password, expires_at),
        max_age=ADMIN_SESSION_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/api/study/admin",
    )
    return {"status": "authenticated"}


@app.post("/api/study/admin/logout")
async def study_admin_logout(response: Response) -> dict[str, str]:
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/api/study/admin", samesite="strict")
    return {"status": "logged_out"}


@app.get("/api/study/tests")
async def list_paid_study_tests() -> list[dict[str, object]]:
    with get_study_db() as connection:
        rows = connection.execute(
            "SELECT id, title, class_name, subject, price, description FROM study_tests ORDER BY updated_at DESC"
        ).fetchall()
    return [row_to_public_study_test(row) for row in rows]


@app.post("/api/study/admin/tests")
async def save_paid_study_test(payload: StudyTestPayload, request: Request) -> dict[str, object]:
    require_study_admin(request)
    try:
        parsed_url = urlsplit(payload.test_url)
    except ValueError:
        raise HTTPException(status_code=422, detail="Test link must be a valid HTTPS URL.") from None
    if parsed_url.scheme not in {"https", "http"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        raise HTTPException(status_code=422, detail="Test link must be a valid HTTP or HTTPS URL.")
    now = datetime.now(timezone.utc).isoformat()
    with get_study_db() as connection:
        connection.execute(
            """
            INSERT INTO study_tests (id, title, class_name, subject, price, description, test_url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                class_name = excluded.class_name,
                subject = excluded.subject,
                price = excluded.price,
                description = excluded.description,
                test_url = excluded.test_url,
                updated_at = excluded.updated_at
            """,
            (
                payload.id,
                payload.title.strip(),
                payload.class_name.strip(),
                payload.subject.strip(),
                payload.price,
                payload.description.strip(),
                payload.test_url.strip(),
                now,
            ),
        )
    return {"status": "saved", **payload.model_dump(by_alias=True, exclude={"test_url"})}


@app.get("/api/study/admin/tests")
async def list_admin_study_tests(request: Request) -> list[dict[str, object]]:
    require_study_admin(request)
    with get_study_db() as connection:
        rows = connection.execute("SELECT * FROM study_tests ORDER BY updated_at DESC").fetchall()
    return [
        {**row_to_public_study_test(row), "testUrl": row["test_url"]}
        for row in rows
    ]


@app.delete("/api/study/admin/tests/{test_id}")
async def delete_paid_study_test(test_id: str, request: Request) -> dict[str, str]:
    require_study_admin(request)
    with get_study_db() as connection:
        if not connection.execute("SELECT 1 FROM study_tests WHERE id = ?", (test_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Test series not found.")
        has_orders = connection.execute(
            "SELECT 1 FROM study_orders WHERE test_id = ? LIMIT 1",
            (test_id,),
        ).fetchone()
        if has_orders:
            raise HTTPException(status_code=409, detail="This test has order history and cannot be deleted.")
        connection.execute("DELETE FROM study_tests WHERE id = ?", (test_id,))
    return {"status": "deleted"}


@app.post("/api/study/orders")
async def create_study_order(payload: StudyOrderPayload, request: Request) -> dict[str, object]:
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(client_ip)
    order_id = uuid.uuid4().hex
    with get_study_db() as connection:
        test = connection.execute(
            "SELECT id, title, class_name, subject, price FROM study_tests WHERE id = ?",
            (payload.test_id,),
        ).fetchone()
        if not test:
            raise HTTPException(status_code=404, detail="Paid test series not found.")
        connection.execute(
            """
            INSERT INTO study_orders (id, test_id, student_name, phone, amount_due_paise, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'awaiting_payment', ?)
            """,
            (
                order_id,
                payload.test_id,
                payload.student_name.strip(),
                payload.phone,
                test["price"] * 90,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return {
        "orderId": order_id,
        "test": {
            "id": test["id"],
            "title": test["title"],
            "className": test["class_name"],
            "subject": test["subject"],
            "price": test["price"],
            "amountDuePaise": test["price"] * 90,
        },
    }


@app.post("/api/study/orders/{order_id}/proof")
async def submit_study_payment_proof(
    order_id: str,
    payload: StudyPaymentProof,
) -> dict[str, str]:
    with get_study_db() as connection:
        order = connection.execute(
            "SELECT phone, status FROM study_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not order or not hmac.compare_digest(order["phone"], payload.phone):
            raise HTTPException(status_code=404, detail="Payment request not found for those details.")
        if order["status"] == "approved":
            raise HTTPException(status_code=409, detail="This payment request has already been approved.")
        try:
            connection.execute(
                """
                UPDATE study_orders
                SET payment_reference = ?, status = 'awaiting_review'
                WHERE id = ?
                """,
                (payload.reference.strip().upper(), order_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="This payment reference has already been submitted.") from None
    return {"status": "submitted_for_review"}


@app.get("/api/study/admin/orders")
async def list_study_orders(request: Request) -> list[dict[str, object]]:
    require_study_admin(request)
    with get_study_db() as connection:
        rows = connection.execute(
            """
            SELECT o.id, o.student_name, o.phone, o.amount_due_paise, o.payment_reference, o.status, o.created_at,
                   c.redeemed_at,
                   t.title, t.class_name, t.subject, t.price
            FROM study_orders o
            JOIN study_tests t ON t.id = o.test_id
            LEFT JOIN study_access_codes c ON c.order_id = o.id
            ORDER BY CASE o.status WHEN 'awaiting_review' THEN 0 ELSE 1 END, o.created_at DESC
            """
        ).fetchall()
    return [
        {
            "id": row["id"],
            "studentName": row["student_name"],
            "phone": row["phone"],
            "amountDuePaise": row["amount_due_paise"],
            "paymentReference": row["payment_reference"],
            "status": row["status"],
            "redeemedAt": row["redeemed_at"],
            "createdAt": row["created_at"],
            "title": row["title"],
            "className": row["class_name"],
            "subject": row["subject"],
            "price": row["price"],
        }
        for row in rows
    ]


@app.delete("/api/study/admin/orders/{order_id}")
async def delete_study_order(order_id: str, request: Request) -> dict[str, str]:
    require_study_admin(request)
    with get_study_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute(
            "SELECT 1 FROM study_orders WHERE id = ?",
            (order_id,),
        ).fetchone():
            raise HTTPException(status_code=404, detail="Paid test order not found.")
        connection.execute("DELETE FROM study_access_codes WHERE order_id = ?", (order_id,))
        connection.execute("DELETE FROM study_orders WHERE id = ?", (order_id,))
    return {"status": "deleted"}


@app.post("/api/study/admin/orders/{order_id}/approve")
async def approve_study_order(order_id: str, request: Request) -> dict[str, object]:
    require_study_admin(request)
    with get_study_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            "SELECT status FROM study_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Payment request not found.")
        if order["status"] != "awaiting_review":
            raise HTTPException(status_code=409, detail="Submit and review a payment reference before approval.")
        code = f"ASF-{secrets.token_hex(8).upper()}"
        connection.execute(
            "INSERT INTO study_access_codes (code_hash, order_id) VALUES (?, ?)",
            (hash_access_code(code), order_id),
        )
        connection.execute(
            "UPDATE study_orders SET status = 'approved', approved_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), order_id),
        )
        student = connection.execute(
            "SELECT student_name, phone FROM study_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    return {
        "status": "approved",
        "accessCode": code,
        "studentName": student["student_name"],
        "phone": student["phone"],
    }


@app.post("/api/study/admin/orders/{order_id}/reissue-code")
async def reissue_study_access_code(order_id: str, request: Request) -> dict[str, str]:
    require_study_admin(request)
    with get_study_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            """
            SELECT o.status, o.student_name, o.phone, c.redeemed_at
            FROM study_orders o
            JOIN study_access_codes c ON c.order_id = o.id
            WHERE o.id = ?
            """,
            (order_id,),
        ).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Approved order not found.")
        if order["status"] != "approved":
            raise HTTPException(status_code=409, detail="Approve the payment before issuing an access code.")
        if order["redeemed_at"]:
            raise HTTPException(status_code=409, detail="This order's access code has already been redeemed.")
        code = f"ASF-{secrets.token_hex(8).upper()}"
        connection.execute(
            "UPDATE study_access_codes SET code_hash = ? WHERE order_id = ? AND redeemed_at IS NULL",
            (hash_access_code(code), order_id),
        )
    return {
        "status": "reissued",
        "accessCode": code,
        "studentName": order["student_name"],
        "phone": order["phone"],
    }


@app.post("/api/study/redeem")
async def redeem_study_access_code(payload: StudyRedeemPayload) -> dict[str, str]:
    code_hash = hash_access_code(payload.code.strip())
    with get_study_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT c.code_hash, c.redeemed_at, o.test_id, t.title, t.test_url
            FROM study_access_codes c
            JOIN study_orders o ON o.id = c.order_id
            JOIN study_tests t ON t.id = o.test_id
            WHERE c.code_hash = ?
            """,
            (code_hash,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Access code is invalid.")
        if row["test_id"] != payload.test_id:
            raise HTTPException(status_code=409, detail="This access code belongs to a different test series.")
        if row["redeemed_at"]:
            raise HTTPException(status_code=409, detail="This one-time access code has already been used.")
        connection.execute(
            "UPDATE study_access_codes SET redeemed_at = ? WHERE code_hash = ?",
            (datetime.now(timezone.utc).isoformat(), code_hash),
        )
    return {"testId": row["test_id"], "title": row["title"], "testUrl": row["test_url"]}


@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request) -> dict[str, str]:
    client_ip = request.client.host if request.client else "unknown"
    enforce_rate_limit(client_ip)

    if payload.messages[-1].role != "user":
        raise HTTPException(
            status_code=422,
            detail="The latest chat message must be a question from the student.",
        )
    if any(not message.content.strip() for message in payload.messages):
        raise HTTPException(status_code=422, detail="Chat messages cannot be blank.")
    if sum(len(message.content) for message in payload.messages) > MAX_TOTAL_CHARS:
        raise HTTPException(
            status_code=413,
            detail="This conversation is too long. Restart the chat and try again.",
        )

    base_url, api_key, model = get_provider_settings()
    provider_url = f"{base_url.rstrip('/')}/chat/completions"
    provider_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    provider_messages.extend(
        {"role": message.role, "content": message.content}
        for message in payload.messages
    )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
        ) as client:
            logger.info("Calling AI provider: %s | model: %s", provider_url, model)
            response = await client.post(
                provider_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": provider_messages,
                    "temperature": 0.2,
                    "max_tokens": 400,
                    "reasoning_effort": "low",
                },
            )
    except httpx.TimeoutException:
        logger.warning("AI request timed out after 60 seconds")
        
        raise HTTPException(
            status_code=504,
            detail="The AI provider took too long to respond. Please try again.",
        ) from None
    except httpx.RequestError:
        logger.exception("Could not connect to the configured AI provider")
        raise HTTPException(
            status_code=502,
            detail="Could not connect to the AI provider. Check the server configuration.",
        ) from None

    if not response.is_success:
        logger.warning("AI provider returned HTTP %s", response.status_code)
        raise HTTPException(
            status_code=502,
            detail="The AI provider rejected the request. Check the server model and API configuration.",
        )

    try:
        result = response.json()
    except ValueError:
        logger.warning("AI provider returned invalid JSON")
        raise HTTPException(
            status_code=502,
            detail="The AI provider returned an invalid response. Please try again.",
        ) from None

    choices = result.get("choices") if isinstance(result, dict) else None
    first_choice = choices[0] if isinstance(choices, list) and choices else None
    message = first_choice.get("message") if isinstance(first_choice, dict) else None
    reply = message.get("content") if isinstance(message, dict) else None
    if not isinstance(reply, str) or not reply.strip():
        logger.warning("AI provider response did not contain a text answer")
        raise HTTPException(
            status_code=502,
            detail="The AI provider returned an empty answer. Please try again.",
        )

    return {"reply": reply.strip()}

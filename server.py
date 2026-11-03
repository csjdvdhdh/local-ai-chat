#!/usr/bin/env python3
"""Multi-tenant Telegram bot hosting control plane for Railway."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, create_engine, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
MAX_SOURCE_BYTES = 256 * 1024
MAX_REQUIREMENTS_BYTES = 8 * 1024
MAX_REQUEST_BYTES = MAX_SOURCE_BYTES + MAX_REQUIREMENTS_BYTES + 64 * 1024
MAX_REQUIREMENTS = 30
SESSION_DAYS = 14
COOKIE_NAME = "botpanel_session"
RAILWAY_API = "https://backboard.railway.com/graphql/v2"


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
RAILWAY_API_TOKEN = os.getenv("RAILWAY_API_TOKEN", "").strip()
RAILWAY_WORKSPACE_ID = os.getenv("RAILWAY_WORKSPACE_ID", "").strip()
PLATFORM_INVITE_CODE = os.getenv("PLATFORM_INVITE_CODE", "").strip()
MAX_BOTS_PER_USER = max(1, min(int(os.getenv("MAX_BOTS_PER_USER", "1")), 20))
MAX_TOTAL_BOTS = max(1, min(int(os.getenv("MAX_TOTAL_BOTS", "10")), 500))
PRODUCTION = env_bool("PRODUCTION", bool(os.getenv("RAILWAY_ENVIRONMENT")))
COOKIE_SECURE = env_bool("COOKIE_SECURE", PRODUCTION or PUBLIC_BASE_URL.startswith("https://"))

raw_database_url = os.getenv("DATABASE_URL", "").strip()
if raw_database_url.startswith("postgres://"):
    raw_database_url = "postgresql+psycopg://" + raw_database_url[len("postgres://"):]
elif raw_database_url.startswith("postgresql://") and "+psycopg" not in raw_database_url:
    raw_database_url = "postgresql+psycopg://" + raw_database_url[len("postgresql://"):]
if raw_database_url:
    DATABASE_URL = raw_database_url
else:
    local_db = Path(os.getenv("BOT_PLATFORM_DB", str(ROOT / "data" / "platform.db")))
    local_db.parent.mkdir(parents=True, exist_ok=True)
    DATABASE_URL = f"sqlite:///{local_db}"

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sessions: Mapped[list["AuthSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    bots: Mapped[list["Bot"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[User] = relationship(back_populates="sessions")


class Bot(Base):
    __tablename__ = "bots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    source_code: Mapped[str] = mapped_column(Text)
    source_filename: Mapped[str] = mapped_column(String(120), default="bot.py")
    requirements: Mapped[list[str]] = mapped_column(JSON, default=list)
    runtime_token_hash: Mapped[str] = mapped_column(String(64))
    railway_project_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    railway_environment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    railway_service_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="provisioning")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    owner: Mapped[User] = relationship(back_populates="bots")


Base.metadata.create_all(bind=engine)

app = FastAPI(title="BotDock Control Plane", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def secure_headers(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/api/bots":
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                oversized = int(raw_length) > MAX_REQUEST_BYTES
            except ValueError:
                oversized = True
            if oversized:
                return JSONResponse(status_code=413, content={"detail": "حجم الطلب يتجاوز الحد المسموح"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt_text, digest_text = encoded.split("$", 2)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


@dataclass
class AuthContext:
    user: User
    session: AuthSession


def auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw or len(raw) > 256:
        raise HTTPException(status_code=401, detail="يلزم تسجيل الدخول")
    item = db.get(AuthSession, sha256_hex(raw))
    if not item or item.expires_at <= datetime.utcnow():
        if item:
            db.delete(item)
            db.commit()
        raise HTTPException(status_code=401, detail="انتهت الجلسة، سجّل الدخول مجدداً")
    user = db.get(User, item.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="الجلسة غير صالحة")
    return AuthContext(user=user, session=item)


def csrf_context(request: Request, auth: AuthContext = Depends(auth_context)) -> AuthContext:
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied or not hmac.compare_digest(supplied, auth.session.csrf_token):
        raise HTTPException(status_code=403, detail="رمز الحماية غير صالح؛ حدّث الصفحة ثم أعد المحاولة")
    origin = request.headers.get("origin")
    if origin:
        expected = urlparse(PUBLIC_BASE_URL) if PUBLIC_BASE_URL else urlparse(f"{request.url.scheme}://{request.headers.get('host', '')}")
        supplied_origin = urlparse(origin)
        if supplied_origin.scheme != expected.scheme or supplied_origin.netloc != expected.netloc:
            raise HTTPException(status_code=403, detail="مصدر الطلب غير مسموح")
    return auth


def create_session(db: Session, user: User) -> tuple[str, str]:
    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    db.add(AuthSession(
        token_hash=sha256_hex(raw_token),
        csrf_token=csrf_token,
        user_id=user.id,
        expires_at=datetime.utcnow() + timedelta(days=SESSION_DAYS),
    ))
    db.commit()
    return raw_token, csrf_token


def set_auth_cookie(response: JSONResponse, raw_token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        raw_token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def config_missing() -> list[str]:
    missing = []
    if not os.getenv("DATABASE_URL"):
        missing.append("DATABASE_URL")
    if not PUBLIC_BASE_URL or not PUBLIC_BASE_URL.startswith("https://"):
        missing.append("PUBLIC_BASE_URL")
    if not RAILWAY_API_TOKEN:
        missing.append("RAILWAY_API_TOKEN")
    if not RAILWAY_WORKSPACE_ID:
        missing.append("RAILWAY_WORKSPACE_ID")
    if PRODUCTION and not PLATFORM_INVITE_CODE:
        missing.append("PLATFORM_INVITE_CODE")
    return missing


def require_platform_ready() -> None:
    missing = config_missing()
    if missing:
        raise HTTPException(
            status_code=503,
            detail="خدمة الاستضافة غير مهيأة بعد. أضف إعدادات PostgreSQL وRailway وعنوان الموقع ورمز الدعوة في متغيرات Railway.",
        )


def require_account_storage() -> None:
    if PRODUCTION and not os.getenv("DATABASE_URL"):
        raise HTTPException(status_code=503, detail="يلزم إعداد PostgreSQL قبل فتح الحسابات")


class SignupBody(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    invite_code: str = Field(default="", max_length=256)


class LoginBody(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=256)


def validate_email(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise HTTPException(status_code=422, detail="أدخل بريداً إلكترونياً صحيحاً")
    return value


def user_payload(user: User, csrf_token: str) -> dict[str, Any]:
    return {"user": {"id": user.id, "email": user.email}, "csrf_token": csrf_token}


@app.get("/api/config")
def api_config():
    missing = config_missing()
    ready = not PRODUCTION or not missing
    limits = {
        "bots_per_user": MAX_BOTS_PER_USER,
        "bots": MAX_BOTS_PER_USER,
        "per_user": MAX_BOTS_PER_USER,
        "total_bots": MAX_TOTAL_BOTS,
        "source_bytes": MAX_SOURCE_BYTES,
        "max_file_size": MAX_SOURCE_BYTES,
        "max_upload_size": MAX_SOURCE_BYTES,
        "requirements_bytes": MAX_REQUIREMENTS_BYTES,
    }
    return {
        "configured": ready,
        "message": "كل بوت يعمل كخدمة Railway مستقلة ويستهلك موارد مساحة المنصة؛ الحد الحالي بوت واحد لكل حساب." if not missing else "وضع التطوير: ستحتاج إعداد Railway قبل تشغيل بوتات فعلية." if not PRODUCTION else "يلزم إكمال إعدادات النشر قبل تشغيل البوتات.",
        "limits": limits,
        "signups_open": bool(PLATFORM_INVITE_CODE) or not PRODUCTION,
    }


@app.post("/api/auth/signup")
def signup(body: SignupBody, request: Request, db: Session = Depends(get_db)):
    require_account_storage()
    if PRODUCTION and not PLATFORM_INVITE_CODE:
        raise HTTPException(status_code=503, detail="التسجيل مغلق مؤقتاً حتى يضبط مشرف المنصة رمز الدعوة")
    if PLATFORM_INVITE_CODE and not hmac.compare_digest(body.invite_code, PLATFORM_INVITE_CODE):
        raise HTTPException(status_code=403, detail="رمز الدعوة غير صحيح")
    email = validate_email(body.email)
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="يوجد حساب بهذا البريد الإلكتروني")
    user = User(email=email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="تعذر إنشاء الحساب؛ تحقق من البريد الإلكتروني")
    raw_token, csrf_token = create_session(db, user)
    response = JSONResponse(user_payload(user, csrf_token))
    set_auth_cookie(response, raw_token)
    return response


@app.post("/api/auth/login")
def login(body: LoginBody, db: Session = Depends(get_db)):
    require_account_storage()
    email = validate_email(body.email)
    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="البريد الإلكتروني أو كلمة المرور غير صحيحة")
    raw_token, csrf_token = create_session(db, user)
    response = JSONResponse(user_payload(user, csrf_token))
    set_auth_cookie(response, raw_token)
    return response


@app.post("/api/auth/logout")
def logout(auth: AuthContext = Depends(csrf_context), db: Session = Depends(get_db)):
    db.delete(auth.session)
    db.commit()
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/api/me")
def me(auth: AuthContext = Depends(auth_context)):
    return {"user": {"id": auth.user.id, "email": auth.user.email}, "csrf_token": auth.session.csrf_token}


def requirements_from_text(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) > MAX_REQUIREMENTS:
        raise HTTPException(status_code=422, detail=f"الحد الأقصى {MAX_REQUIREMENTS} مكتبة")
    valid = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?(?:\s*(?:==|>=|<=|~=|!=)\s*[A-Za-z0-9][A-Za-z0-9.*+!_-]*)?$")
    for line in lines:
        if len(line) > 160 or not valid.fullmatch(line):
            raise HTTPException(status_code=422, detail="ملف requirements.txt يحتوي صيغة غير مسموحة؛ استخدم أسماء مكتبات وإصدارات فقط دون روابط أو خيارات pip")
    return lines


def railway_request(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    if not RAILWAY_API_TOKEN:
        raise HTTPException(status_code=503, detail="تكامل Railway غير مضبوط")
    try:
        response = requests.post(
            RAILWAY_API,
            headers={"Authorization": f"Bearer {RAILWAY_API_TOKEN}", "Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}},
            timeout=25,
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(status_code=502, detail="تعذر الاتصال بـ Railway حالياً")
    if not response.ok or payload.get("errors"):
        error = (payload.get("errors") or [{}])[0].get("message", "Railway API error")
        raise HTTPException(status_code=502, detail=f"رفض Railway العملية: {str(error)[:240]}")
    return payload.get("data") or {}


BOOTSTRAP = """import json,os,pwd,site,subprocess,sys,urllib.request
req=urllib.request.Request(os.environ['PLATFORM_RUNTIME_URL'],headers={'Authorization':'Bearer '+os.environ['RUNTIME_ACCESS_TOKEN']})
with urllib.request.urlopen(req,timeout=30) as response: payload=json.loads(response.read())
if os.geteuid()==0:
 account=pwd.getpwnam('nobody')
 home='/tmp/telegram-bot-user'
 os.makedirs(home,exist_ok=True)
 os.chown(home,account.pw_uid,account.pw_gid)
 os.environ['HOME']=home
 os.environ['USER']='nobody'
 os.environ['LOGNAME']='nobody'
 os.setgroups([])
 os.setgid(account.pw_gid)
 os.setuid(account.pw_uid)
requirements=payload.get('requirements') or []
if requirements: subprocess.run([sys.executable,'-m','pip','install','--user','--disable-pip-version-check','--no-cache-dir',*requirements],check=True,timeout=600)
site.addsitedir(site.getusersitepackages())
filename=payload.get('filename','bot.py')
exec(compile(payload['source'],filename,'exec'),{'__name__':'__main__','__file__':filename})
"""


def railway_start_command() -> str:
    encoded = base64.b64encode(BOOTSTRAP.encode()).decode("ascii")
    python_code = f"import base64;exec(compile(base64.b64decode('{encoded}'),'<railway-bootstrap>','exec'))"
    return "python -c " + shlex.quote(python_code)


def provision_bot(db: Session, bot: Bot, telegram_token: str) -> None:
    require_platform_ready()
    project_name = "bot-" + bot.id.replace("-", "")[:12]
    project_data = railway_request(
        "mutation projectCreate($input: ProjectCreateInput!) { projectCreate(input: $input) { id name } }",
        {"input": {"name": project_name, "description": f"Isolated Telegram bot runtime {bot.id}", "workspaceId": RAILWAY_WORKSPACE_ID, "defaultEnvironmentName": "production", "isPublic": False}},
    )
    project_id = project_data["projectCreate"]["id"]
    bot.railway_project_id = project_id
    db.commit()

    env_data = railway_request(
        "query environments($projectId: String!) { environments(projectId: $projectId) { edges { node { id name } } } }",
        {"projectId": project_id},
    )
    environments = [edge["node"] for edge in env_data.get("environments", {}).get("edges", [])]
    if not environments:
        raise RuntimeError("Railway لم تنشئ بيئة افتراضية للمشروع")
    environment_id = next((env["id"] for env in environments if env.get("name") == "production"), environments[0]["id"])
    bot.railway_environment_id = environment_id
    db.commit()

    service_data = railway_request(
        "mutation serviceCreate($input: ServiceCreateInput!) { serviceCreate(input: $input) { id name } }",
        {"input": {"projectId": project_id, "name": "telegram-bot", "source": {"image": "python:3.12-slim"}}},
    )
    service_id = service_data["serviceCreate"]["id"]
    bot.railway_service_id = service_id
    db.commit()

    runtime_token = secrets.token_urlsafe(48)
    bot.runtime_token_hash = sha256_hex(runtime_token)
    variables = {
        "BOT_TOKEN": telegram_token,
        "PLATFORM_RUNTIME_URL": f"{PUBLIC_BASE_URL}/api/runtime/{bot.id}",
        "RUNTIME_ACCESS_TOKEN": runtime_token,
    }
    railway_request(
        "mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }",
        {"input": {"projectId": project_id, "environmentId": environment_id, "serviceId": service_id, "variables": variables, "replace": True, "skipDeploys": True}},
    )
    railway_request(
        "mutation serviceInstanceUpdate($serviceId: String!, $environmentId: String!, $input: ServiceInstanceUpdateInput!) { serviceInstanceUpdate(serviceId: $serviceId, environmentId: $environmentId, input: $input) }",
        {"serviceId": service_id, "environmentId": environment_id, "input": {"startCommand": railway_start_command(), "restartPolicyType": "ON_FAILURE", "restartPolicyMaxRetries": 10, "sleepApplication": False}},
    )
    railway_request(
        "mutation serviceInstanceDeployV2($serviceId: String!, $environmentId: String!) { serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId) }",
        {"serviceId": service_id, "environmentId": environment_id},
    )
    bot.status = "starting"
    bot.last_error = None
    db.commit()


def owned_bot(db: Session, auth: AuthContext, bot_id: str) -> Bot:
    try:
        parsed = str(uuid.UUID(bot_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    bot = db.scalar(select(Bot).where(Bot.id == parsed, Bot.owner_id == auth.user.id))
    if not bot:
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    return bot


def bot_payload(bot: Bot) -> dict[str, Any]:
    return {
        "id": bot.id,
        "name": bot.name,
        "status": bot.status,
        "created_at": bot.created_at.isoformat() + "Z",
        "updated_at": bot.updated_at.isoformat() + "Z",
        "last_error": bot.last_error,
        "has_runtime": bool(bot.railway_service_id),
    }


@app.get("/api/bots")
def list_bots(auth: AuthContext = Depends(auth_context), db: Session = Depends(get_db)):
    bots = db.scalars(select(Bot).where(Bot.owner_id == auth.user.id).order_by(Bot.created_at.desc())).all()
    return {"bots": [bot_payload(bot) for bot in bots], "limits": {"per_user": MAX_BOTS_PER_USER, "total": MAX_TOTAL_BOTS}}


@app.post("/api/bots")
async def create_bot(
    name: str = Form(...),
    telegram_token: str = Form(...),
    source_file: UploadFile = File(...),
    requirements_file: UploadFile | None = File(None),
    auth: AuthContext = Depends(csrf_context),
    db: Session = Depends(get_db),
):
    require_platform_ready()
    name = " ".join(name.strip().split())
    if not name or len(name) > 100:
        raise HTTPException(status_code=422, detail="اسم البوت مطلوب وبحد أقصى 100 حرف")
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", telegram_token.strip()):
        raise HTTPException(status_code=422, detail="صيغة توكن Telegram غير صحيحة")
    if not (source_file.filename or "").lower().endswith(".py"):
        raise HTTPException(status_code=422, detail="ارفع ملف Python بامتداد .py فقط")
    source_bytes = await source_file.read(MAX_SOURCE_BYTES + 1)
    if not source_bytes or len(source_bytes) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail="حجم ملف البوت فارغ أو يتجاوز 256 كيلوبايت")
    try:
        source_code = source_bytes.decode("utf-8")
        compile(source_code, source_file.filename or "bot.py", "exec")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="يجب أن يكون ملف Python بترميز UTF-8")
    except SyntaxError as exc:
        raise HTTPException(status_code=422, detail=f"خطأ صياغة Python في السطر {exc.lineno}")
    if re.search(r"\b\d{5,15}:[A-Za-z0-9_-]{20,}\b", source_code):
        raise HTTPException(status_code=422, detail="لا تضع توكن Telegram داخل الملف؛ اقرأه من متغير البيئة BOT_TOKEN")

    requirements: list[str] = []
    if requirements_file and requirements_file.filename:
        if not requirements_file.filename.lower().endswith(".txt"):
            raise HTTPException(status_code=422, detail="ملف المكتبات الاختياري يجب أن يكون requirements.txt")
        raw_requirements = await requirements_file.read(MAX_REQUIREMENTS_BYTES + 1)
        if len(raw_requirements) > MAX_REQUIREMENTS_BYTES:
            raise HTTPException(status_code=413, detail="ملف المكتبات يتجاوز 8 كيلوبايت")
        try:
            requirements = requirements_from_text(raw_requirements.decode("utf-8"))
        except UnicodeDecodeError:
            raise HTTPException(status_code=422, detail="ملف المكتبات يجب أن يكون UTF-8")

    if DATABASE_URL.startswith("postgresql"):
        db.execute(text("SELECT pg_advisory_xact_lock(814560942713)"))
    user_bot_count = db.scalar(select(func.count(Bot.id)).where(Bot.owner_id == auth.user.id)) or 0
    total_bot_count = db.scalar(select(func.count(Bot.id))) or 0
    if user_bot_count >= MAX_BOTS_PER_USER:
        raise HTTPException(status_code=429, detail=f"وصلت إلى الحد الحالي وهو {MAX_BOTS_PER_USER} بوت لكل حساب")
    if total_bot_count >= MAX_TOTAL_BOTS:
        raise HTTPException(status_code=503, detail="وصلت المنصة إلى الحد التشغيلي للبوتات؛ تواصل مع المشرف")

    runtime_token = secrets.token_urlsafe(48)
    bot = Bot(
        owner_id=auth.user.id,
        name=name,
        source_code=source_code,
        source_filename=Path(source_file.filename or "bot.py").name[:120],
        requirements=requirements,
        runtime_token_hash=sha256_hex(runtime_token),
        status="provisioning",
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    try:
        provision_bot(db, bot, telegram_token.strip())
    except HTTPException as exc:
        bot.status = "error"
        bot.last_error = str(exc.detail)[:500]
        db.commit()
        raise
    except Exception as exc:
        bot.status = "error"
        bot.last_error = "تعذر تجهيز خدمة البوت. يمكن حذف السجل أو إعادة المحاولة بعد مراجعة إعدادات Railway."
        db.commit()
        raise HTTPException(status_code=502, detail=bot.last_error) from exc
    payload = bot_payload(bot)
    return {**payload, "bot": payload, "message": "تم إنشاء خدمة البوت المعزولة على Railway؛ قد يستغرق ظهور الحالة بضع دقائق."}


@app.get("/api/bots/{bot_id}")
def get_bot(bot_id: str, auth: AuthContext = Depends(auth_context), db: Session = Depends(get_db)):
    bot = owned_bot(db, auth, bot_id)
    if bot.railway_service_id and bot.railway_environment_id:
        try:
            data = railway_request(
                "query serviceInstance($serviceId: String!, $environmentId: String!) { serviceInstance(serviceId: $serviceId, environmentId: $environmentId) { latestDeployment { id status createdAt } } }",
                {"serviceId": bot.railway_service_id, "environmentId": bot.railway_environment_id},
            )
            deployment = (data.get("serviceInstance") or {}).get("latestDeployment")
            if deployment:
                status = deployment.get("status", "").upper()
                bot.status = "running" if status == "SUCCESS" else "error" if status in {"FAILED", "CRASHED"} else "stopped" if status in {"REMOVED", "SLEEPING"} else "starting"
                if bot.status == "error":
                    bot.last_error = "فشل تشغيل البوت؛ راجع سجلات Railway لمعرفة السبب."
                db.commit()
        except HTTPException:
            pass
    return {"bot": bot_payload(bot), "requirements": bot.requirements, "source_filename": bot.source_filename}


@app.post("/api/bots/{bot_id}/start")
def start_bot(bot_id: str, auth: AuthContext = Depends(csrf_context), db: Session = Depends(get_db)):
    require_platform_ready()
    bot = owned_bot(db, auth, bot_id)
    if not bot.railway_service_id or not bot.railway_environment_id:
        raise HTTPException(status_code=409, detail="لم تكتمل خدمة Railway لهذا البوت؛ احذفه ثم أعد إضافته")
    railway_request(
        "mutation serviceInstanceDeployV2($serviceId: String!, $environmentId: String!) { serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId) }",
        {"serviceId": bot.railway_service_id, "environmentId": bot.railway_environment_id},
    )
    bot.status = "starting"
    bot.last_error = None
    db.commit()
    return {"bot": bot_payload(bot)}


@app.post("/api/bots/{bot_id}/stop")
def stop_bot(bot_id: str, auth: AuthContext = Depends(csrf_context), db: Session = Depends(get_db)):
    require_platform_ready()
    bot = owned_bot(db, auth, bot_id)
    if bot.railway_service_id and bot.railway_environment_id:
        data = railway_request(
            "query serviceInstance($serviceId: String!, $environmentId: String!) { serviceInstance(serviceId: $serviceId, environmentId: $environmentId) { latestDeployment { id status } } }",
            {"serviceId": bot.railway_service_id, "environmentId": bot.railway_environment_id},
        )
        deployment = (data.get("serviceInstance") or {}).get("latestDeployment")
        if deployment and deployment.get("id"):
            railway_request(
                "mutation deploymentStop($id: String!) { deploymentStop(id: $id) }",
                {"id": deployment["id"]},
            )
    bot.status = "stopped"
    db.commit()
    return {"bot": bot_payload(bot)}


@app.get("/api/bots/{bot_id}/logs")
def bot_logs(bot_id: str, auth: AuthContext = Depends(auth_context), db: Session = Depends(get_db)):
    bot = owned_bot(db, auth, bot_id)
    if not bot.railway_service_id or not bot.railway_environment_id:
        return {"logs": [], "message": "لم تبدأ خدمة هذا البوت بعد"}
    instance_data = railway_request(
        "query serviceInstance($serviceId: String!, $environmentId: String!) { serviceInstance(serviceId: $serviceId, environmentId: $environmentId) { latestDeployment { id status } } }",
        {"serviceId": bot.railway_service_id, "environmentId": bot.railway_environment_id},
    )
    deployment = (instance_data.get("serviceInstance") or {}).get("latestDeployment")
    if not deployment or not deployment.get("id"):
        return {"logs": [], "message": "لا توجد سجلات نشر بعد"}
    logs_data = railway_request(
        "query deploymentLogs($deploymentId: String!, $limit: Int) { deploymentLogs(deploymentId: $deploymentId, limit: $limit) { timestamp message severity } }",
        {"deploymentId": deployment["id"], "limit": 250},
    )
    entries = logs_data.get("deploymentLogs") or []
    return {"logs": [{"timestamp": entry.get("timestamp"), "message": entry.get("message", ""), "severity": entry.get("severity", "INFO")} for entry in entries]}


@app.delete("/api/bots/{bot_id}")
def delete_bot(bot_id: str, auth: AuthContext = Depends(csrf_context), db: Session = Depends(get_db)):
    require_platform_ready()
    bot = owned_bot(db, auth, bot_id)
    if bot.railway_project_id:
        railway_request(
            "mutation projectDelete($id: String!) { projectDelete(id: $id) }",
            {"id": bot.railway_project_id},
        )
    db.delete(bot)
    db.commit()
    return {"ok": True}


@app.get("/api/runtime/{bot_id}")
def runtime_source(bot_id: str, authorization: str = Header(default=""), db: Session = Depends(get_db)):
    bot = db.get(Bot, bot_id)
    if not bot or not bot.runtime_token_hash or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=404, detail="Not found")
    token = authorization[7:].strip()
    if not hmac.compare_digest(sha256_hex(token), bot.runtime_token_hash):
        raise HTTPException(status_code=404, detail="Not found")
    return JSONResponse(
        {"source": bot.source_code, "filename": bot.source_filename, "requirements": bot.requirements},
        headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/manifest.json", include_in_schema=False)
def manifest():
    return FileResponse(WEB_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")


@app.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"ok": True}


@app.get("/{path:path}", include_in_schema=False)
def spa_fallback(path: str):
    if path.startswith(("api/", "docs", "redoc")):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(WEB_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=not PRODUCTION)

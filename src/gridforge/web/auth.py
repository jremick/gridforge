"""Opt-in hosted authentication, signed cookies, PBKDF2 passwords, Google OAuth & Turnstile."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import HTTPException, Request, Response, status

from gridforge.web import db
from gridforge.web.access import get_client_ip, get_effective_scheme, is_secure_request
from gridforge.web.limits import LimitsConfig, get_capabilities, get_quotas_snapshot
from gridforge.web.schemas import (
    AcceptInviteRequest,
    AuthSessionResponse,
    AuthUserSummary,
)

logger = logging.getLogger(__name__)

GUEST_COOKIE_NAME = "gf_guest"
SESSION_COOKIE_NAME = "gf_session"
OAUTH_STATE_COOKIE_NAME = "gf_oauth_state"

_GUEST_ID_RE = re.compile(r"^[a-f0-9]{16}$")
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes"})
_FALSY: frozenset[str] = frozenset({"0", "false", "no"})
_DUMMY_PASSWORD_HASH: str = (
    "pbkdf2_sha256$310000$000102030405060708090a0b0c0d0e0f$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


_LEGACY_ENV_WARNINGS: tuple[tuple[str, str], ...] = (
    ("GRIDFORGE_TURNSTILE_SITEKEY", "GRIDFORGE_TURNSTILE_SITE_KEY"),
    ("GRIDFORGE_TURNSTILE_SECRET", "GRIDFORGE_TURNSTILE_SECRET_KEY"),
    ("GRIDFORGE_BOOTSTRAP_ADMIN_EMAIL", "GRIDFORGE_ADMIN_EMAIL"),
    ("GRIDFORGE_BOOTSTRAP_ADMIN_PASSWORD", "GRIDFORGE_ADMIN_PASSWORD"),
)

_AUTH_HTTP_CLIENT: httpx.Client | None = None
_ORIG_HTTPX_POST = httpx.post
_ORIG_HTTPX_GET = httpx.get


def _get_auth_http_client() -> httpx.Client:
    global _AUTH_HTTP_CLIENT
    if _AUTH_HTTP_CLIENT is None or _AUTH_HTTP_CLIENT.is_closed:
        _AUTH_HTTP_CLIENT = httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
    return _AUTH_HTTP_CLIENT


def _auth_http_post(*args: Any, **kwargs: Any) -> httpx.Response:
    if httpx.post is not _ORIG_HTTPX_POST:
        return httpx.post(*args, **kwargs)
    return _get_auth_http_client().post(*args, **kwargs)


def _auth_http_get(*args: Any, **kwargs: Any) -> httpx.Response:
    if httpx.get is not _ORIG_HTTPX_GET:
        return httpx.get(*args, **kwargs)
    return _get_auth_http_client().get(*args, **kwargs)


def _warn_misnamed_env_vars() -> None:
    for wrong, right in _LEGACY_ENV_WARNINGS:
        if os.environ.get(wrong, "").strip() and not os.environ.get(right, "").strip():
            logger.warning(
                "Environment variable %s is set but ignored; use %s instead.",
                wrong,
                right,
            )


def _pkce_s256(verifier: str) -> str:
    return _b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())


def _parse_opt_bool_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    norm = raw.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSY:
        return False
    return None


def _opt_str_env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_password(password: str, *, iterations: int = 310_000) -> str:
    """Hash a plaintext password using PBKDF2-HMAC-SHA256."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Verify a plaintext password against a stored PBKDF2-HMAC-SHA256 hash."""
    if not stored_hash or not password:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected_dk = bytes.fromhex(parts[3])
    except (ValueError, TypeError):
        return False
    if iterations < 10_000:
        return False
    actual_dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual_dk, expected_dk)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(encoded: str) -> bytes | None:
    if not encoded:
        return None
    pad = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + pad)
    except Exception:
        return None


def sign_token(payload: str, secret: bytes) -> str:
    """Sign a UTF-8 payload string with HMAC-SHA256."""
    payload_b64 = _b64url_encode(payload.encode("utf-8"))
    sig = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_signed_token(token: str | None, secret: bytes) -> str | None:
    """Verify an HMAC-SHA256 signed token and return its decoded payload, or None."""
    if not token or "." not in token:
        return None
    payload_b64, sig_b64 = token.split(".", 1)
    if not payload_b64 or not sig_b64:
        return None
    expected_sig = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    actual_sig = _b64url_decode(sig_b64)
    if actual_sig is None or not hmac.compare_digest(actual_sig, expected_sig):
        return None
    raw_payload = _b64url_decode(payload_b64)
    if raw_payload is None:
        return None
    try:
        return raw_payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


DEFAULT_SIGNED_URL_TTL_SECONDS: int = 3600


def sign_artifact_token(
    job_id: str,
    resource: str,
    secret: bytes,
    ttl_seconds: int | None = None,
    *,
    now_unix: int | None = None,
) -> str:
    """Sign a short-lived HMAC-SHA256 token scoped to one job artifact or page preview."""
    ttl = DEFAULT_SIGNED_URL_TTL_SECONDS if ttl_seconds is None else max(1, int(ttl_seconds))
    now_ts = int(time.time()) if now_unix is None else int(now_unix)
    payload = json.dumps(
        {"j": job_id, "r": resource, "exp": now_ts + ttl},
        separators=(",", ":"),
    )
    return sign_token(payload, secret)


def verify_artifact_token(
    token: str | None,
    *,
    expected_job_id: str,
    expected_resource: str,
    secret: bytes,
    now_unix: int | None = None,
) -> bool:
    """Verify a short-lived artifact/page HMAC token against job_id, resource, and expiry."""
    decoded = verify_signed_token(token, secret)
    if not decoded:
        return False
    try:
        obj = json.loads(decoded)
    except (ValueError, TypeError):
        return False
    if not isinstance(obj, dict):
        return False
    j = obj.get("j")
    r = obj.get("r")
    exp = obj.get("exp")
    if not isinstance(j, str) or not isinstance(r, str) or not isinstance(exp, int) or isinstance(exp, bool):
        return False
    now_ts = int(time.time()) if now_unix is None else int(now_unix)
    if exp <= now_ts:
        return False
    return hmac.compare_digest(j, expected_job_id) and hmac.compare_digest(r, expected_resource)


def extract_api_key_from_headers(headers: Any) -> tuple[bool, str | None]:
    """Extract an explicitly supplied Bearer or ``X-API-Key`` token from HTTP headers.

    Returns ``(was_supplied, raw_token)``.
    """
    auth_header = headers.get("authorization")
    if auth_header is not None and auth_header.strip():
        parts = auth_header.strip().split(None, 1)
        if parts and parts[0].lower() == "bearer":
            return (True, parts[1].strip() if len(parts) > 1 else "")
    if "x-api-key" in headers:
        return (True, (headers.get("x-api-key") or "").strip())
    return (False, None)


def constant_time_key_matches(supplied: str | None, expected: str | None) -> bool:
    """Compare two API keys in constant time."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


@dataclass(frozen=True)
class AuthConfig:
    """Dynamic environment configuration for GridForge authentication."""

    auth_mode: Literal["none", "hosted"] = "none"
    session_secret: bytes = b""
    session_secret_ephemeral: bool = False
    cookie_secure: bool | None = None
    session_ttl_hours: int = 720
    guest_ttl_hours: int = 24
    signed_url_ttl_seconds: int = DEFAULT_SIGNED_URL_TTL_SECONDS
    api_key: str | None = None
    public_url: str | None = None
    turnstile_site_key: str | None = None
    turnstile_secret_key: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None
    admin_email: str | None = None
    admin_password: str | None = None
    admin_name: str = "Admin"
    brand_name: str = "GridForge"
    brand_subtitle: str = "Studio"
    brand_tagline: str = "Tactical Topographic Print Studio"

    @property
    def turnstile_configured(self) -> bool:
        return bool(self.turnstile_site_key and self.turnstile_secret_key)

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @classmethod
    def from_env(cls) -> AuthConfig:
        raw_mode = os.environ.get("GRIDFORGE_AUTH_MODE", "none").strip().lower()
        if not raw_mode:
            raw_mode = "none"
        if raw_mode not in {"none", "hosted"}:
            raise ValueError(
                f"Invalid GRIDFORGE_AUTH_MODE={raw_mode!r}. Expected 'none' or 'hosted'."
            )
        auth_mode: Literal["none", "hosted"] = "hosted" if raw_mode == "hosted" else "none"
        _warn_misnamed_env_vars()

        raw_secret = os.environ.get("GRIDFORGE_SESSION_SECRET", "").strip()
        ephemeral = False
        if raw_secret:
            secret_bytes = raw_secret.encode("utf-8")
            if auth_mode == "hosted" and len(secret_bytes) < 32:
                raise ValueError(
                    "GRIDFORGE_SESSION_SECRET must be at least 32 bytes in hosted mode "
                    "(generate with `openssl rand -hex 32`)."
                )
        else:
            secret_bytes = secrets.token_bytes(32)
            ephemeral = True
            if auth_mode == "hosted":
                logger.warning(
                    "GRIDFORGE_SESSION_SECRET is unset; using ephemeral secret "
                    "(sessions and guest cookies will invalidate on restart)."
                )

        session_ttl = max(1, int(os.environ.get("GRIDFORGE_SESSION_TTL_HOURS", "720").strip() or "720"))
        guest_ttl = max(1, int(os.environ.get("GRIDFORGE_GUEST_COOKIE_TTL_HOURS", "24").strip() or "24"))
        raw_sig_ttl = os.environ.get("GRIDFORGE_SIGNED_URL_TTL_SECONDS", "").strip()
        signed_url_ttl = (
            max(1, int(raw_sig_ttl))
            if raw_sig_ttl
            else DEFAULT_SIGNED_URL_TTL_SECONDS
        )
        raw_public_url = _opt_str_env("GRIDFORGE_PUBLIC_URL")
        public_url = raw_public_url.rstrip("/") if raw_public_url else None

        return cls(
            auth_mode=auth_mode,
            session_secret=secret_bytes,
            session_secret_ephemeral=ephemeral,
            cookie_secure=_parse_opt_bool_env("GRIDFORGE_COOKIE_SECURE"),
            session_ttl_hours=session_ttl,
            guest_ttl_hours=guest_ttl,
            signed_url_ttl_seconds=signed_url_ttl,
            api_key=_opt_str_env("GRIDFORGE_API_KEY"),
            public_url=public_url,
            turnstile_site_key=_opt_str_env("GRIDFORGE_TURNSTILE_SITE_KEY"),
            turnstile_secret_key=_opt_str_env("GRIDFORGE_TURNSTILE_SECRET_KEY"),
            google_client_id=_opt_str_env("GRIDFORGE_GOOGLE_CLIENT_ID"),
            google_client_secret=_opt_str_env("GRIDFORGE_GOOGLE_CLIENT_SECRET"),
            google_redirect_uri=_opt_str_env("GRIDFORGE_GOOGLE_REDIRECT_URI"),
            admin_email=_opt_str_env("GRIDFORGE_ADMIN_EMAIL"),
            admin_password=_opt_str_env("GRIDFORGE_ADMIN_PASSWORD"),
            admin_name=_opt_str_env("GRIDFORGE_ADMIN_NAME") or "Admin",
            brand_name=_opt_str_env("GRIDFORGE_BRAND_NAME") or "GridForge",
            brand_subtitle=_opt_str_env("GRIDFORGE_BRAND_SUBTITLE") or "Studio",
            brand_tagline=(
                _opt_str_env("GRIDFORGE_BRAND_TAGLINE")
                or "Tactical Topographic Print Studio"
            ),
        )


@dataclass
class AuthContext:
    """Resolved identity and tenant context for a single HTTP request."""

    auth_mode: Literal["none", "hosted"]
    role: Literal["none", "guest", "friend", "admin"]
    authenticated: bool
    owner_id: str
    user: dict[str, Any] | None
    client_ip: str
    new_guest_cookie_value: str | None = None
    clear_session_cookie: bool = False
    session_token_hash: str | None = None
    auth_method: Literal["none", "guest", "session", "api_key"] = "none"
    api_key_id: str | None = None


class AuthManager:
    """Coordinates hosted authentication, signed cookies, OAuth, and Turnstile."""

    def __init__(self, *, db_path: Path, config: AuthConfig) -> None:
        self.db_path = db_path
        self.config = config

    def bootstrap_admin(self) -> dict[str, Any] | None:
        """Seed or ensure the initial admin account when ``GRIDFORGE_ADMIN_EMAIL`` is set."""
        if self.config.auth_mode != "hosted" or not self.config.admin_email:
            return None
        pw_hash = (
            hash_password(self.config.admin_password)
            if self.config.admin_password
            else None
        )
        return db.ensure_seed_admin(
            self.db_path,
            user_id=f"usr_{uuid.uuid4().hex[:12]}",
            email=self.config.admin_email,
            display_name=self.config.admin_name,
            password_hash=pw_hash,
        )

    def _cookie_is_secure(self, request: Request) -> bool:
        if self.config.cookie_secure is not None:
            return self.config.cookie_secure
        return is_secure_request(request)

    def _mint_guest_cookie(self) -> tuple[str, str]:
        guest_id = uuid.uuid4().hex[:16]
        exp_unix = int(time.time()) + self.config.guest_ttl_hours * 3600
        signed = sign_token(f"{guest_id}:{exp_unix}", self.config.session_secret)
        return (guest_id, signed)

    def _parse_guest_cookie(self, raw_cookie: str | None) -> str | None:
        payload = verify_signed_token(raw_cookie, self.config.session_secret)
        if not payload or ":" not in payload:
            return None
        guest_id, exp_str = payload.split(":", 1)
        if not _GUEST_ID_RE.match(guest_id):
            return None
        try:
            exp_unix = int(exp_str)
        except ValueError:
            return None
        if exp_unix <= int(time.time()):
            return None
        return guest_id

    def resolve_request_context(
        self,
        request: Request,
        *,
        issue_guest_cookie: bool = True,
    ) -> AuthContext:
        """Resolve role, owner_id, user, and pending cookie updates for an HTTP request.

        Checks ``Authorization: Bearer <tok>`` and ``X-API-Key: <tok>`` headers BEFORE cookies.
        """
        client_ip = get_client_ip(request)
        key_supplied, raw_api_key = extract_api_key_from_headers(request.headers)

        if self.config.auth_mode == "none":
            if self.config.api_key:
                path = request.url.path
                is_mcp = path == "/mcp" or path.startswith("/mcp/")
                is_mutating_api = path.startswith("/api/") and request.method.upper() not in {
                    "GET",
                    "HEAD",
                    "OPTIONS",
                }
                if key_supplied:
                    if not constant_time_key_matches(raw_api_key, self.config.api_key):
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired API key.",
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                    return AuthContext(
                        auth_mode="none",
                        role="none",
                        authenticated=True,
                        owner_id="local",
                        user=None,
                        client_ip=client_ip,
                        auth_method="api_key",
                    )
                if is_mutating_api or (is_mcp and request.method.upper() != "OPTIONS"):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="API key required.",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            return AuthContext(
                auth_mode="none",
                role="none",
                authenticated=False,
                owner_id="local",
                user=None,
                client_ip=client_ip,
                auth_method="none",
            )

        # Hosted mode: check Bearer / X-API-Key headers BEFORE session/guest cookies.
        if key_supplied:
            if not raw_api_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired API key.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token_hash = sha256_hex(raw_api_key)
            hit = db.get_api_key_and_user_by_token_hash(self.db_path, token_hash)
            if hit is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired API key.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            api_key_row, user = hit
            if user.get("disabled"):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired API key.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                db.touch_api_key_last_used(self.db_path, str(api_key_row["key_id"]))
            except Exception as exc:
                logger.debug("Failed to update api_key last_used_at: %s", exc)
            role_val: Literal["friend", "admin"] = (
                "admin" if user["role"] == "admin" else "friend"
            )
            return AuthContext(
                auth_mode="hosted",
                role=role_val,
                authenticated=True,
                owner_id=str(user["user_id"]),
                user=user,
                client_ip=client_ip,
                auth_method="api_key",
                api_key_id=str(api_key_row["key_id"]),
            )

        clear_session = False
        raw_session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if raw_session_cookie:
            raw_session_token = verify_signed_token(
                raw_session_cookie, self.config.session_secret
            )
            if raw_session_token:
                token_hash = sha256_hex(raw_session_token)
                user = db.get_session_and_user_by_token_hash(self.db_path, token_hash)
                if user is not None and not user.get("disabled"):
                    role_val_sess: Literal["friend", "admin"] = (
                        "admin" if user["role"] == "admin" else "friend"
                    )
                    return AuthContext(
                        auth_mode="hosted",
                        role=role_val_sess,
                        authenticated=True,
                        owner_id=str(user["user_id"]),
                        user=user,
                        client_ip=client_ip,
                        session_token_hash=token_hash,
                        auth_method="session",
                    )
            clear_session = True

        existing_guest_id = self._parse_guest_cookie(request.cookies.get(GUEST_COOKIE_NAME))
        new_guest_cookie: str | None = None
        if existing_guest_id is None:
            guest_id, minted = self._mint_guest_cookie()
            if issue_guest_cookie:
                new_guest_cookie = minted
        else:
            guest_id = existing_guest_id

        return AuthContext(
            auth_mode="hosted",
            role="guest",
            authenticated=False,
            owner_id=f"guest:{guest_id}",
            user=None,
            client_ip=client_ip,
            new_guest_cookie_value=new_guest_cookie,
            clear_session_cookie=clear_session,
            auth_method="guest",
        )

    def apply_auth_cookies(
        self,
        response: Response,
        ctx: AuthContext,
        request: Request,
    ) -> None:
        """Attach pending ``gf_guest`` or clear invalid ``gf_session`` cookies on ``response``."""
        if ctx.auth_mode != "hosted":
            return
        secure = self._cookie_is_secure(request)
        if ctx.clear_session_cookie:
            response.delete_cookie(
                key=SESSION_COOKIE_NAME,
                path="/",
                httponly=True,
                samesite="lax",
                secure=secure,
            )
        if ctx.new_guest_cookie_value is not None:
            response.set_cookie(
                key=GUEST_COOKIE_NAME,
                value=ctx.new_guest_cookie_value,
                max_age=self.config.guest_ttl_hours * 3600,
                path="/",
                httponly=True,
                samesite="lax",
                secure=secure,
            )

    def set_session_cookie(
        self,
        response: Response,
        raw_session_token: str,
        request: Request,
    ) -> None:
        secure = self._cookie_is_secure(request)
        signed = sign_token(raw_session_token, self.config.session_secret)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=signed,
            max_age=self.config.session_ttl_hours * 3600,
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure,
        )

    def delete_session_cookie(self, response: Response, request: Request) -> None:
        secure = self._cookie_is_secure(request)
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure,
        )

    def _create_user_session(self, user_id: str) -> str:
        raw_token = secrets.token_urlsafe(32)
        expires_at = db._dt_to_iso(
            datetime.now(timezone.utc) + timedelta(hours=self.config.session_ttl_hours)
        )
        db.create_session(
            self.db_path,
            session_id=uuid.uuid4().hex[:16],
            token_hash=sha256_hex(raw_token),
            user_id=user_id,
            expires_at=expires_at,
        )
        return raw_token

    def build_session_response(
        self,
        ctx: AuthContext,
        limits_cfg: LimitsConfig,
    ) -> AuthSessionResponse:
        """Build the ``GET /api/auth/session`` response payload."""
        caps = get_capabilities(
            ctx.role,
            limits_cfg,
            turnstile_sitekey=self.config.turnstile_site_key,
            turnstile_configured=self.config.turnstile_configured,
            google_oauth_enabled=self.config.google_oauth_enabled,
        )
        quotas = get_quotas_snapshot(
            self.db_path,
            role=ctx.role,
            owner_id=ctx.owner_id,
            client_ip=ctx.client_ip,
            user_dict=ctx.user,
            cfg=limits_cfg,
        )
        user_summary: AuthUserSummary | None = None
        if ctx.authenticated and ctx.user is not None:
            role_val: Literal["friend", "admin"] = (
                "admin" if ctx.user["role"] == "admin" else "friend"
            )
            user_summary = AuthUserSummary(
                user_id=str(ctx.user["user_id"]),
                email=str(ctx.user["email"]),
                display_name=str(ctx.user.get("display_name") or ""),
                role=role_val,
            )

        return AuthSessionResponse(
            auth_mode=ctx.auth_mode,
            authenticated=ctx.authenticated,
            user=user_summary,
            role=ctx.role,
            capabilities=caps,
            quotas=quotas,
            brand_name=self.config.brand_name,
            brand_subtitle=self.config.brand_subtitle,
            brand_tagline=self.config.brand_tagline,
        )

    def authenticate_password(self, email: str, password: str) -> tuple[dict[str, Any], str]:
        """Validate email/password credentials and return ``(user_dict, raw_session_token)``."""
        if self.config.auth_mode != "hosted":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Login is disabled when GRIDFORGE_AUTH_MODE=none.",
            )
        user = db.get_user_by_email(self.db_path, email)
        stored_hash = user.get("password_hash") if user is not None else None
        if not stored_hash:
            verify_password(password or "dummy", _DUMMY_PASSWORD_HASH)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
            )
        if not verify_password(password, stored_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
            )
        assert user is not None
        if user.get("disabled"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account has been disabled by an administrator.",
            )
        raw_token = self._create_user_session(str(user["user_id"]))
        return (user, raw_token)

    def logout_session(self, ctx: AuthContext) -> None:
        if ctx.session_token_hash:
            db.delete_session_by_token_hash(self.db_path, ctx.session_token_hash)

    def accept_invite(self, req: AcceptInviteRequest) -> tuple[dict[str, Any], str]:
        """Atomically redeem an invite with email/password and create an authenticated session."""
        if self.config.auth_mode != "hosted":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite redemption is disabled when GRIDFORGE_AUTH_MODE=none.",
            )
        raw_session_token = secrets.token_urlsafe(32)
        expires_at = db._dt_to_iso(
            datetime.now(timezone.utc) + timedelta(hours=self.config.session_ttl_hours)
        )
        try:
            user = db.accept_invite_and_create_user(
                self.db_path,
                token_hash=sha256_hex(req.token.strip()),
                new_user_id=f"usr_{uuid.uuid4().hex[:12]}",
                supplied_email=req.email,
                display_name=req.display_name,
                password_hash=hash_password(req.password),
                google_sub=None,
                session_id=uuid.uuid4().hex[:16],
                session_token_hash=sha256_hex(raw_session_token),
                session_expires_at=expires_at,
            )
        except db.InviteActionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return (user, raw_session_token)

    def verify_turnstile_if_required(
        self,
        request: Request,
        ctx: AuthContext,
        *,
        body_token: str | None = None,
    ) -> None:
        """Verify Cloudflare Turnstile token for anonymous guest requests when configured."""
        if (
            self.config.auth_mode != "hosted"
            or ctx.role != "guest"
            or not self.config.turnstile_configured
        ):
            return

        header_token = (request.headers.get("x-turnstile-token") or "").strip()
        token = header_token or (body_token or "").strip()
        if not token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Human verification (Turnstile) is required.",
            )

        assert self.config.turnstile_secret_key is not None
        try:
            resp = _auth_http_post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": self.config.turnstile_secret_key,
                    "response": token,
                    "remoteip": ctx.client_ip,
                },
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning("Turnstile verification request error: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Turnstile verification failed; please try again.",
            ) from exc

        if resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Turnstile verification failed; please try again.",
            )
        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Turnstile verification failed; please try again.",
            ) from exc

        if not isinstance(payload, dict) or not payload.get("success"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Turnstile verification failed; please try again.",
            )

        cf_hostname = str(payload.get("hostname") or "").strip().lower()
        allowed_hosts_raw = os.environ.get("GRIDFORGE_ALLOWED_HOSTS", "").strip()
        if cf_hostname and allowed_hosts_raw:
            allowed_set = {
                h.strip().lower().split(":", 1)[0]
                for h in allowed_hosts_raw.split(",")
                if h.strip()
            }
            allowed_set.update({"localhost", "127.0.0.1", "example.com"})
            if cf_hostname not in allowed_set:
                logger.warning("Turnstile token hostname mismatch: %s", cf_hostname)
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Turnstile verification failed; please try again.",
                )

    def _resolve_google_redirect_uri(self, request: Request) -> str:
        if self.config.google_redirect_uri:
            return self.config.google_redirect_uri
        raw_url = str(request.url_for("api_google_callback"))
        scheme = get_effective_scheme(request.scope, request.headers)
        parsed = urllib.parse.urlsplit(raw_url)
        if parsed.scheme != scheme:
            raw_url = urllib.parse.urlunsplit(
                (scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
            )
        return raw_url

    def build_google_oauth_start(
        self,
        request: Request,
        *,
        invite_token: str | None = None,
    ) -> tuple[str, str]:
        """Build the Google OAuth consent redirect URL and signed state cookie value."""
        if self.config.auth_mode != "hosted" or not self.config.google_oauth_enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google sign-in is not configured on this server.",
            )
        state = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        state_payload = json.dumps(
            {
                "state": state,
                "invite_token": (invite_token or "").strip(),
                "code_verifier": code_verifier,
                "exp": int(time.time()) + 600,
            },
            separators=(",", ":"),
        )
        signed_cookie = sign_token(state_payload, self.config.session_secret)
        redirect_uri = self._resolve_google_redirect_uri(request)
        query = urllib.parse.urlencode(
            {
                "client_id": self.config.google_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "code_challenge": _pkce_s256(code_verifier),
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return (f"https://accounts.google.com/o/oauth2/v2/auth?{query}", signed_cookie)

    def handle_google_oauth_callback(
        self,
        request: Request,
        *,
        code: str | None,
        state: str | None,
        error: str | None,
    ) -> tuple[dict[str, Any], str]:
        """Verify OAuth state, exchange code, and log in an existing or invited user."""
        if self.config.auth_mode != "hosted" or not self.config.google_oauth_enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google sign-in is not configured on this server.",
            )
        if error:
            safe_err = re.sub(r"[^a-zA-Z0-9_-]", "", error)[:40] or "access_denied"
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Google sign-in declined ({safe_err}).",
            )
        if not code or not state:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing OAuth code or state parameter.",
            )

        raw_cookie = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
        decoded_state = verify_signed_token(raw_cookie, self.config.session_secret)
        if not decoded_state:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or missing OAuth state cookie.",
            )
        try:
            state_obj = json.loads(decoded_state)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Malformed OAuth state cookie.",
            ) from exc

        expected_state = str(state_obj.get("state") or "")
        exp_unix = int(state_obj.get("exp") or 0)
        if exp_unix <= int(time.time()) or not hmac.compare_digest(expected_state, state):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="OAuth state mismatch or expired.",
            )

        redirect_uri = self._resolve_google_redirect_uri(request)
        token_data_form: dict[str, Any] = {
            "code": code,
            "client_id": self.config.google_client_id,
            "client_secret": self.config.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        code_verifier = str(state_obj.get("code_verifier") or "").strip()
        if code_verifier:
            token_data_form["code_verifier"] = code_verifier
        try:
            token_resp = _auth_http_post(
                "https://oauth2.googleapis.com/token",
                data=token_data_form,
                timeout=10.0,
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
            access_token = str(token_data.get("access_token") or "")
            if not access_token:
                raise ValueError("Missing access_token")
            info_resp = _auth_http_get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0,
            )
            info_resp.raise_for_status()
            profile = info_resp.json()
        except Exception as exc:
            logger.warning("Google OAuth provider exchange failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to verify identity with Google OAuth.",
            ) from exc

        email = str(profile.get("email") or "").strip().lower()
        raw_ev = profile.get("email_verified")
        email_verified = (raw_ev is True) or (
            isinstance(raw_ev, str) and raw_ev.strip().lower() == "true"
        )
        google_sub = str(profile.get("sub") or "").strip() or None
        display_name = str(profile.get("name") or "").strip() or None

        if not email or not email_verified:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Google account must have a verified email address.",
            )

        user = (
            db.get_user_by_google_sub(self.db_path, google_sub)
            if google_sub
            else None
        )
        if user is None:
            user = db.get_user_by_email(self.db_path, email)

        if user is not None:
            if user.get("disabled"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This account has been disabled by an administrator.",
                )
            if google_sub and not user.get("google_sub"):
                updated = db.update_user(
                    self.db_path,
                    str(user["user_id"]),
                    google_sub=google_sub,
                    set_google_sub=True,
                )
                if updated is not None:
                    user = updated
            raw_session_token = self._create_user_session(str(user["user_id"]))
            return (user, raw_session_token)

        invite_token = str(state_obj.get("invite_token") or "").strip()
        if not invite_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account not found; an admin invite is required.",
            )

        raw_session_token = secrets.token_urlsafe(32)
        expires_at = db._dt_to_iso(
            datetime.now(timezone.utc) + timedelta(hours=self.config.session_ttl_hours)
        )
        try:
            new_user = db.accept_invite_and_create_user(
                self.db_path,
                token_hash=sha256_hex(invite_token),
                new_user_id=f"usr_{uuid.uuid4().hex[:12]}",
                supplied_email=email,
                display_name=display_name,
                password_hash=None,
                google_sub=google_sub,
                session_id=uuid.uuid4().hex[:16],
                session_token_hash=sha256_hex(raw_session_token),
                session_expires_at=expires_at,
            )
        except db.InviteActionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return (new_user, raw_session_token)

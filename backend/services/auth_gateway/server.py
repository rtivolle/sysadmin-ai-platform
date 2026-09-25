#!/usr/bin/env python3
"""
Hardened ForwardAuth Gateway Service for Traefik.
Enforces genuine Bearer token and session cookie authentication for 10 sysadmin accounts,
strips spoofed client headers, blocks unauthenticated requests (HTTP 401),
and integrates with Valkey for session management and pre-admission quota checks.
"""
import os
import sys
import json
import secrets
import asyncio
import hashlib
import hmac
import datetime
from pathlib import Path
from typing import Optional, Dict

import redis
from fastapi import FastAPI, Request, Response, HTTPException, status
from fastapi.responses import JSONResponse
import uvicorn

# Ensure auth_gateway dir is in sys.path
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

try:
    from services.auth_gateway.quota_manager import QuotaManager, QuotaExceededException, quota_mgr
except ImportError:
    from quota_manager import QuotaManager, QuotaExceededException, quota_mgr

# Durable control store (the PostgreSQL leg). Importing it never imports a
# database driver: the store is contacted only when SYSADMIN_CONTROL_STORE is
# 'postgres' (or an explicit SYSADMIN_DATABASE_URL is set). A broken import must
# not silently downgrade an explicitly selected store to the file map, so the
# failure is remembered and surfaced as a fail-closed 503 on use.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
_CONTROL_STORE_IMPORT_ERROR: Optional[str] = None
try:
    from services.control_store import (  # noqa: E402
        ControlStoreError,
        ControlStoreUnavailable,
        open_key_store,
    )
except ImportError as _exc:  # pragma: no cover - only on a broken checkout
    ControlStoreError = RuntimeError  # type: ignore[assignment,misc]
    ControlStoreUnavailable = ConnectionError  # type: ignore[assignment,misc]
    open_key_store = None  # type: ignore[assignment]
    _CONTROL_STORE_IMPORT_ERROR = str(_exc)

from services.agent_tools.audit import log_audit_event
from services.logging_setup import RequestLoggingMiddleware, configure, get_logger, log_event

app = FastAPI(title="Sysadmin AI Platform Hardened Auth Gateway", version="2.0.0")
app.add_middleware(RequestLoggingMiddleware, service="auth_gateway")
_LOG = get_logger("auth_gateway.server")


async def _audit_async(**event_kwargs):
    """Best-effort, non-blocking audit emission for async handlers.

    Offloads the blocking VictoriaLogs/outbox write to a worker thread so a
    flood of 401s or logins cannot stall the event loop. Any failure is logged
    to stderr and never affects the request outcome.
    """
    try:
        await asyncio.to_thread(log_audit_event, **event_kwargs)
    except Exception as exc:
        print(f"[!] Audit emission failed: {exc}", file=sys.stderr)


@app.exception_handler(ConnectionError)
async def shared_state_unavailable(_request: Request, _exc: ConnectionError):
    """A required shared store (Valkey, or the durable control store) is down.

    Fail closed with 503 rather than continuing on local state, and do not echo
    the exception text: it can name an internal DSN or store. The class name is
    logged so an operator can tell a store outage from a bug, without risking a
    credential in the log.
    """
    _LOG.warning("shared state store unavailable: %s", _exc.__class__.__name__)
    return JSONResponse(status_code=503, content={"detail": "Shared state store unavailable"})

ROOT_DIR = CURRENT_DIR.parent.parent
KEYS_DIR = ROOT_DIR / "config" / "keys"
WORKSPACES_DIR = ROOT_DIR / "data" / "workspaces"

# Valkey Connection
VALKEY_HOST = os.getenv("VALKEY_HOST", "127.0.0.1")
VALKEY_PORT = int(os.getenv("VALKEY_PORT", 6379))
VALKEY_PASS = os.getenv("VALKEY_PASSWORD", "CONFIGURE_VIA_PLATFORM_SH")

valkey_client: Optional[redis.Redis] = None

def get_valkey() -> Optional[redis.Redis]:
    global valkey_client
    if valkey_client is None:
        try:
            valkey_client = redis.Redis(
                host=VALKEY_HOST,
                port=VALKEY_PORT,
                password=VALKEY_PASS,
                decode_responses=True,
                socket_timeout=2.0
            )
            valkey_client.ping()
        except Exception:
            valkey_client = None
    return valkey_client

VALID_USERS = {f"sysadmin-{i:02d}" for i in range(1, 11)} | {"emergency-p1-oncall"}
LOGIN_CREDENTIALS_FILE = Path(os.getenv("SYSADMIN_LOGIN_CREDENTIALS_FILE", str(KEYS_DIR / "login-credentials.json")))

# Master API token for administrative control
MASTER_TOKEN = os.getenv("LITELLM_MASTER_KEY", "")


def valid_login_password(username: str, password: str) -> bool:
    """Compare a provisioned PBKDF2-SHA256 credential with constant-time equality."""
    if username not in VALID_USERS or not password:
        return False
    try:
        credentials = json.loads(LOGIN_CREDENTIALS_FILE.read_text())
        salt_hex, digest_hex = credentials[username].split("$", 1)
        expected = bytes.fromhex(digest_hex)
        calculated = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 600_000)
        return hmac.compare_digest(calculated, expected)
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return False

def load_valid_tokens() -> Dict[str, str]:
    """Loads provisioned API keys from backend/config/keys/ into a token -> user_id mapping."""
    tokens = {MASTER_TOKEN: "sysadmin-admin"} if MASTER_TOKEN else {}
    if KEYS_DIR.is_dir():
        master_file = KEYS_DIR / "master.key"
        if master_file.is_file():
            master_tok = master_file.read_text().strip()
            if master_tok:
                tokens[master_tok] = "sysadmin-admin"
        for key_file in KEYS_DIR.glob("*.key"):
            if key_file.name in {"master.key", "valkey-password.key"}:
                continue
            user_name = key_file.stem
            user_id = "emergency-p1-oncall" if user_name == "emergency-p1" else user_name
            try:
                token_val = key_file.read_text().strip()
                if token_val:
                    tokens[token_val] = user_id
            except Exception as e:
                print(f"[!] Warning: Failed to read key file {key_file}: {e}", file=sys.stderr)
    return tokens


_key_store: Optional[object] = None


def master_credential() -> Optional[str]:
    """The recovery/admin credential: the injected master key, else master.key."""
    if MASTER_TOKEN:
        return MASTER_TOKEN
    try:
        value = (KEYS_DIR / "master.key").read_text().strip()
    except OSError:
        return None
    return value or None


def get_key_store():
    """Return the durable key store, or None when file mode is selected.

    Raises ``ConnectionError`` (HTTP 503) when the store is selected but cannot
    be built or reached: an authoritative store must never be bypassed.
    """
    global _key_store
    selected = (
        os.getenv("SYSADMIN_CONTROL_STORE", "").strip().lower() in {"postgres", "postgresql"}
        or bool(os.getenv("SYSADMIN_DATABASE_URL", "").strip())
    )
    if _key_store is None:
        if open_key_store is None:
            if selected:
                raise ConnectionError(
                    "Durable control store selected but its module is unavailable: "
                    f"{_CONTROL_STORE_IMPORT_ERROR}"
                )
            return None
        try:
            _key_store = open_key_store()
        except ControlStoreError as exc:
            raise ConnectionError(f"Durable control store misconfigured: {exc}") from exc
        if _key_store is None and selected:
            raise ConnectionError("Durable control store selected but no DSN could be resolved")
    return _key_store


def resolve_identity(token: str) -> Optional[str]:
    """Resolve a bearer token to a user_id, or None when it is not valid.

    With ``SYSADMIN_CONTROL_STORE=postgres` the durable store is authoritative
    and an outage raises ``ConnectionError`` instead of falling back to the file
    map (ADR-0001 corrective work, AGENTS.md §4). The master credential always
    resolves to the admin identity so the store stays administrable.
    """
    clean = (token or "").strip()
    if not clean:
        return None
    master = master_credential()
    if master and hmac.compare_digest(clean, master):
        return "sysadmin-admin"
    store = get_key_store()
    if store is None:
        return load_valid_tokens().get(clean)
    return store.resolve(clean)


def authenticate_request(request: Request) -> tuple[str, str]:
    """Validate credentials directly; never accept forwarded identity headers."""
    auth_header = request.headers.get("authorization", "").strip()
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        user_id = resolve_identity(token)
        if user_id:
            return user_id, "bearer_token"

    session_cookie = request.cookies.get("session_id", "").strip()
    if session_cookie:
        try:
            store = get_valkey()
            if store:
                user_id = store.get(f"session:{session_cookie}")
                if user_id in VALID_USERS:
                    return user_id, "session_cookie"
        except Exception as exc:
            print(f"[!] Valkey session lookup error: {exc}", file=sys.stderr)
    raise HTTPException(status_code=401, detail="Authentication required")


def role_for_user(user_id: str) -> str:
    """Derive authority from validated identity, never from request headers."""
    if user_id == "sysadmin-admin":
        return "admin"
    return "p1-operator" if quota_mgr.is_p1_elevated(user_id) else "sysadmin"

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "auth_gateway", "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def require_quota_admin(request: Request) -> str:
    user_id, _ = authenticate_request(request)
    if user_id != "sysadmin-admin":
        raise HTTPException(status_code=403, detail="Administrator required")
    return user_id


@app.get("/api/v1/admin/quotas")
def admin_quotas(request: Request):
    require_quota_admin(request)
    try:
        return {"users": [quota_mgr.quota_snapshot(user) for user in sorted(VALID_USERS)],
                "bounds": QuotaManager.LIMIT_BOUNDS}
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="Shared quota state unavailable") from exc


@app.post("/api/v1/admin/quotas/{user_id}")
async def admin_set_quota(user_id: str, request: Request):
    reviewer = require_quota_admin(request)
    if user_id not in VALID_USERS:
        raise HTTPException(status_code=404, detail="Unknown quota user")
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"limits"}:
            raise ValueError("limits object is required; use {} to restore defaults")
        quota_mgr.set_limits(user_id, body["limits"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="Shared quota state unavailable") from exc
    from services.agent_tools.audit import log_audit_event
    log_audit_event(user_id=reviewer, session_id="", tool_name="quota_update",
                    action="quota_update", parameters={"user_id": user_id, "limits": body["limits"]},
                    exit_code=0, duration_ms=0)
    return {"status": "updated", "user_id": user_id, "overrides": body["limits"]}

def require_key_store():
    """Return the durable key store or explain, fail-closed, why it is absent."""
    store = get_key_store()
    if store is None:
        raise HTTPException(
            status_code=409,
            detail="Durable key store is not enabled (set SYSADMIN_CONTROL_STORE=postgres)",
        )
    return store


async def _optional_label(request: Request) -> str:
    try:
        body = await request.json()
    except Exception:
        return ""
    if isinstance(body, dict) and isinstance(body.get("label"), str):
        return body["label"][:120]
    return ""


@app.get("/api/v1/admin/keys")
def admin_list_keys(request: Request, user: Optional[str] = None):
    """Metadata for live keys; token material and hashes are never returned."""
    require_quota_admin(request)
    try:
        return {"keys": require_key_store().active_keys(user)}
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="Durable control store unavailable") from exc


@app.post("/api/v1/admin/keys/{user_id}/rotate")
async def admin_rotate_key(user_id: str, request: Request):
    """Atomically replace every live key of a user; the new key is shown once."""
    admin = require_quota_admin(request)
    if user_id not in VALID_USERS:
        raise HTTPException(status_code=404, detail="Unknown identity")
    label = await _optional_label(request)
    try:
        token = require_key_store().rotate(user_id, label=label, created_by=admin)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="Durable control store unavailable") from exc
    # Never cached, never audited, never logged: this is the only copy the
    # operator receives. The store keeps only the SHA-256 of the token.
    return JSONResponse(
        status_code=200,
        content={"status": "rotated", "user_id": user_id, "key": token, "shown_once": True},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/v1/admin/keys/{user_id}/revoke")
async def admin_revoke_key(user_id: str, request: Request):
    """Revoke every live key of a user; validation is immediate (no cached map)."""
    admin = require_quota_admin(request)
    if user_id not in VALID_USERS:
        raise HTTPException(status_code=404, detail="Unknown identity")
    try:
        revoked = require_key_store().revoke(user_id, actor=admin)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="Durable control store unavailable") from exc
    return {"status": "revoked", "user_id": user_id, "revoked": revoked}


@app.get("/verify")
@app.post("/verify")
@app.get("/api/v1/auth/verify")
@app.post("/api/v1/auth/verify")
async def verify(request: Request):
    """
    Traefik ForwardAuth verification endpoint.
    STRICT SECURITY RULES:
    1. Completely ignores incoming client-supplied 'X-User' or 'X-Forwarded-*' headers for identity.
    2. Validates Bearer token against loaded token directory (exact match).
    3. Validates session_id cookie against Valkey session store.
    4. Rejects missing/invalid credentials with HTTP 401.
    5. Checks daily token quota in Valkey; returns HTTP 429 if exhausted.
    6. Injects trusted identity headers on HTTP 200.
    """
    try:
        user_id, auth_method = authenticate_request(request)
    except HTTPException:
        reason = "invalid_credentials" if (
            request.headers.get("authorization", "").strip()
            or request.cookies.get("session_id", "").strip()
        ) else "missing_credentials"
        await _audit_async(
            user_id="anonymous",
            session_id="",
            tool_name="auth_denied",
            action="auth_denied",
            exit_code=1,
            extra={"reason": reason},
        )
        return Response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=json.dumps({
                "error": "unauthorized",
                "message": "Authentication required. Provide a valid Bearer token or session cookie."
            }),
            media_type="application/json",
            headers={"WWW-Authenticate": 'Bearer realm="sysadmin-platform"'}
        )

    # 4. Check Daily Quota (2M Tokens/Day) with Midnight Rollover
    try:
        quota_mgr.check_daily_token_budget(user_id)
    except QuotaExceededException as qe:
        return Response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=json.dumps({
                "error": "quota_exhausted",
                "user_id": user_id,
                "consumed_tokens": qe.current,
                "limit": qe.limit,
                "message": str(qe)
            }),
            media_type="application/json"
        )
    except ConnectionError:
        return Response(status_code=503, content=json.dumps({"error": "quota_state_unavailable"}), media_type="application/json")

    # 5. Determine Role and Isolated Workspace Path
    role = role_for_user(user_id)
    workspace_path = str(WORKSPACES_DIR / user_id)

    # Ensure workspace header is valid latin-1 for HTTP specification
    abs_ws = str(WORKSPACES_DIR / user_id)
    try:
        abs_ws.encode("latin-1")
        workspace_header = abs_ws
    except UnicodeEncodeError:
        workspace_header = f"./backend/data/workspaces/{user_id}"

    # 6. Return 200 OK with Authoritative Identity Headers
    response = Response(status_code=status.HTTP_200_OK)
    response.headers["X-Forwarded-User"] = user_id
    response.headers["X-Forwarded-Role"] = role
    response.headers["X-User"] = user_id
    response.headers["X-User-Role"] = role
    response.headers["X-User-Workspace"] = workspace_header
    response.headers["X-Auth-Method"] = auth_method

    if quota_mgr.is_p1_elevated(user_id):
        response.headers["X-Priority"] = "P1-CRITICAL"
        meta = quota_mgr.get_p1_metadata(user_id)
        if meta and meta.get("incident_id"):
            response.headers["X-Incident-ID"] = meta["incident_id"]

    return response

@app.post("/api/v1/auth/p1/elevate")
async def p1_elevate(request: Request):
    """Dynamically grants 60-minute P1 elevation bound to an on-call sysadmin and incident ID."""
    user_id, _ = authenticate_request(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    incident_id = data.get("incident_id", "").strip()
    reason = data.get("reason", "On-call emergency")
    ttl = int(data.get("ttl_seconds", 3600))

    if not incident_id:
        raise HTTPException(status_code=400, detail="incident_id is mandatory for P1 elevation")

    try:
        from services.auth_gateway.p1_elevation import get_p1_gate
    except ImportError:
        from p1_elevation import get_p1_gate

    gate = get_p1_gate()
    try:
        token = gate.issue_p1_token(user_id, incident_id, ttl_seconds=ttl, reason=reason)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    return {
        "status": "elevated",
        "user_id": user_id,
        "incident_id": incident_id,
        "token": token,
        "priority": "P1-CRITICAL",
        "max_in_flight": 6,
        "rpm_limit": 200,
        "expires_in_seconds": ttl,
    }

@app.get("/api/v1/auth/p1/status")
async def p1_status(request: Request):
    """Returns current P1 elevation status for the caller."""
    user_id, _ = authenticate_request(request)
    try:
        from services.auth_gateway.p1_elevation import get_p1_gate
    except ImportError:
        from p1_elevation import get_p1_gate
    gate = get_p1_gate()
    return gate.get_p1_status(user_id)

@app.post("/api/v1/auth/p1/revoke")
async def p1_revoke(request: Request):
    """Revokes active P1 elevation for caller."""
    user_id, _ = authenticate_request(request)
    try:
        from services.auth_gateway.p1_elevation import get_p1_gate
    except ImportError:
        from p1_elevation import get_p1_gate
    gate = get_p1_gate()
    gate.revoke_p1_elevation(user_id)
    return {"status": "revoked", "user_id": user_id}

@app.post("/api/v1/auth/login")
@app.post("/auth/login")
@app.post("/login")
async def login(request: Request):
    """Secure login endpoint creating high-entropy session in Valkey."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if valid_login_password(username, password):
        session_id = secrets.token_urlsafe(32)
        r = get_valkey()
        if not r:
            raise HTTPException(status_code=503, detail="Session store unavailable")
        try:
            r.setex(f"session:{session_id}", 86400, username)  # 24h TTL
        except Exception as e:
            raise HTTPException(status_code=503, detail="Session store unavailable") from e

        resp = JSONResponse({
            "status": "authenticated",
            "user": username,
            "session_id": session_id,
            "role": "p1-operator" if username == "emergency-p1-oncall" else "sysadmin"
        })
        resp.set_cookie(
            key="session_id",
            value=session_id,
            httponly=True,
            samesite="lax",
            max_age=86400,
            secure=False
        )
        await _audit_async(
            user_id=username,
            session_id=session_id,
            tool_name="login_success",
            action="login_success",
            exit_code=0,
        )
        return resp
    await _audit_async(
        user_id="anonymous",
        session_id="",
        tool_name="login_failure",
        action="login_failure",
        exit_code=1,
        parameters={"attempted_user": username},
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid sysadmin credentials")

@app.post("/api/v1/auth/logout")
@app.post("/auth/logout")
@app.post("/logout")
async def logout(request: Request):
    session_cookie = request.cookies.get("session_id", "").strip()
    user_id = "anonymous"
    if session_cookie:
        r = get_valkey()
        if r:
            try:
                resolved = r.get(f"session:{session_cookie}")
                if resolved in VALID_USERS:
                    user_id = resolved
                r.delete(f"session:{session_cookie}")
            except Exception:
                pass
    await _audit_async(
        user_id=user_id,
        session_id="",
        tool_name="logout",
        action="logout",
        exit_code=0,
    )
    resp = JSONResponse({"status": "logged_out"})
    resp.delete_cookie("session_id")
    return resp

if __name__ == "__main__":
    configure("auth_gateway")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3081
    log_event(_LOG, "service_start", f"auth gateway listening on 127.0.0.1:{port}",
              fields={"port": port, "host": "127.0.0.1"})
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=False)  # requests are logged as JSON by the middleware

"""Local browser authentication and authorized case collaboration.

All functions share the caller's transaction. In particular a failed login must
commit its normal response so that lockout counters persist. API-key clients
continue to use the independent dependencies in ``api.py``.
"""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from opengrid.contracts import CaseUpdate
from opengrid.db import Anomaly, Asset, Audit, Case, now
from opengrid.product_models import BrowserSession, FollowUp, InvestigationNote, User
from opengrid.services import Conflict, update_case

COOKIE_NAME = "opengrid_session"
LOGIN_CSRF_COOKIE = "opengrid_login_csrf"
SESSION_SECONDS = 12 * 60 * 60
ROLES = ("ADMIN", "OPERATOR", "INVESTIGATOR", "VIEWER")
RESOLUTIONS = (
    "Technical loss",
    "Meter issue",
    "Communication issue",
    "Data quality issue",
    "Topology/mapping issue",
    "Expected operational condition",
    "Under investigation",
    "Other",
)
PERMISSIONS = {
    "view": frozenset(ROLES),
    "assign": frozenset({"ADMIN", "OPERATOR"}),
    "investigate": frozenset({"ADMIN", "OPERATOR", "INVESTIGATOR"}),
    "simulate": frozenset({"ADMIN"}),
    "manage_users": frozenset({"ADMIN"}),
}
SCRYPT_N, SCRYPT_R, SCRYPT_P = 32768, 8, 3


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("password must contain 12 to 1024 characters")
    salt = secrets.token_bytes(32)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or len(password) > 1024:
        return False
    try:
        name, n, r, p, salt, expected = encoded.split("$")
        # Never accept arbitrary work factors from a corrupted database record.
        if name != "scrypt" or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt_bytes, expected_bytes = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt_bytes) != 32 or len(expected_bytes) != 32:
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt_bytes,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            maxmem=64 * 1024 * 1024,
            dklen=32,
        )
        return hmac.compare_digest(derived, expected_bytes)
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return False


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    return hash_password(secrets.token_urlsafe(32))


def normalize_username(username: str) -> str:
    value = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", value):
        raise ValueError("username must be 3 to 64 letters, digits, dots, dashes or underscores")
    return value


def create_user(
    s: Session, username: str, password: str, role: str = "VIEWER", display_name: str | None = None
) -> User:
    username = normalize_username(username)
    if role not in ROLES:
        raise ValueError("unknown user role")
    display_name = (display_name or username).strip()
    if not 1 <= len(display_name) <= 128:
        raise ValueError("display name must contain 1 to 128 characters")
    if s.scalar(select(User.id).where(User.username == username)):
        raise Conflict("username already exists")
    user = User(
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        role=role,
    )
    s.add(user)
    s.flush()
    return user


def bootstrap_admin(
    s: Session, username: str | None, password: str | None, display_name: str = "Administrator"
) -> User | None:
    if not username and not password:
        return None
    if not username or not password:
        raise ValueError("supply both UI_ADMIN_USERNAME and UI_ADMIN_PASSWORD")
    username = normalize_username(username)
    existing = s.scalar(select(User).where(User.username == username))
    if existing:
        if existing.role != "ADMIN" or not existing.active:
            raise Conflict("bootstrap username belongs to an inactive or non-admin account")
        return existing
    return create_user(s, username, password, "ADMIN", display_name)


def authenticate(s: Session, username: str, password: str) -> User | None:
    try:
        username = normalize_username(username)
    except (ValueError, AttributeError):
        verify_password(password, _dummy_password_hash())
        return None
    user = s.scalar(select(User).where(User.username == username).with_for_update())
    moment = now()
    if not user or not user.active or (user.locked_until and user.locked_until > moment):
        verify_password(password, _dummy_password_hash())
        return None
    if user.locked_until:
        user.failed_login_count, user.locked_until = 0, None
    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= 5:
            user.locked_until = moment + timedelta(minutes=15)
        s.flush()
        return None
    user.failed_login_count, user.locked_until = 0, None
    s.flush()
    return user


def public_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "active": user.active,
    }


def require_permission(user: User | None, permission: str) -> User:
    if user is None:
        raise HTTPException(401, "sign in required")
    if not user.active or user.role not in PERMISSIONS.get(permission, frozenset()):
        raise HTTPException(403, "your role cannot perform this action")
    return user


def create_session(
    s: Session, user: User, ttl_seconds: int = SESSION_SECONDS
) -> tuple[str, BrowserSession]:
    require_permission(user, "view")
    if not 60 <= ttl_seconds <= 7 * 24 * 60 * 60:
        raise ValueError("session lifetime must be between one minute and seven days")
    token = secrets.token_urlsafe(32)
    browser_session = BrowserSession(
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        expires_at=now() + timedelta(seconds=ttl_seconds),
    )
    s.add(browser_session)
    s.flush()
    return token, browser_session


def session_user(s: Session, raw_token: str | None) -> tuple[User, BrowserSession] | None:
    if not raw_token or not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw_token):
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    row = s.execute(
        select(User, BrowserSession)
        .join(BrowserSession, BrowserSession.user_id == User.id)
        .where(
            BrowserSession.token_hash == token_hash,
            BrowserSession.expires_at > now(),
            User.active.is_(True),
        )
    ).first()
    return (row[0], row[1]) if row else None


def revoke_session(s: Session, raw_token: str | None) -> None:
    if raw_token and len(raw_token) <= 128:
        s.execute(
            delete(BrowserSession).where(
                BrowserSession.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
            )
        )


def set_session_cookie(
    response: Response, token: str, secure: bool = False, ttl_seconds: int = SESSION_SECONDS
) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=ttl_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        LOGIN_CSRF_COOKIE, path="/", httponly=True, secure=secure, samesite="strict"
    )


def clear_session_cookie(response: Response, secure: bool = False) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=secure, samesite="lax")


def issue_login_csrf(response: Response, secure: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        max_age=600,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    return token


def _verify_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin:
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if not hmac.compare_digest(origin.encode(), expected.encode()):
            raise HTTPException(403, "cross-origin form submission rejected")


def _verify_token(expected: str | None, submitted: str | None) -> None:
    if (
        not expected
        or not submitted
        or len(submitted) > 128
        or not hmac.compare_digest(expected.encode(), submitted.encode())
    ):
        raise HTTPException(403, "invalid or expired form token; reload the page")


def verify_login_csrf(request: Request, submitted_token: str | None) -> None:
    _verify_origin(request)
    _verify_token(request.cookies.get(LOGIN_CSRF_COOKIE), submitted_token)


def verify_csrf(
    request: Request, browser_session: BrowserSession, submitted_token: str | None = None
) -> None:
    _verify_origin(request)
    _verify_token(
        browser_session.csrf_token, submitted_token or request.headers.get("x-csrf-token")
    )


def safe_next(value: str | None) -> str:
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        return "/dashboard"
    parsed = urlsplit(value)
    return value if not parsed.scheme and not parsed.netloc else "/dashboard"


def _case(s: Session, case_id: int) -> Case:
    case = s.get(Case, case_id, with_for_update=True)
    if not case:
        raise LookupError("case not found")
    return case


def _validate_assignee(s: Session, username: str | None) -> str | None:
    if username:
        username = normalize_username(username)
        user = s.scalar(select(User).where(User.username == username, User.active.is_(True)))
        if not user or user.role == "VIEWER":
            raise ValueError("assignee must be an active operator, investigator or administrator")
    return username or None


def _audit(s: Session, user: User, case_id: int, action: str, old: dict, new: dict) -> Audit:
    audit = Audit(
        case_id=case_id,
        actor=f"user:{user.username}",
        body={"action": action, "user_id": user.id, "old": old, "new": new},
    )
    s.add(audit)
    return audit


def create_operator_case(s: Session, anomaly_id: int, user: User) -> Case:
    require_permission(user, "assign")
    anomaly = s.get(Anomaly, anomaly_id)
    if not anomaly:
        raise LookupError("anomaly not found")
    # Same asset lock/order as the anomaly worker preserves one active case per asset.
    s.get(Asset, anomaly.asset_id, with_for_update=True)
    s.refresh(anomaly)
    existing = s.scalar(select(Case).where(Case.anomaly_id == anomaly_id))
    if not existing:
        existing = s.scalar(
            select(Case)
            .where(
                Case.asset_id == anomaly.asset_id,
                Case.status.in_(["NEW", "ASSIGNED", "INVESTIGATING"]),
            )
            .limit(1)
        )
    if existing:
        return existing
    if anomaly.status != "OPEN":
        raise Conflict("only an open anomaly can create a new investigation")
    case = Case(
        case_no=f"CASE-{now().year}-{uuid4().hex[:12].upper()}",
        asset_id=anomaly.asset_id,
        anomaly_id=anomaly.id,
        priority=anomaly.score,
        evidence=dict(anomaly.evidence),
    )
    s.add(case)
    s.flush()
    _audit(s, user, case.id, "CREATED", {}, {"anomaly_id": anomaly.id, "status": case.status})
    return case


def update_operator_case(s: Session, case_id: int, update: CaseUpdate, user: User) -> Case:
    require_permission(user, "investigate")
    case = _case(s, case_id)
    old = {name: getattr(case, name) for name in ("status", "assigned_to", "resolution", "version")}
    if update.assigned_to != case.assigned_to:
        require_permission(user, "assign")
        update = update.model_copy(
            update={"assigned_to": _validate_assignee(s, update.assigned_to)}
        )
    if update.status in {"RESOLVED", "DISMISSED"} and update.resolution not in RESOLUTIONS:
        raise ValueError("choose a supported resolution category")
    previous = {id(item) for item in s.new}
    case = update_case(s, case_id, update, f"user:{user.username}")
    new = {name: getattr(case, name) for name in old}
    action = "STATUS_CHANGED" if old["status"] != new["status"] else "UPDATED"
    if new["status"] in {"RESOLVED", "DISMISSED"}:
        action = new["status"]
    elif old["status"] in {"RESOLVED", "DISMISSED"} and new["status"] == "NEW":
        action = "REOPENED"
    elif old["assigned_to"] != new["assigned_to"]:
        action = "ASSIGNED"
    # Enrich the single not-yet-persisted audit record created by the existing service.
    for item in s.new:
        if isinstance(item, Audit) and id(item) not in previous:
            item.body = {**item.body, "action": action, "user_id": user.id, "old": old, "new": new}
    s.flush()
    return case


def _content(body: str, limit: int) -> str:
    value = body.strip()
    if not 1 <= len(value) <= limit:
        raise ValueError(f"text must contain 1 to {limit} characters")
    return value


def add_note(s: Session, case_id: int, body: str, user: User) -> InvestigationNote:
    require_permission(user, "investigate")
    _case(s, case_id)
    note = InvestigationNote(case_id=case_id, author_id=user.id, body=_content(body, 10000))
    s.add(note)
    s.flush()
    _audit(s, user, case_id, "NOTE_ADDED", {}, {"note_id": note.id, "body": note.body})
    return note


def create_follow_up(
    s: Session,
    case_id: int,
    body: str,
    user: User,
    due_at: datetime | None = None,
    assigned_to: str | None = None,
) -> FollowUp:
    require_permission(user, "investigate")
    _case(s, case_id)
    if assigned_to and assigned_to != user.username:
        require_permission(user, "assign")
    assigned_to = _validate_assignee(s, assigned_to)
    if due_at:
        if due_at.tzinfo is None:
            raise ValueError("follow-up due time requires a UTC offset")
        due_at = due_at.astimezone(UTC).replace(tzinfo=None)
    follow_up = FollowUp(
        case_id=case_id,
        author_id=user.id,
        body=_content(body, 2000),
        due_at=due_at,
        assigned_to=assigned_to,
    )
    s.add(follow_up)
    s.flush()
    _audit(
        s,
        user,
        case_id,
        "FOLLOW_UP_CREATED",
        {},
        {
            "follow_up_id": follow_up.id,
            "body": follow_up.body,
            "assigned_to": assigned_to,
            "due_at": due_at.isoformat() + "Z" if due_at else None,
        },
    )
    return follow_up


def complete_follow_up(s: Session, follow_up_id: int, user: User) -> FollowUp:
    require_permission(user, "investigate")
    follow_up = s.get(FollowUp, follow_up_id, with_for_update=True)
    if not follow_up:
        raise LookupError("follow-up not found")
    if follow_up.status != "COMPLETED":
        follow_up.status, follow_up.completed_at = "COMPLETED", now()
        _audit(
            s,
            user,
            follow_up.case_id,
            "FOLLOW_UP_COMPLETED",
            {"status": "OPEN"},
            {
                "follow_up_id": follow_up.id,
                "status": "COMPLETED",
            },
        )
    return follow_up

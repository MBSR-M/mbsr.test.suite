"""Security boundaries and case workflow; persistence checks use real MySQL."""

from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select

from opengrid.contracts import CaseUpdate
from opengrid.db import Anomaly, Asset, Audit, Case, now, session_factory
from opengrid.product_models import BrowserSession, User
from opengrid.services import Conflict
from opengrid.ui_auth import (
    COOKIE_NAME,
    LOGIN_CSRF_COOKIE,
    add_note,
    authenticate,
    bootstrap_admin,
    clear_session_cookie,
    complete_follow_up,
    create_follow_up,
    create_operator_case,
    create_session,
    create_user,
    hash_password,
    issue_login_csrf,
    public_user,
    require_permission,
    revoke_session,
    safe_next,
    session_user,
    set_session_cookie,
    update_operator_case,
    verify_csrf,
    verify_login_csrf,
    verify_password,
)

PASSWORD = "Test-only password 123!"


def request(headers=None):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/login",
            "server": ("testserver", 443),
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        }
    )


def test_password_hashes_are_salted_bounded_and_tamper_resistant():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert not verify_password("incorrect password", first)
    assert not verify_password(PASSWORD, first.replace("32768", "1073741824"))
    assert not verify_password(PASSWORD, "broken")
    assert not verify_password("x" * 1025, first)
    with pytest.raises(ValueError, match="12 to 1024"):
        hash_password("too short")


@pytest.mark.parametrize(
    "role,permission,allowed",
    [
        ("ADMIN", "manage_users", True),
        ("ADMIN", "simulate", True),
        ("OPERATOR", "assign", True),
        ("OPERATOR", "investigate", True),
        ("OPERATOR", "manage_users", False),
        ("INVESTIGATOR", "investigate", True),
        ("INVESTIGATOR", "assign", False),
        ("VIEWER", "view", True),
        ("VIEWER", "investigate", False),
        ("VIEWER", "assign", False),
        ("ADMIN", "nonexistent_permission", False),
    ],
)
def test_role_permissions_fail_closed(role, permission, allowed):
    user = User(username="person", role=role, active=True)
    if allowed:
        assert require_permission(user, permission) is user
    else:
        with pytest.raises(HTTPException) as error:
            require_permission(user, permission)
        assert error.value.status_code == 403


def test_csrf_requires_token_and_rejects_cross_origin():
    browser_session = BrowserSession(csrf_token="valid-token")
    verify_csrf(request(), browser_session, "valid-token")
    verify_csrf(request({"Origin": "https://testserver"}), browser_session, "valid-token")
    verify_csrf(request({"X-CSRF-Token": "valid-token"}), browser_session)
    for req, token in [
        (request(), None),
        (request(), "wrong"),
        (request({"Origin": "https://attacker.example"}), "valid-token"),
        (request({"Origin": "null"}), "valid-token"),
    ]:
        with pytest.raises(HTTPException) as error:
            verify_csrf(req, browser_session, token)
        assert error.value.status_code == 403


def test_login_csrf_and_session_cookie_security():
    response = Response()
    token = issue_login_csrf(response, secure=True)
    cookie = SimpleCookie(response.headers["set-cookie"])[LOGIN_CSRF_COOKIE]
    assert cookie.value == token
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "strict"
    verify_login_csrf(request({"Cookie": f"{LOGIN_CSRF_COOKIE}={token}"}), token)
    with pytest.raises(HTTPException):
        verify_login_csrf(request(), token)
    response = Response()
    set_session_cookie(response, "opaque-token", secure=True)
    cookie = SimpleCookie(response.headers.getlist("set-cookie")[0])[COOKIE_NAME]
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "lax"
    assert cookie["path"] == "/" and cookie["max-age"] == "43200"
    response = Response()
    clear_session_cookie(response, secure=True)
    assert SimpleCookie(response.headers["set-cookie"])[COOKIE_NAME]["max-age"] == "0"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/transformers?page=2", "/transformers?page=2"),
        ("https://attacker.example", "/dashboard"),
        ("//attacker.example", "/dashboard"),
        ("/\\attacker.example", "/dashboard"),
        ("/\r\nLocation:evil", "/dashboard"),
        (None, "/dashboard"),
    ],
)
def test_login_redirect_is_local(value, expected):
    assert safe_next(value) == expected


@pytest.fixture
def mysql_session():
    factory = session_factory()
    with factory() as s:
        try:
            yield s
        finally:
            s.rollback()
    factory.kw["bind"].dispose()


def new_user(s, role="OPERATOR"):
    return create_user(s, "test-" + uuid4().hex[:20], PASSWORD, role, "Test operator")


@pytest.mark.integration
def test_mysql_login_sessions_are_hashed_expiring_revocable(mysql_session):
    s = mysql_session
    user = new_user(s)
    assert authenticate(s, user.username.upper(), PASSWORD) is user
    raw_token, browser_session = create_session(s, user)
    assert raw_token != browser_session.token_hash
    assert len(browser_session.token_hash) == 64
    assert session_user(s, raw_token)[0].id == user.id
    assert session_user(s, "not-a-token") is None
    assert "password_hash" not in public_user(user)
    assert "locked_until" not in public_user(user)
    browser_session.expires_at = now() - timedelta(seconds=1)
    s.flush()
    assert session_user(s, raw_token) is None
    raw_token, browser_session = create_session(s, user)
    revoke_session(s, raw_token)
    assert session_user(s, raw_token) is None
    assert s.get(BrowserSession, browser_session.token_hash) is None
    raw_token, _ = create_session(s, user)
    user.active = False
    s.flush()
    assert session_user(s, raw_token) is None


@pytest.mark.integration
def test_mysql_login_lockout_persists_and_bootstrap_never_overwrites(mysql_session):
    s = mysql_session
    user = new_user(s, "ADMIN")
    encoded = user.password_hash
    assert bootstrap_admin(s, user.username, "Different password 123!") is user
    assert user.password_hash == encoded
    for _ in range(5):
        assert authenticate(s, user.username, "wrong password") is None
    s.expire(user)
    assert user.failed_login_count == 5 and user.locked_until > now()
    assert authenticate(s, user.username, PASSWORD) is None
    user.locked_until = now() - timedelta(seconds=1)
    s.flush()
    assert authenticate(s, user.username, PASSWORD) is user
    assert user.failed_login_count == 0 and user.locked_until is None
    with pytest.raises(Conflict):
        create_user(s, user.username, PASSWORD)
    assert bootstrap_admin(s, None, None) is None
    with pytest.raises(ValueError):
        bootstrap_admin(s, "only-username", None)


@pytest.mark.integration
def test_mysql_investigation_workflow_preserves_versioning_and_complete_audit(mysql_session):
    s = mysql_session
    operator, investigator, viewer = new_user(s), new_user(s, "INVESTIGATOR"), new_user(s, "VIEWER")
    asset = Asset(code="UI-" + uuid4().hex[:16], kind="TRANSFORMER")
    s.add(asset)
    s.flush()
    anomaly = Anomaly(
        asset_id=asset.id,
        start=datetime(2026, 1, 1),
        anomaly_type="ENERGY_IMBALANCE",
        status="OPEN",
        severity="HIGH",
        score=70,
        evidence={"current_percent": "18.7"},
    )
    s.add(anomaly)
    s.flush()
    case = create_operator_case(s, anomaly.id, operator)
    assert create_operator_case(s, anomaly.id, operator).id == case.id
    assert s.scalar(select(func.count()).select_from(Case).where(Case.asset_id == asset.id)) == 1
    with pytest.raises(HTTPException):
        add_note(s, case.id, "viewer cannot write", viewer)
    with pytest.raises(HTTPException):
        update_operator_case(
            s,
            case.id,
            CaseUpdate(version=1, status="ASSIGNED", assigned_to=investigator.username),
            investigator,
        )
    case = update_operator_case(
        s,
        case.id,
        CaseUpdate(version=1, status="ASSIGNED", assigned_to=investigator.username),
        operator,
    )
    with pytest.raises(Conflict):
        update_operator_case(
            s,
            case.id,
            CaseUpdate(version=1, status="INVESTIGATING", assigned_to=investigator.username),
            investigator,
        )
    case = update_operator_case(
        s,
        case.id,
        CaseUpdate(version=2, status="INVESTIGATING", assigned_to=investigator.username),
        investigator,
    )
    note = add_note(s, case.id, "Checked communication logs. Field check required.", investigator)
    assert note.author_id == investigator.id
    follow_up = create_follow_up(
        s,
        case.id,
        "Inspect CT wiring",
        investigator,
        due_at=datetime.now(UTC) + timedelta(days=1),
        assigned_to=investigator.username,
    )
    complete_follow_up(s, follow_up.id, investigator)
    complete_follow_up(s, follow_up.id, investigator)
    assert follow_up.completed_at is not None
    with pytest.raises(ValueError, match="resolution category"):
        update_operator_case(
            s,
            case.id,
            CaseUpdate(
                version=3,
                status="RESOLVED",
                assigned_to=investigator.username,
                resolution="Unsupported",
            ),
            investigator,
        )
    case = update_operator_case(
        s,
        case.id,
        CaseUpdate(
            version=3,
            status="RESOLVED",
            assigned_to=investigator.username,
            resolution="Meter issue",
        ),
        investigator,
    )
    assert case.closed_at is not None and case.version == 4
    case = update_operator_case(
        s, case.id, CaseUpdate(version=4, status="NEW", assigned_to=investigator.username), operator
    )
    assert case.closed_at is None and case.version == 5
    activity = list(s.scalars(select(Audit).where(Audit.case_id == case.id).order_by(Audit.id)))
    assert [entry.body["action"] for entry in activity] == [
        "CREATED",
        "ASSIGNED",
        "STATUS_CHANGED",
        "NOTE_ADDED",
        "FOLLOW_UP_CREATED",
        "FOLLOW_UP_COMPLETED",
        "RESOLVED",
        "REOPENED",
    ]
    assigned = activity[1]
    assert assigned.body["old"]["assigned_to"] is None
    assert assigned.body["new"]["assigned_to"] == investigator.username
    assert assigned.body["from"] == "NEW" and assigned.body["status"] == "ASSIGNED"
    assert all(entry.actor.startswith("user:") and entry.body["user_id"] for entry in activity)

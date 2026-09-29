"""Server-rendered operator application; legacy API-key routes remain independent."""
import csv
import hmac
import inspect
import io
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from opengrid import ui_queries as queries
from opengrid.config import settings
from opengrid.contracts import CaseUpdate
from opengrid.contracts.common import SuccessResponse
from opengrid.contracts.dashboard import DashboardContract, DashboardFilterContract
from opengrid.contracts.errors import ErrorCode, ErrorDetailContract, ErrorResponse
from opengrid.contracts.feeder import (
    FeederContract,
    FeederSummaryContract,
    FeederTopologyContract,
    FeederTrendContract,
)
from opengrid.db import Feeder, Job
from opengrid.product_models import FollowUp, InvestigationNote, User
from opengrid.read_contracts import (
    canonical_dashboard,
    canonical_feeder,
    feeder_summary,
    feeder_topology,
    feeder_trends,
)
from opengrid.ui_auth import (
    COOKIE_NAME,
    RESOLUTIONS,
    ROLES,
    add_note,
    authenticate,
    clear_session_cookie,
    complete_follow_up,
    create_follow_up,
    create_operator_case,
    create_session,
    create_user,
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
)

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
router = APIRouter()
CANONICAL_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


def fmt(value, digits=1, suffix=""):
    if value is None or value == "":
        return "N/A"
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (ValueError, TypeError):
        return str(value)


def timestamp(value):
    if not value:
        return "Not available"
    if isinstance(value, str):
        return value.replace("T", " ").replace("+00:00", "").removesuffix("Z")[:19] + " UTC"
    return value.strftime("%d %b %Y, %H:%M UTC")


templates.env.filters.update(num=fmt, timestamp=timestamp)


def database(request: Request):
    with request.app.state.factory.begin() as s:
        yield s


def identity(request: Request, s=Depends(database, scope="function")):
    auth = session_user(s, request.cookies.get(COOKIE_NAME))
    if not auth:
        raise HTTPException(401, "Sign in to continue.")
    request.state.user, request.state.browser_session = auth
    # Keep the template identity detached from the request transaction.  This
    # also lets an error page render after dependency cleanup.
    request.state.user_context = public_user(auth[0])
    request.state.csrf_token = auth[1].csrf_token
    return auth[0]


def api_identity(request: Request, s=Depends(database, scope="function")):
    key = request.headers.get("x-api-key", "")
    if key and any(hmac.compare_digest(key, candidate) for candidate in (settings().api_key, settings().read_api_key)):
        return SimpleNamespace(role="VIEWER", username="api-client", active=True)
    return identity(request, s)


def context(request, **extra):
    user = getattr(request.state, "user", None)
    browser = getattr(request.state, "browser_session", None)
    template_user = getattr(request.state, "user_context", None)
    if template_user is None and user:
        template_user = public_user(user)
    csrf_token = getattr(request.state, "csrf_token", None)
    if csrf_token is None and browser:
        csrf_token = browser.csrf_token
    def url(**changes):
        params = dict(request.query_params)
        params.update(changes)
        return request.url.path + "?" + urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    return {"request": request, "user": template_user,
            "csrf_token": csrf_token or "", "grafana_url": settings().grafana_url,
            "app_version": "0.2.0", "active_page": request.url.path.strip("/").split("/")[0],
            "query": request.query_params, "url": url, "request_id": getattr(request.state, "request_id", ""), **extra}


def render(request, name, status=200, **extra):
    return templates.TemplateResponse(request=request, name=name, context=context(request, **extra), status_code=status)


def call_query(function, s, request, **overrides):
    allowed = inspect.signature(function).parameters
    params = {k: v for k, v in request.query_params.items() if k in allowed and v != ""}
    for name in ("page", "page_size", "transformer_id", "asset_id", "case_id"):
        if name in params:
            params[name] = int(params[name])
    for name in ("min_imbalance", "max_imbalance", "min_completeness", "max_completeness", "min_score"):
        if name in params:
            params[name] = float(params[name])
    params.update(overrides)
    return function(s, **params)


def chart(trend, meter=False):
    points = jsonable_encoder(trend.get("points", []))
    series = [("input_kwh", "Transformer input", "#168b89"), ("downstream_kwh", "Downstream energy", "#5976cf"),
              ("unaccounted_kwh", "Accounting difference", "#dc9232")]
    if meter:
        series = [("import_kwh", "Interval consumption", "#168b89"), ("baseline_7d", "7-day baseline", "#5976cf"), ("baseline_28d", "28-day baseline", "#dc9232")]
    return {"type": "line", "data": {"labels": [p.get("time", p.get("timestamp")) for p in points],
            "datasets": [{"label": label, "data": [p.get(key) for p in points], "borderColor": color,
                          "backgroundColor": color + "18", "pointRadius": 0, "borderWidth": 2,
                          "spanGaps": False, "tension": 0.2} for key, label, color in series]},
            "options": {"responsive": True, "maintainAspectRatio": False, "interaction": {"mode": "index", "intersect": False}},
            "details": points}


def canonical_chart(trend):
    """Adapt an authoritative contract to the presentation-only chart shape."""
    points = [
        {
            "time": point.timestamp,
            "input_kwh": point.input_energy_kwh,
            "downstream_kwh": point.downstream_energy_kwh,
            "unaccounted_kwh": point.accounting_difference_kwh,
        }
        for point in trend.points
    ]
    return chart({"points": points})


def v2_filters(
    from_time: datetime,
    to_time: datetime,
    feeder_ids: list[int],
    transformer_ids: list[int],
    severity: list[str],
    granularity: str,
):
    return DashboardFilterContract(
        from_time=from_time,
        to_time=to_time,
        feeder_ids=feeder_ids,
        transformer_ids=transformer_ids,
        severity=severity,
        granularity=granularity,
    )


def canonical_api_error(request: Request, status: int, code: ErrorCode, message: str, details=None):
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    body = ErrorResponse(
        error=ErrorDetailContract(
            code=code,
            message=message[:500],
            details=details or {},
            request_id=UUID(request_id),
        )
    )
    return JSONResponse(status_code=status, content=jsonable_encoder(body))


TABLES = {
    "transformers": (queries.list_transformers, [("code", "Transformer"), ("feeder_code", "Feeder"), ("meter_count", "Meters"), ("imbalance_percent", "Imbalance %"), ("baseline_percent", "Baseline %"), ("deviation_pp", "Deviation pp"), ("unaccounted_kwh", "Difference kWh"), ("completeness_percent", "Completeness %"), ("severity", "Severity"), ("investigation_status", "Investigation")]),
    "meters": (queries.list_meters, [("code", "Meter"), ("transformer_code", "Transformer"), ("current_kwh", "Consumption kWh"), ("baseline_28d", "Baseline kWh"), ("deviation_percent", "Deviation %"), ("last_reading", "Last reading"), ("anomaly_type", "Anomaly"), ("status", "Quality")]),
    "investigations": (queries.list_investigations, [("case_no", "Case"), ("asset_code", "Entity"), ("priority", "Priority"), ("severity", "Severity"), ("unaccounted_kwh", "Difference kWh"), ("confidence", "Confidence"), ("assigned_to", "Assigned to"), ("status", "Status"), ("opened_at", "Opened")]),
    "anomalies": (queries.list_anomalies, [("id", "Anomaly"), ("asset_code", "Entity"), ("anomaly_type", "Type"), ("score", "Score"), ("severity", "Severity"), ("status", "Status"), ("start", "Observed")]),
    "data-quality": (queries.list_quality, [("asset_code", "Entity"), ("start", "Interval"), ("status", "Quality"), ("import_kwh", "Import kWh"), ("flags", "Flags")]),
    "events": (queries.list_events, [("time", "Time"), ("entity_code", "Entity"), ("event_type", "Event"), ("source", "Source"), ("severity", "Severity"), ("details", "Details")]),
}


@router.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/dashboard"):
    response = Response()
    token = issue_login_csrf(response, settings().ui_secure_cookies)
    rendered = render(request, "login.html", csrf_token=token, next=safe_next(next))
    rendered.headers.append("set-cookie", response.headers["set-cookie"])
    return rendered


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(max_length=100), password: str = Form(max_length=1024),
          csrf_token: str = Form(), next: str = Form("/dashboard"), s=Depends(database, scope="function")):
    verify_login_csrf(request, csrf_token)
    user = authenticate(s, username, password)
    if not user:
        return render(request, "login.html", status=401, csrf_token=csrf_token, username=username,
                      next=safe_next(next), error="Unable to sign in. Check your credentials or try again later.")
    revoke_session(s, request.cookies.get(COOKIE_NAME))
    token, _ = create_session(s, user)
    response = RedirectResponse(safe_next(next), status_code=303)
    set_session_cookie(response, token, settings().ui_secure_cookies)
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(), user=Depends(identity), s=Depends(database, scope="function")):
    verify_csrf(request, request.state.browser_session, csrf_token)
    revoke_session(s, request.cookies.get(COOKIE_NAME))
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, settings().ui_secure_cookies)
    return response


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(identity), s=Depends(database, scope="function")):
    summary = call_query(queries.dashboard_summary, s, request)
    trend = call_query(queries.dashboard_trends, s, request)
    candidates = queries.list_transformers(s, page_size=8, sort="score", order="desc")["items"]
    return render(request, "dashboard.html", page_title="Network overview", summary=summary,
                  trend=chart(trend), candidates=candidates,
                  meter_findings=queries.list_anomalies(s, page_size=5, asset_kind="METER")["items"],
                  cases=queries.list_investigations(s, page_size=5)["items"],
                  quality=call_query(queries.quality_summary, s, request))


def list_page(request: Request, user, s, kind):
    function, columns = TABLES[kind]
    data = call_query(function, s, request)
    title = {"data-quality": "Data quality", "events": "Event explorer"}.get(kind, kind.title())
    return render(request, "list.html", page_title=title, kind=kind, data=data, columns=columns,
                  quality=call_query(queries.quality_summary, s, request) if kind == "data-quality" else None)


for _kind in TABLES:
    def make_list(kind):
        def endpoint(request: Request, user=Depends(identity), s=Depends(database, scope="function")):
            return list_page(request, user, s, kind)
        return endpoint
    router.add_api_route("/" + _kind, make_list(_kind), methods=["GET"], response_class=HTMLResponse, name=f"ui_{_kind}")


@router.get("/feeders", response_class=HTMLResponse)
def feeders(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    q: str = Query("", max_length=128),
    user=Depends(identity),
    s=Depends(database, scope="function"),
):
    query = select(Feeder).order_by(Feeder.code, Feeder.id)
    if q:
        query = query.where(
            or_(Feeder.code.contains(q, autoescape=True), Feeder.name.contains(q, autoescape=True))
        )
    total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = s.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    # Feeder summary is an authoritative read model. The browser only renders
    # those values and does not reconstruct accounting totals.
    items = [
        {"feeder": canonical_feeder(s, row.id), "summary": feeder_summary(s, row.id)}
        for row in rows
    ]
    data = {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": (total + page_size - 1) // page_size,
    }
    return render(request, "feeders.html", page_title="Feeders", data=data)


@router.get("/feeders/{feeder_id}", response_class=HTMLResponse)
def feeder(
    request: Request,
    feeder_id: int,
    user=Depends(identity),
    s=Depends(database, scope="function"),
):
    item = canonical_feeder(s, feeder_id)
    if not item:
        raise HTTPException(404, "Feeder not found.")
    return render(
        request,
        "feeder.html",
        page_title=item.feeder_code,
        item=item,
        summary=feeder_summary(s, feeder_id),
        topology=feeder_topology(s, feeder_id),
        chart_data=canonical_chart(feeder_trends(s, feeder_id)),
    )


@router.get("/transformers/{asset_id}", response_class=HTMLResponse)
def transformer(request: Request, asset_id: int, user=Depends(identity), s=Depends(database, scope="function")):
    item = call_query(queries.transformer_detail, s, request, asset_id=asset_id)
    if not item:
        raise HTTPException(404, "Transformer not found.")
    return render(request, "entity.html", page_title=item["code"], item=item, kind="transformers",
                  chart_data=chart(item["trend"]), columns=TABLES["meters"][1])


@router.get("/meters/{asset_id}", response_class=HTMLResponse)
def meter(request: Request, asset_id: int, user=Depends(identity), s=Depends(database, scope="function")):
    item = call_query(queries.meter_detail, s, request, asset_id=asset_id)
    if not item:
        raise HTTPException(404, "Meter not found.")
    return render(request, "entity.html", page_title=item["code"], item=item, kind="meters",
                  chart_data=chart(item["trend"], meter=True), columns=TABLES["meters"][1])


@router.get("/investigations/{case_id}", response_class=HTMLResponse)
def investigation(request: Request, case_id: int, user=Depends(identity), s=Depends(database, scope="function")):
    item = call_query(queries.investigation_detail, s, request, case_id=case_id)
    if not item:
        raise HTTPException(404, "Investigation not found.")
    notes = [{"body": n.body, "created_at": n.created_at, "author": author.display_name}
             for n, author in s.execute(select(InvestigationNote, User).join(User, User.id == InvestigationNote.author_id)
                 .where(InvestigationNote.case_id == case_id).order_by(InvestigationNote.id.desc()).limit(100))]
    followups = s.scalars(select(FollowUp).where(FollowUp.case_id == case_id).order_by(FollowUp.id.desc()).limit(100)).all()
    users = s.scalars(select(User).where(User.active.is_(True), User.role != "VIEWER").order_by(User.username).limit(100)).all()
    return render(request, "investigation.html", page_title=item["case_no"], item=item, notes=notes,
                  followups=followups, users=users, resolutions=RESOLUTIONS,
                  chart_data=chart(item.get("trend", {"points": []}), meter=item.get("asset_kind") == "METER"))


@router.post("/investigations/create")
def create_case(request: Request, anomaly_id: int = Form(), csrf_token: str = Form(),
                user=Depends(identity), s=Depends(database, scope="function")):
    verify_csrf(request, request.state.browser_session, csrf_token)
    case = create_operator_case(s, anomaly_id, user)
    return RedirectResponse(f"/investigations/{case.id}", status_code=303)


@router.post("/investigations/{case_id}/update")
def change_case(request: Request, case_id: int, version: int = Form(), status: str = Form(),
                assigned_to: str = Form(""), resolution: str = Form(""), csrf_token: str = Form(),
                user=Depends(identity), s=Depends(database, scope="function")):
    verify_csrf(request, request.state.browser_session, csrf_token)
    update_operator_case(s, case_id, CaseUpdate(version=version, status=status,
        assigned_to=assigned_to or None, resolution=resolution or None), user)
    return RedirectResponse(f"/investigations/{case_id}?saved=1", status_code=303)


@router.post("/investigations/{case_id}/notes")
def note_create(request: Request, case_id: int, body: str = Form(max_length=10000), csrf_token: str = Form(),
                user=Depends(identity), s=Depends(database, scope="function")):
    verify_csrf(request, request.state.browser_session, csrf_token)
    add_note(s, case_id, body, user)
    return RedirectResponse(f"/investigations/{case_id}#notes", status_code=303)


@router.post("/investigations/{case_id}/follow-ups")
def followup_create(request: Request, case_id: int, body: str = Form(max_length=2000), due_at: str = Form(""),
                    assigned_to: str = Form(""), csrf_token: str = Form(), user=Depends(identity), s=Depends(database, scope="function")):
    verify_csrf(request, request.state.browser_session, csrf_token)
    due = datetime.fromisoformat(due_at).replace(tzinfo=UTC) if due_at else None
    create_follow_up(s, case_id, body, user, due, assigned_to or None)
    return RedirectResponse(f"/investigations/{case_id}#notes", status_code=303)


@router.post("/follow-ups/{followup_id}/complete")
def followup_complete(request: Request, followup_id: int, csrf_token: str = Form(),
                      user=Depends(identity), s=Depends(database, scope="function")):
    verify_csrf(request, request.state.browser_session, csrf_token)
    item = complete_follow_up(s, followup_id, user)
    return RedirectResponse(f"/investigations/{item.case_id}#notes", status_code=303)


@router.get("/ui/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", user=Depends(identity), s=Depends(database, scope="function")):
    return render(request, "partials/search.html", results=queries.global_search(s, q))


@router.get("/ui/alerts", response_class=HTMLResponse)
def alerts(request: Request, user=Depends(identity), s=Depends(database, scope="function")):
    return render(request, "partials/alerts.html", alerts=queries.alert_summary(s))


@router.get("/ui/activity", response_class=HTMLResponse)
def activity(request: Request, user=Depends(identity), s=Depends(database, scope="function")):
    return render(request, "partials/timeline.html", timeline=queries.list_events(s, page_size=8, period="7d"))


@router.get("/network", response_class=HTMLResponse)
def network(request: Request, user=Depends(identity), s=Depends(database, scope="function")):
    return render(request, "network.html", page_title="Network topology",
                  topology=call_query(queries.network_topology, s, request),
                  heatmap=call_query(queries.network_heatmap, s, request))


@router.get("/network/map", response_class=HTMLResponse)
def map_page(request: Request, user=Depends(identity)):
    return render(request, "message.html", page_title="Network map", message="Geographical coordinates are not present in the current asset model. Use the feeder-to-meter topology view to explore this network.", link="/network", link_label="Open network topology")


@router.get("/system", response_class=HTMLResponse)
def system(request: Request, user=Depends(identity), s=Depends(database, scope="function")):
    from opengrid.ui_operations import system_status
    return render(request, "system.html", page_title="System health", system=system_status(s))


@router.get("/settings", response_class=HTMLResponse)
def configuration(request: Request, user=Depends(identity), s=Depends(database, scope="function")):
    users = [public_user(u) for u in s.scalars(select(User).order_by(User.username).limit(100))] if user.role == "ADMIN" else []
    return render(request, "settings.html", page_title="Settings", values=queries.safe_settings(), users=users, roles=ROLES)


@router.post("/settings/users")
def user_create(request: Request, username: str = Form(), display_name: str = Form(), role: str = Form(),
                password: str = Form(max_length=1024), csrf_token: str = Form(), user=Depends(identity), s=Depends(database, scope="function")):
    verify_csrf(request, request.state.browser_session, csrf_token)
    require_permission(user, "manage_users")
    create_user(s, username, password, role, display_name)
    return RedirectResponse("/settings?created=1", status_code=303)


@router.get("/simulator", response_class=HTMLResponse)
def simulator_page(request: Request, user=Depends(identity), s=Depends(database, scope="function")):
    from opengrid.ui_operations import simulation_status
    return render(request, "simulator.html", page_title="Scenario simulator", jobs=simulation_status(s), demo_enabled=settings().ui_demo_enabled)


@router.post("/simulator/start")
def simulation_start(request: Request, scenario: str = Form("transformer-imbalance"), transformers: int = Form(4),
                     meters: int = Form(5), days: int = Form(29), action: str = Form("start"), csrf_token: str = Form(),
                     user=Depends(identity), s=Depends(database, scope="function")):
    from opengrid.ui_operations import start_simulation
    verify_csrf(request, request.state.browser_session, csrf_token)
    require_permission(user, "simulate")
    start_simulation(s, user.username, scenario, transformers, meters, days, action=action)
    return RedirectResponse("/simulator", status_code=303)


@router.post("/simulator/{job_id}/stop")
def simulation_stop(request: Request, job_id: str, csrf_token: str = Form(), user=Depends(identity), s=Depends(database, scope="function")):
    verify_csrf(request, request.state.browser_session, csrf_token)
    require_permission(user, "simulate")
    job = s.get(Job, job_id, with_for_update=True)
    if not job or job.payload.get("kind") != "SIMULATION":
        raise HTTPException(404, "Simulation not found.")
    if job.status in {"SIMULATION_QUEUED", "SIMULATION_RUNNING"}:
        job.status = "SIMULATION_STOPPED"
    return RedirectResponse("/simulator", status_code=303)


def csv_safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


@router.get("/exports/{kind}.csv")
def export(request: Request, kind: str, user=Depends(identity), s=Depends(database, scope="function")):
    if kind not in TABLES:
        raise HTTPException(404)
    function, columns = TABLES[kind]
    # Bounded export shares active filters; paging explicit, no full raw-observation download.
    data = call_query(function, s, request, page=1, page_size=100)
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow([label for _, label in columns])
    writer.writerows([[csv_safe(row.get(key)) for key, _ in columns] for row in data["items"]])
    return Response(stream.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="opengrid-{kind}.csv"', "X-Export-Limit": "100"})


@router.get("/api/v1/dashboard/summary")
def summary_api(request: Request, user=Depends(api_identity), s=Depends(database, scope="function")):
    return call_query(queries.dashboard_summary, s, request)


@router.get("/api/v1/dashboard/trends")
def trends_api(request: Request, user=Depends(api_identity), s=Depends(database, scope="function")):
    return call_query(queries.dashboard_trends, s, request)


@router.get("/api/v1/search")
def search_api(q: str, user=Depends(api_identity), s=Depends(database, scope="function")):
    return queries.global_search(s, q)


@router.get("/api/v1/network/topology")
def topology_api(request: Request, user=Depends(api_identity), s=Depends(database, scope="function")):
    return call_query(queries.network_topology, s, request)


@router.get("/api/v1/system/status")
def system_api(user=Depends(api_identity), s=Depends(database, scope="function")):
    from opengrid.ui_operations import system_status
    return system_status(s)


@router.get("/api/v1/ui/{kind}")
def list_api(request: Request, kind: str, user=Depends(api_identity), s=Depends(database, scope="function")):
    if kind not in TABLES:
        raise HTTPException(404)
    result = call_query(TABLES[kind][0], s, request)
    return {**result, "request_id": request.state.request_id, "schema_version": 1}


@router.get("/api/v1/transformers/{asset_id}/timeline")
def transformer_timeline(request: Request, asset_id: int, user=Depends(api_identity), s=Depends(database, scope="function")):
    return call_query(queries.entity_timeline, s, request, asset_id=asset_id, include_meters=True)


@router.get("/api/v1/meters/{asset_id}/timeline")
def meter_timeline(request: Request, asset_id: int, user=Depends(api_identity), s=Depends(database, scope="function")):
    return call_query(queries.entity_timeline, s, request, asset_id=asset_id)


@router.get("/api/v1/investigations/{case_id}/timeline")
def case_timeline(request: Request, case_id: int, user=Depends(api_identity), s=Depends(database, scope="function")):
    return call_query(queries.investigation_timeline, s, request, case_id=case_id)


# Version 2 exposes canonical Pydantic read contracts. Version 1 retains its
# original compact payloads for installed API-key clients.
@router.get("/api/v2/dashboard", response_model=SuccessResponse[DashboardContract], responses=CANONICAL_ERROR_RESPONSES)
def canonical_dashboard_api(
    request: Request,
    from_time: datetime,
    to_time: datetime,
    feeder_ids: list[int] = Query(default=[]),
    transformer_ids: list[int] = Query(default=[]),
    severity: list[str] = Query(default=[]),
    granularity: str = Query(default="1h", pattern="^(15m|1h|1d)$"),
    user=Depends(api_identity),
    s=Depends(database, scope="function"),
):
    filters = v2_filters(from_time, to_time, feeder_ids, transformer_ids, severity, granularity)
    return SuccessResponse(data=canonical_dashboard(s, filters), request_id=UUID(request.state.request_id))


@router.get("/api/v2/feeders/{feeder_id}", response_model=SuccessResponse[FeederContract], responses=CANONICAL_ERROR_RESPONSES)
def canonical_feeder_api(
    request: Request,
    feeder_id: int,
    user=Depends(api_identity),
    s=Depends(database, scope="function"),
):
    item = canonical_feeder(s, feeder_id)
    if not item:
        raise HTTPException(404, "feeder not found")
    return SuccessResponse(data=item, request_id=UUID(request.state.request_id))


@router.get("/api/v2/feeders/{feeder_id}/summary", response_model=SuccessResponse[FeederSummaryContract], responses=CANONICAL_ERROR_RESPONSES)
def canonical_feeder_summary_api(
    request: Request,
    feeder_id: int,
    from_time: datetime,
    to_time: datetime,
    granularity: str = Query(default="1h", pattern="^(15m|1h|1d)$"),
    user=Depends(api_identity),
    s=Depends(database, scope="function"),
):
    item = feeder_summary(s, feeder_id, v2_filters(from_time, to_time, [], [], [], granularity))
    if not item:
        raise HTTPException(404, "feeder not found")
    return SuccessResponse(data=item, request_id=UUID(request.state.request_id))


@router.get("/api/v2/feeders/{feeder_id}/trends", response_model=SuccessResponse[FeederTrendContract], responses=CANONICAL_ERROR_RESPONSES)
def canonical_feeder_trends_api(
    request: Request,
    feeder_id: int,
    from_time: datetime,
    to_time: datetime,
    granularity: str = Query(default="1h", pattern="^(15m|1h|1d)$"),
    user=Depends(api_identity),
    s=Depends(database, scope="function"),
):
    item = feeder_trends(s, feeder_id, v2_filters(from_time, to_time, [], [], [], granularity))
    if not item:
        raise HTTPException(404, "feeder not found")
    return SuccessResponse(data=item, request_id=UUID(request.state.request_id))


@router.get("/api/v2/feeders/{feeder_id}/topology", response_model=SuccessResponse[FeederTopologyContract], responses=CANONICAL_ERROR_RESPONSES)
def canonical_feeder_topology_api(
    request: Request,
    feeder_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user=Depends(api_identity),
    s=Depends(database, scope="function"),
):
    item = feeder_topology(s, feeder_id, page=page, page_size=page_size)
    if not item:
        raise HTTPException(404, "feeder not found")
    return SuccessResponse(data=item, request_id=UUID(request.state.request_id))


def install_ui(app):
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    app.include_router(router)

    @app.middleware("http")
    async def headers(request, call_next):
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception:
            logging.exception("request failed request_id=%s", request.state.request_id)
            if request.url.path.startswith("/api/v2/"):
                return canonical_api_error(
                    request, 500, ErrorCode.INTERNAL_ERROR, "Unable to complete the request."
                )
            if request.url.path.startswith("/api/"):
                return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "Unable to complete the request.", "request_id": request.state.request_id}})
            response = render(request, "error.html", status=500, status_code=500, error_title="Unable to complete this request", error_message="Please try again. Contact your administrator if the issue continues.")
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        if request.url.path.startswith("/api/v2/"):
            code = {
                401: ErrorCode.AUTHENTICATION_REQUIRED,
                403: ErrorCode.AUTHORIZATION_FAILED,
                404: ErrorCode.NOT_FOUND,
            }.get(error.status_code, ErrorCode.INVALID_REQUEST)
            return canonical_api_error(request, error.status_code, code, str(error.detail))
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
        if error.status_code == 401 and request.method == "GET":
            return RedirectResponse("/login?" + urlencode({"next": request.url.path}), status_code=303)
        return render(request, "error.html", status=error.status_code, status_code=error.status_code,
                      error_title={403: "Access restricted", 404: "Entity not found", 409: "This record has changed", 503: "Service temporarily unavailable"}.get(error.status_code, "Unable to complete this action"), error_message=str(error.detail))

    @app.exception_handler(LookupError)
    async def lookup_error(request, error):
        return await http_error(request, HTTPException(404, str(error)))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        logging.exception("database request failed request_id=%s", request.state.request_id)
        if request.url.path.startswith("/api/v2/"):
            return canonical_api_error(
                request, 503, ErrorCode.DATABASE_ERROR, "Database service is temporarily unavailable."
            )
        return await http_error(request, HTTPException(503, "Database service is temporarily unavailable."))

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request, error):
        if request.url.path.startswith("/api/v2/"):
            details = {
                "issues": [
                    {"location": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
                    for item in error.errors()[:20]
                ]
            }
            return canonical_api_error(
                request, 422, ErrorCode.VALIDATION_ERROR, "Request validation failed.", details
            )
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(error.errors())})


def ui_error(request, message, status):
    return render(request, "error.html", status=status, status_code=status,
                  error_title="Unable to complete this action", error_message=message)

import hmac
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from opengrid.config import settings
from opengrid.contracts import (
    AssetInput,
    AssignmentInput,
    CaseUpdate,
    EventInput,
    ReadingInput,
    ReprocessInput,
)
from opengrid.db import (
    Anomaly,
    Asset,
    Assignment,
    Audit,
    Balance,
    Case,
    Configuration,
    Evaluation,
    Feeder,
    Heartbeat,
    Interval,
    Job,
    MeterEvent,
    Outbox,
    Quarantine,
    Reading,
    session_factory,
)
from opengrid.messaging import relay_loop
from opengrid.services import Conflict, accept_reading, emit, update_case
from opengrid.worker import configure_logging
from opengrid.ui import install_ui, ui_error


@asynccontextmanager
async def lifespan(app):
    configure_logging()
    app.state.factory = session_factory()
    stop = threading.Event()
    owner = "ingestion" if settings().service == "ingestion" else "api"
    thread = threading.Thread(target=relay_loop, args=(app.state.factory, owner, stop), daemon=True)
    thread.start()
    from opengrid.ui_operations import simulation_loop
    simulation_thread = threading.Thread(target=simulation_loop, args=(app.state.factory, stop), daemon=True)
    if settings().service == "api":
        simulation_thread.start()
    yield
    stop.set()
    thread.join(timeout=10)
    if simulation_thread.is_alive():
        simulation_thread.join(timeout=10)
    app.state.factory.kw["bind"].dispose()


app = FastAPI(title="OpenGrid Loss", version="0.2.0", lifespan=lifespan)
install_ui(app)


def write_auth(x_api_key: str = Header(default="")):
    if not hmac.compare_digest(x_api_key, settings().api_key):
        raise HTTPException(401, "valid write API key required")
    return "operator-key"


def read_auth(x_api_key: str = Header(default="")):
    if not settings().public_reads and not any(
        hmac.compare_digest(x_api_key, key) for key in (settings().api_key, settings().read_api_key)
    ):
        raise HTTPException(401, "valid API key required")


def db(request: Request):
    with request.app.state.factory.begin() as session:
        yield session


def serialize(row):
    result = {}
    for col in inspect(type(row)).columns:
        value = getattr(row, col.key)
        if isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat() + "Z"
        result[col.key] = value
    return result


@app.exception_handler(Conflict)
async def conflict_handler(request, error):
    if not request.url.path.startswith("/api/"):
        return ui_error(request, str(error), 409)
    return JSONResponse(status_code=409, content={"detail": str(error)})


@app.exception_handler(ValueError)
async def value_handler(request, error):
    if not request.url.path.startswith("/api/"):
        return ui_error(request, str(error), 422)
    return JSONResponse(status_code=422, content={"detail": str(error)})


@app.exception_handler(IntegrityError)
async def integrity_handler(request, error):
    return JSONResponse(
        status_code=409, content={"detail": "constraint conflict or missing referenced entity"}
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready(s=Depends(db, scope="function")):
    revision = s.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != "0002":
        raise HTTPException(503, "incompatible database schema")
    return {"status": "ready", "scope": "database-backed API", "schema": "0002"}


@app.get("/metrics", dependencies=[Depends(read_auth)])
def metrics(s=Depends(db, scope="function")):
    pending = s.scalar(select(func.count()).select_from(Outbox).where(Outbox.sent.is_(False)))
    quarantined = s.scalar(select(func.count()).select_from(Quarantine))
    return PlainTextResponse(
        f"opengrid_outbox_pending {pending}\nopengrid_quarantined {quarantined}\n"
    )


@app.post("/api/v1/readings/{kind}", status_code=202, dependencies=[Depends(write_auth)])
def ingest(kind: str, items: list[ReadingInput] | ReadingInput, s=Depends(db, scope="function")):
    if kind not in {"meters", "transformers", "bulk"}:
        raise HTTPException(404)
    batch = items if isinstance(items, list) else [items]
    if not batch or len(batch) > settings().max_bulk_readings:
        raise HTTPException(413, "batch size outside configured bounds")
    expected = {"meters": "METER", "transformers": "TRANSFORMER", "bulk": None}[kind]
    ids = [accept_reading(s, item, expected) for item in batch]
    return {"status": "ACCEPTED", "event_ids": ids}


@app.get("/api/v1/ingestions/{event_id}", dependencies=[Depends(read_auth)])
def ingestion_status(event_id: str, s=Depends(db, scope="function")):
    reading = s.scalar(select(Reading).where(Reading.event_id == event_id))
    if not reading:
        raise HTTPException(404)
    return {"event_id": event_id, "status": "ACCEPTED", "reading_id": reading.id}


def register_assets(path, kind):
    @app.post(
        f"/api/v1/{path}",
        status_code=201,
        dependencies=[Depends(write_auth)],
        name=f"create_{path}",
    )
    def create(item: AssetInput, s=Depends(db, scope="function")):
        if kind == "METER":
            parent = s.get(Asset, item.transformer_id) if item.transformer_id else None
            if not parent or parent.kind != "TRANSFORMER":
                raise ValueError("meter requires transformer_id")
        asset = Asset(code=item.code, kind=kind, name=item.name, feeder_id=item.feeder_id)
        s.add(asset)
        s.flush()
        s.add(
            Configuration(
                asset_id=asset.id,
                valid_from=item.valid_from.replace(tzinfo=None),
                multiplier=item.multiplier,
                import_only=item.import_only,
                modulus=item.modulus,
                max_power_kw=item.max_power_kw,
            )
        )
        if kind == "METER":
            s.add(
                Assignment(
                    meter_id=asset.id,
                    transformer_id=item.transformer_id,
                    valid_from=item.valid_from.replace(tzinfo=None),
                )
            )
        return serialize(asset)

    @app.get(f"/api/v1/{path}", dependencies=[Depends(read_auth)], name=f"list_{path}")
    def listing(
        page: int = Query(1, ge=1, le=10000),
        page_size: int = Query(100, ge=1, le=1000),
        sort: str = "id",
        order: str = "asc",
        s=Depends(db, scope="function"),
    ):
        if sort not in {"id", "code", "name"} or order not in {"asc", "desc"}:
            raise ValueError("unsupported sort")
        column = getattr(Asset, sort)
        query = (
            select(Asset)
            .where(Asset.kind == kind)
            .order_by(column.desc() if order == "desc" else column, Asset.id)
        )
        return [
            serialize(a) for a in s.scalars(query.offset((page - 1) * page_size).limit(page_size))
        ]

    @app.get(f"/api/v1/{path}/{{asset_id}}", dependencies=[Depends(read_auth)], name=f"get_{path}")
    def get(asset_id: int, s=Depends(db, scope="function")):
        asset = s.get(Asset, asset_id)
        if not asset or asset.kind != kind:
            raise HTTPException(404)
        return serialize(asset)

    @app.put(
        f"/api/v1/{path}/{{asset_id}}", dependencies=[Depends(write_auth)], name=f"update_{path}"
    )
    def update(asset_id: int, body: dict, s=Depends(db, scope="function")):
        if set(body) - {"name", "active"}:
            raise ValueError(
                "only name and active metadata may be edited; historical changes require assignment API"
            )
        asset = s.get(Asset, asset_id, with_for_update=True)
        if not asset or asset.kind != kind:
            raise HTTPException(404)
        if "name" in body:
            if not isinstance(body["name"], str) or len(body["name"]) > 255:
                raise ValueError("invalid name")
            asset.name = body["name"]
        if "active" in body:
            if not isinstance(body["active"], bool):
                raise ValueError("active must be boolean")
            asset.active = body["active"]
        return serialize(asset)


register_assets("meters", "METER")
register_assets("transformers", "TRANSFORMER")


@app.post("/api/v1/feeders", status_code=201, dependencies=[Depends(write_auth)])
def create_feeder(body: dict, s=Depends(db, scope="function")):
    if (
        set(body) - {"code", "name"}
        or not isinstance(body.get("code"), str)
        or not 1 <= len(body["code"]) <= 64
    ):
        raise ValueError("code required; optional name")
    feeder = Feeder(code=body["code"], name=body.get("name", ""))
    s.add(feeder)
    s.flush()
    return serialize(feeder)


@app.get("/api/v1/feeders", dependencies=[Depends(read_auth)])
def list_feeders(s=Depends(db, scope="function")):
    return [serialize(f) for f in s.scalars(select(Feeder).limit(1000))]


@app.get("/api/v1/meters/{asset_id}/{resource}", dependencies=[Depends(read_auth)])
@app.get("/api/v1/transformers/{asset_id}/{resource}", dependencies=[Depends(read_auth)])
def resource(
    request: Request,
    asset_id: int,
    resource: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
    before_id: int | None = None,
    s=Depends(db, scope="function"),
):
    kind = request.url.path.split("/")[3]
    models = {
        "readings": Reading,
        "energy": Interval,
        "quality": Interval,
        "balance": Balance,
        "anomalies": Anomaly,
        "evaluations": Evaluation,
        "events": MeterEvent,
    }
    if kind not in {"meters", "transformers"}:
        raise HTTPException(404)
    if resource == "meters" and kind == "transformers":
        return [
            serialize(r)
            for r in s.scalars(
                select(Asset)
                .join(Assignment, Assignment.meter_id == Asset.id)
                .where(Assignment.transformer_id == asset_id, Assignment.valid_to.is_(None))
                .limit(limit)
            )
        ]
    model = models.get(resource)
    if not model:
        raise HTTPException(404)
    query = select(model).where(model.asset_id == asset_id)
    column = model.time if model in {Reading, MeterEvent} else model.start
    for value in (start, end):
        if value and value.tzinfo is None:
            raise ValueError("time filter requires offset")
    if start:
        query = query.where(column >= start.astimezone(UTC).replace(tzinfo=None))
    if end:
        query = query.where(column < end.astimezone(UTC).replace(tzinfo=None))
    if before_id:
        query = query.where(model.id < before_id)
    return [serialize(r) for r in s.scalars(query.order_by(model.id.desc()).limit(limit))]


@app.get("/api/v1/anomalies", dependencies=[Depends(read_auth)])
def anomalies(
    status: str | None = None,
    severity: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    s=Depends(db, scope="function"),
):
    query = select(Anomaly)
    if status:
        query = query.where(Anomaly.status == status)
    if severity:
        query = query.where(Anomaly.severity == severity)
    return [serialize(r) for r in s.scalars(query.order_by(Anomaly.id.desc()).limit(limit))]


@app.get("/api/v1/investigations", dependencies=[Depends(read_auth)])
def cases(
    status: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    s=Depends(db, scope="function"),
):
    query = select(Case)
    if status:
        query = query.where(Case.status == status)
    return [
        serialize(r) for r in s.scalars(query.order_by(Case.priority.desc(), Case.id).limit(limit))
    ]


@app.get("/api/v1/investigations/{case_id}", dependencies=[Depends(read_auth)])
def case_get(case_id: int, s=Depends(db, scope="function")):
    row = s.get(Case, case_id)
    if not row:
        raise HTTPException(404)
    return serialize(row)


@app.get("/api/v1/investigations/{case_id}/history", dependencies=[Depends(read_auth)])
def case_history(case_id: int, s=Depends(db, scope="function")):
    return [
        serialize(r)
        for r in s.scalars(select(Audit).where(Audit.case_id == case_id).order_by(Audit.id))
    ]


@app.patch("/api/v1/investigations/{case_id}")
def case_patch(
    case_id: int, body: CaseUpdate, actor=Depends(write_auth), s=Depends(db, scope="function")
):
    try:
        return serialize(update_case(s, case_id, body, actor))
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@app.post("/api/v1/reprocessing-jobs", status_code=202, dependencies=[Depends(write_auth)])
def reprocess(body: ReprocessInput, s=Depends(db, scope="function")):
    transformer = s.get(Asset, body.transformer_id)
    if not transformer or transformer.kind != "TRANSFORMER":
        raise ValueError("unknown transformer")
    job = Job(id=str(uuid4()), payload=body.model_dump(mode="json"))
    s.add(job)
    emit(s, "api", "rabbit:openami.reprocessing", job.id, {"job_id": job.id})
    return {"job_id": job.id, "status": "QUEUED"}


@app.get("/api/v1/reprocessing-jobs/{job_id}", dependencies=[Depends(read_auth)])
def job_status(job_id: str, s=Depends(db, scope="function")):
    job = s.get(Job, job_id)
    if not job:
        raise HTTPException(404)
    return serialize(job)


@app.get("/api/v1/quality/summary", dependencies=[Depends(read_auth)])
def quality_summary(s=Depends(db, scope="function")):
    return [
        {"status": status, "intervals": count}
        for status, count in s.execute(
            select(Interval.status, func.count()).group_by(Interval.status)
        )
    ]


@app.get("/api/v1/loss/summary", dependencies=[Depends(read_auth)])
def loss_summary(s=Depends(db, scope="function")):
    t, m, d = s.execute(
        select(
            func.sum(Balance.input_kwh),
            func.sum(Balance.downstream_kwh),
            func.sum(Balance.accounting_difference_kwh),
        ).where(Balance.status == "COMPLETE")
    ).one()
    return {
        "input_kwh": str(t or 0),
        "downstream_kwh": str(m or 0),
        "accounting_difference_kwh": str(d or 0),
        "accounting_difference_percent": str(d / t * 100) if t and t > 0 else None,
    }


@app.get("/api/v1/system", dependencies=[Depends(read_auth)])
def system_status(s=Depends(db, scope="function")):
    return {
        "workers": [serialize(r) for r in s.scalars(select(Heartbeat))],
        "quarantined": s.scalar(select(func.count()).select_from(Quarantine)),
    }


@app.post("/api/v1/meters/{meter_id}/assignments", dependencies=[Depends(write_auth)])
def assign(meter_id: int, body: AssignmentInput, s=Depends(db, scope="function")):

    meter = s.get(Asset, meter_id, with_for_update=True)
    transformer = s.get(Asset, body.transformer_id)
    if not meter or meter.kind != "METER" or not transformer or transformer.kind != "TRANSFORMER":
        raise ValueError("valid meter and transformer required")
    boundary = body.valid_from.replace(tzinfo=None)
    current = s.scalar(
        select(Assignment).where(Assignment.meter_id == meter_id, Assignment.valid_to.is_(None))
    )
    if current and boundary <= current.valid_from:
        raise Conflict("new assignment must follow the current assignment start")
    old_transformer = current.transformer_id if current else None
    if current:
        current.valid_to = boundary
    assignment = Assignment(
        meter_id=meter_id, transformer_id=body.transformer_id, valid_from=boundary
    )
    s.add(assignment)
    # Rebuild all already calculated intervals affected by the mapping change.
    times = s.scalars(
        select(Interval.start).where(Interval.asset_id == meter_id, Interval.start >= boundary)
    ).all()
    if len(times) > 31 * 96:
        raise ValueError(
            "historical assignment edits are limited to 31 days; split the reprocessing range"
        )
    for time in times:
        for owner in {old_transformer, body.transformer_id} - {None}:
            emit(
                s,
                "api",
                "openami.quality.result",
                owner,
                {"asset_id": owner, "start": time.isoformat()},
            )
    s.flush()
    return serialize(assignment)


@app.post("/api/v1/events", status_code=202, dependencies=[Depends(write_auth)])
def event_create(body: EventInput, s=Depends(db, scope="function")):
    meter = s.get(Asset, body.meter_id, with_for_update=True)
    if not meter or meter.kind != "METER":
        raise ValueError("valid meter required")
    event_id = str(body.event_id)
    payload = {
        "asset_id": meter.id,
        "time": body.event_time.replace(tzinfo=None).isoformat(),
        "event_type": body.event_type,
        "evidence": body.evidence,
    }
    existing = s.get(Outbox, event_id)
    if existing:
        if existing.payload["payload"] != payload:
            raise Conflict("event ID conflict")
    else:
        s.add(
            Outbox(
                id=event_id,
                owner="api",
                topic="openami.meter.event",
                key=meter.code,
                payload={
                    "event_id": event_id,
                    "schema_version": 1,
                    "event_type": "meter.event",
                    "event_time": body.event_time.isoformat(),
                    "producer": "api",
                    "payload": payload,
                },
            )
        )
    return {"event_id": event_id, "status": "ACCEPTED"}


@app.get("/api/v1/feeders/{feeder_id}", dependencies=[Depends(read_auth)])
def feeder_get(feeder_id: int, s=Depends(db, scope="function")):
    row = s.get(Feeder, feeder_id)
    if not row:
        raise HTTPException(404)
    return serialize(row)


@app.put("/api/v1/feeders/{feeder_id}", dependencies=[Depends(write_auth)])
def feeder_update(feeder_id: int, body: dict, s=Depends(db, scope="function")):
    row = s.get(Feeder, feeder_id, with_for_update=True)
    if not row:
        raise HTTPException(404)
    if set(body) - {"name", "active"}:
        raise ValueError("only name and active may change")
    if "name" in body:
        if not isinstance(body["name"], str) or len(body["name"]) > 255:
            raise ValueError("invalid name")
        row.name = body["name"]
    if "active" in body:
        if not isinstance(body["active"], bool):
            raise ValueError("active must be boolean")
        row.active = body["active"]
    return serialize(row)


@app.get("/api/v1/anomalies/{anomaly_id}", dependencies=[Depends(read_auth)])
def anomaly_get(anomaly_id: int, s=Depends(db, scope="function")):
    row = s.get(Anomaly, anomaly_id)
    if not row:
        raise HTTPException(404)
    return serialize(row)

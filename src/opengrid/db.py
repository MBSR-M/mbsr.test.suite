from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from opengrid.config import settings

TIME = DateTime().with_variant(DATETIME(fsp=6), "mysql")
ID = BigInteger().with_variant(Integer, "sqlite")
NUMBER = Numeric(20, 6)


def now():
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Feeder(Base):
    __tablename__ = "feeder"
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Asset(Base):
    __tablename__ = "asset"
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255), default="")
    feeder_id: Mapped[int | None] = mapped_column(ForeignKey("feeder.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Assignment(Base):
    __tablename__ = "assignment"
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    transformer_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    valid_from: Mapped[datetime] = mapped_column(TIME)
    valid_to: Mapped[datetime | None] = mapped_column(TIME)


class Configuration(Base):
    __tablename__ = "measurement_configuration"
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    valid_from: Mapped[datetime] = mapped_column(TIME)
    valid_to: Mapped[datetime | None] = mapped_column(TIME)
    multiplier: Mapped[object] = mapped_column(NUMBER, default=1)
    import_only: Mapped[bool] = mapped_column(Boolean, default=True)
    modulus: Mapped[object | None] = mapped_column(NUMBER)
    max_power_kw: Mapped[object | None] = mapped_column(NUMBER)


class Reading(Base):
    __tablename__ = "reading"
    __table_args__ = (UniqueConstraint("asset_id", "time", "source", "revision"),)
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    time: Mapped[datetime] = mapped_column(TIME, index=True)
    source: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(32))
    epoch: Mapped[str] = mapped_column(String(64))
    primary: Mapped[bool] = mapped_column(Boolean)
    import_value: Mapped[object] = mapped_column(NUMBER)
    export_value: Mapped[object | None] = mapped_column(NUMBER)
    received_at: Mapped[datetime] = mapped_column(TIME)
    ingested_at: Mapped[datetime] = mapped_column(TIME, default=now)
    payload: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))


class Interval(Base):
    __tablename__ = "interval_energy"
    __table_args__ = (UniqueConstraint("asset_id", "start"),)
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    start: Mapped[datetime] = mapped_column(TIME, index=True)
    import_kwh: Mapped[object | None] = mapped_column(NUMBER)
    export_kwh: Mapped[object | None] = mapped_column(NUMBER)
    status: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[dict] = mapped_column(JSON)
    processed_at: Mapped[datetime] = mapped_column(TIME, default=now)


class ProjectionRevision(Base):
    __tablename__ = "projection_revision"
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"))
    start: Mapped[datetime] = mapped_column(TIME)
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TIME, default=now)


class Aggregate(Base):
    __tablename__ = "transformer_aggregate"
    __table_args__ = (UniqueConstraint("asset_id", "start"),)
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    start: Mapped[datetime] = mapped_column(TIME, index=True)
    expected: Mapped[int] = mapped_column(Integer)
    received: Mapped[int] = mapped_column(Integer)
    valid: Mapped[int] = mapped_column(Integer)
    downstream_kwh: Mapped[object] = mapped_column(NUMBER)
    fingerprint: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSON)


class Balance(Base):
    __tablename__ = "transformer_energy_balance"
    __table_args__ = (UniqueConstraint("asset_id", "start"),)
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    start: Mapped[datetime] = mapped_column(TIME, index=True)
    input_kwh: Mapped[object | None] = mapped_column(NUMBER)
    downstream_kwh: Mapped[object] = mapped_column(NUMBER)
    accounting_difference_kwh: Mapped[object | None] = mapped_column(NUMBER)
    accounting_difference_percent: Mapped[object | None] = mapped_column(NUMBER)
    completeness: Mapped[object | None] = mapped_column(NUMBER)
    status: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSON)


class Evaluation(Base):
    __tablename__ = "evaluation"
    __table_args__ = (UniqueConstraint("asset_id", "start"),)
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    start: Mapped[datetime] = mapped_column(TIME, index=True)
    eligible: Mapped[bool] = mapped_column(Boolean)
    abnormal: Mapped[bool] = mapped_column(Boolean)
    evidence: Mapped[dict] = mapped_column(JSON)


class Anomaly(Base):
    __tablename__ = "anomaly"
    __table_args__ = (UniqueConstraint("asset_id", "start", "anomaly_type"),)
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    start: Mapped[datetime] = mapped_column(TIME, index=True)
    anomaly_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    severity: Mapped[str] = mapped_column(String(16))
    score: Mapped[object] = mapped_column(NUMBER)
    evidence: Mapped[dict] = mapped_column(JSON)


class Case(Base):
    __tablename__ = "investigation_case"
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    case_no: Mapped[str] = mapped_column(String(40), unique=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    anomaly_id: Mapped[int] = mapped_column(ForeignKey("anomaly.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="NEW", index=True)
    priority: Mapped[object] = mapped_column(NUMBER)
    assigned_to: Mapped[str | None] = mapped_column(String(128))
    resolution: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[dict] = mapped_column(JSON)
    opened_at: Mapped[datetime] = mapped_column(TIME, default=now)
    closed_at: Mapped[datetime | None] = mapped_column(TIME)


class Audit(Base):
    __tablename__ = "case_history"
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("investigation_case.id"), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    body: Mapped[dict] = mapped_column(JSON)
    time: Mapped[datetime] = mapped_column(TIME, default=now)


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_owner_sent_created_at_id", "owner", "sent", "created_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(32), index=True)
    topic: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(TIME, default=now)


class Inbox(Base):
    __tablename__ = "inbox"
    consumer: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(TIME, default=now)


class Quarantine(Base):
    __tablename__ = "quarantine"
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    service: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TIME, default=now)


class Job(Base):
    __tablename__ = "processing_job"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED")
    payload: Mapped[dict] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIME, default=now)


class MeterEvent(Base):
    __tablename__ = "meter_event"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("asset.id"), index=True)
    time: Mapped[datetime] = mapped_column(TIME, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSON)


class Heartbeat(Base):
    __tablename__ = "worker_heartbeat"
    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    time: Mapped[datetime] = mapped_column(TIME)
    processed: Mapped[int] = mapped_column(Integer, default=0)


def session_factory():
    engine = create_engine(
        settings().database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        isolation_level="READ COMMITTED",
    )
    return sessionmaker(engine, expire_on_commit=False)

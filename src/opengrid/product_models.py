"""Additive operator identity and case collaboration models.

Case activity continues to use ``db.Audit``; no second audit store is introduced.
These models never change raw measurement or accounting tables.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from opengrid.db import ID, TIME, Base, now


class User(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(
            "role IN ('ADMIN', 'OPERATOR', 'INVESTIGATOR', 'VIEWER')", name="ck_user_role"
        ),
    )
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="VIEWER")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TIME, default=now)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TIME)


class BrowserSession(Base):
    __tablename__ = "browser_session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TIME, default=now)
    expires_at: Mapped[datetime] = mapped_column(TIME, index=True)


class InvestigationNote(Base):
    __tablename__ = "investigation_note"
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("investigation_case.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIME, default=now)


class FollowUp(Base):
    __tablename__ = "investigation_follow_up"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN', 'COMPLETED')", name="ck_follow_up_status"),
    )
    id: Mapped[int] = mapped_column(ID, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("investigation_case.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    assigned_to: Mapped[str | None] = mapped_column(ForeignKey("app_user.username"))
    body: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(TIME)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(TIME, default=now)
    completed_at: Mapped[datetime | None] = mapped_column(TIME)

"""Add local operator accounts, expiring sessions, notes and follow-ups.

Revision metadata is explicit and frozen. Existing case_history is retained as
the single case activity log and all released measurement tables are unchanged.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0002"
down_revision = "0001"

ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
TIME = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "app_user",
        sa.Column("id", ID, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", TIME),
        sa.CheckConstraint(
            "role IN ('ADMIN', 'OPERATOR', 'INVESTIGATOR', 'VIEWER')", name="ck_user_role"
        ),
    )
    op.create_table(
        "browser_session",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", ID, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("expires_at", TIME, nullable=False),
    )
    op.create_index("ix_browser_session_user_id", "browser_session", ["user_id"])
    op.create_index("ix_browser_session_expires_at", "browser_session", ["expires_at"])
    op.create_table(
        "investigation_note",
        sa.Column("id", ID, primary_key=True),
        sa.Column("case_id", ID, sa.ForeignKey("investigation_case.id"), nullable=False),
        sa.Column("author_id", ID, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
    )
    op.create_index("ix_investigation_note_case_id", "investigation_note", ["case_id"])
    op.create_table(
        "investigation_follow_up",
        sa.Column("id", ID, primary_key=True),
        sa.Column("case_id", ID, sa.ForeignKey("investigation_case.id"), nullable=False),
        sa.Column("author_id", ID, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("assigned_to", sa.String(64), sa.ForeignKey("app_user.username")),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("due_at", TIME),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TIME, nullable=False),
        sa.Column("completed_at", TIME),
        sa.CheckConstraint("status IN ('OPEN', 'COMPLETED')", name="ck_follow_up_status"),
    )
    op.create_index("ix_investigation_follow_up_case_id", "investigation_follow_up", ["case_id"])


def downgrade():
    op.drop_table("investigation_follow_up")
    op.drop_table("investigation_note")
    op.drop_table("browser_session")
    op.drop_table("app_user")

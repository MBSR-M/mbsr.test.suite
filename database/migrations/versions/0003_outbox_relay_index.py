"""Index pending outbox work by relay ownership and queue order.

The relay claims a bounded, ordered batch with ``FOR UPDATE SKIP LOCKED``.
The composite index lets MySQL find that batch without locking unrelated
historical outbox rows as the table grows.
"""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.create_index(
        "ix_outbox_owner_sent_created_at_id",
        "outbox",
        ["owner", "sent", "created_at", "id"],
    )


def downgrade():
    op.drop_index("ix_outbox_owner_sent_created_at_id", table_name="outbox")

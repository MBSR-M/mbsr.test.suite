"""Initial OpenGrid schema.

The initial metadata is frozen in schema_v1; later application model edits cannot
change the meaning of an already released migration.
"""

from alembic import op

from opengrid.schema_v1 import metadata

revision = "0001"
down_revision = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    metadata.drop_all(op.get_bind())

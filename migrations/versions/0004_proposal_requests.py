from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("proposals", sa.Column("request_scope", sa.String(128), nullable=True))
    op.add_column("proposals", sa.Column("request_key", sa.String(128), nullable=True))
    op.add_column("proposals", sa.Column("normalized_inputs", postgresql.JSONB(), nullable=True))
    op.create_unique_constraint("proposals_request_unique", "proposals", ["request_scope", "request_key"])
    op.create_check_constraint("proposals_request_complete", "proposals",
        "(request_scope IS NULL AND request_key IS NULL AND normalized_inputs IS NULL) OR "
        "(request_scope IS NOT NULL AND request_key IS NOT NULL AND normalized_inputs IS NOT NULL)")


def downgrade():
    op.drop_constraint("proposals_request_complete", "proposals", type_="check")
    op.drop_constraint("proposals_request_unique", "proposals", type_="unique")
    op.drop_column("proposals", "normalized_inputs")
    op.drop_column("proposals", "request_key")
    op.drop_column("proposals", "request_scope")

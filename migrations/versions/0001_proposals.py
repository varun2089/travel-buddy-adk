from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("inputs", postgresql.JSONB(), nullable=False),
        sa.Column("results", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "reviews",
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("proposals.id"), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("confirmation", sa.String(41), unique=True),
        sa.CheckConstraint("status IN ('NEEDS_REVIEW', 'SUBMITTED', 'REJECTED')"),
        sa.CheckConstraint("(status = 'SUBMITTED') = (confirmation IS NOT NULL)"),
    )
    op.create_table(
        "review_audit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("proposals.id"), nullable=False, unique=True),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("confirmation", sa.String(41)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("""
        CREATE FUNCTION prevent_proposal_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Proposals are immutable; create a new proposal';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER proposals_immutable BEFORE UPDATE ON proposals
        FOR EACH ROW EXECUTE FUNCTION prevent_proposal_update()
    """)


def downgrade():
    op.drop_table("review_audit")
    op.drop_table("reviews")
    op.drop_table("proposals")
    op.execute("DROP FUNCTION prevent_proposal_update()")

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "active_policies",
        sa.Column("document_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("section_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "version", "section_id"],
            ["policy_sections.document_id", "policy_sections.version", "policy_sections.section_id"],
            name="active_policy_ingested_section_fk",
        ),
    )


def downgrade():
    op.drop_table("active_policies")

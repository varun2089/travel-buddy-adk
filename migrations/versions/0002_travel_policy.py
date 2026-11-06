from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""
        CREATE TABLE policy_sections (
            document_id varchar(64) NOT NULL,
            version varchar(64) NOT NULL,
            section_id varchar(64) NOT NULL,
            heading varchar(160) NOT NULL,
            text text NOT NULL,
            source_path text NOT NULL,
            content_hash varchar(64) NOT NULL,
            embedding_model varchar(64) NOT NULL,
            dimensions integer NOT NULL CHECK (dimensions = 768),
            embedding vector(768) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (document_id, version, section_id)
        )
    """)


def downgrade():
    op.drop_table("policy_sections")

"""Add assigned_agent to complaints for human desk claim."""

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "complaints",
        sa.Column("assigned_agent", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("complaints", "assigned_agent")

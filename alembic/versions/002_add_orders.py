"""Revision 002: orders table for persistent order store."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="Unknown"),
        sa.Column("destination_country", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("total", sa.Float(), nullable=False, server_default="0"),
        sa.Column("tracking_number", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("carrier", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at_biz", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "items",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="csv"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_orders_order_id", "orders", ["order_id"], unique=True)
    op.create_index("ix_orders_email", "orders", ["email"])


def downgrade() -> None:
    op.drop_index("ix_orders_email", table_name="orders")
    op.drop_index("ix_orders_order_id", table_name="orders")
    op.drop_table("orders")

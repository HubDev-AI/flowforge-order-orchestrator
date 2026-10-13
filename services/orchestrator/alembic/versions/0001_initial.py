import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("payload_hash", sa.Text, nullable=False),
        sa.Column("stages", postgresql.JSONB, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("current_stage", sa.Text),
        sa.Column("current_kind", sa.Text),
        sa.Column("next_due_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("terminal_reason", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
    )
    op.create_index(
        "ix_orders_next_due_at",
        "orders",
        ["next_due_at"],
        postgresql_where=sa.text("next_due_at IS NOT NULL"),
    )
    op.create_table(
        "attempts",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("order_id", sa.Uuid, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("outcome", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("stage_output", postgresql.JSONB),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("order_id", "stage", "kind", "attempt", name="uq_attempts_key"),
    )
    op.create_index(
        "ix_attempts_open_deadline",
        "attempts",
        ["deadline_at"],
        postgresql_where=sa.text("finished_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("attempts")
    op.drop_table("orders")

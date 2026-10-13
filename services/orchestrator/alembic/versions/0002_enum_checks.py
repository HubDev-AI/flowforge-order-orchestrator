from alembic import op

revision = "0002"
down_revision = "0001"

ORDER_STATES = "'pending', 'running', 'compensating', 'completed', 'failed', 'compensation_failed'"
KINDS = "'execute', 'undo'"
OUTCOMES = "'succeeded', 'retry', 'timed_out', 'rejected', 'exhausted'"


def upgrade() -> None:
    op.create_check_constraint("ck_orders_state", "orders", f"state IN ({ORDER_STATES})")
    op.create_check_constraint(
        "ck_orders_current_kind", "orders", f"current_kind IS NULL OR current_kind IN ({KINDS})"
    )
    op.create_check_constraint("ck_attempts_kind", "attempts", f"kind IN ({KINDS})")
    op.create_check_constraint(
        "ck_attempts_outcome", "attempts", f"outcome IS NULL OR outcome IN ({OUTCOMES})"
    )


def downgrade() -> None:
    op.drop_constraint("ck_attempts_outcome", "attempts")
    op.drop_constraint("ck_attempts_kind", "attempts")
    op.drop_constraint("ck_orders_current_kind", "orders")
    op.drop_constraint("ck_orders_state", "orders")

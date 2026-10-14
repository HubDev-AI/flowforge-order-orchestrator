import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.add_column("orders", sa.Column("waiting_since", sa.TIMESTAMP(timezone=True)))


def downgrade() -> None:
    op.drop_column("orders", "waiting_since")

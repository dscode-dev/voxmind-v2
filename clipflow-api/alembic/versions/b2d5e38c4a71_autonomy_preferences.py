"""Os interruptores de autonomia viram estado que o dono governa.

Estavam só no `.env`, e a tela os mostrava como "definido no servidor, somente leitura" —
para ligar a publicação automática era preciso editar um arquivo na máquina e reiniciar a
API. O ambiente continua valendo como teto; esta tabela guarda a intenção.

Uma linha só, com id fixo: não existem dois donos.

Revision ID: b2d5e38c4a71
Revises: a1c4f27b9d30
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b2d5e38c4a71"
down_revision = "a1c4f27b9d30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "autonomy_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Anulável de propósito: NULL é "não decidi" e deixa o ambiente responder, o que é
        # diferente de FALSE, que é a decisão de desligar.
        sa.Column("autopublish_enabled", sa.Boolean(), nullable=True),
        sa.Column("autopublish_public_enabled", sa.Boolean(), nullable=True),
        sa.Column("default_privacy", sa.String(length=16), nullable=True),
        sa.Column("max_per_day", sa.Integer(), nullable=True),
        sa.Column("metrics_collection_enabled", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("autonomy_preferences")

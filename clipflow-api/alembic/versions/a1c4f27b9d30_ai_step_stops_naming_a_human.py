"""O passo de IA deixa de nomear um humano.

``AWAITING_MANUAL_LLM`` descrevia o fluxo antigo: o worker escrevia um prompt, mandava por
Telegram, e a run parava até alguém colar a resposta de volta. Esse fluxo não existe mais —
a IA decide os cortes dentro do pipeline — então o estado passa a se chamar pelo que de fato
acontece nele.

Postgres não renomeia um valor de enum em uso sem reescrever a coluna, então os dois tipos
(`job_status_enum` e `job_queue_status_enum`) são recriados e as linhas existentes migradas.

Revision ID: a1c4f27b9d30
Revises: 7e7d6cbf37fb
"""
from alembic import op

revision = "a1c4f27b9d30"
down_revision = "7e7d6cbf37fb"
branch_labels = None
depends_on = None

_VALUES_NEW = (
    "PENDING_PAYMENT", "QUEUED", "PREPARING", "PROCESSING_AI",
    "FINALIZING", "COMPLETED", "FAILED", "CANCELED",
)
_VALUES_OLD = (
    "PENDING_PAYMENT", "QUEUED", "PREPARING", "AWAITING_MANUAL_LLM",
    "FINALIZING", "COMPLETED", "FAILED", "CANCELED",
)

# A coluna que cada tipo governa.
_TARGETS = (
    ("job_status_enum", "clip_jobs", "status"),
    ("job_queue_status_enum", "job_queue", "status"),
)


def _swap(type_name: str, table: str, column: str, values, old_value: str, new_value: str) -> None:
    joined = ", ".join(f"'{v}'" for v in values)
    op.execute(f"ALTER TYPE {type_name} RENAME TO {type_name}_old")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({joined})")
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {type_name} "
        f"USING (CASE {column}::text WHEN '{old_value}' THEN '{new_value}' "
        f"ELSE {column}::text END)::{type_name}"
    )
    op.execute(f"DROP TYPE {type_name}_old")


def upgrade() -> None:
    for type_name, table, column in _TARGETS:
        _swap(type_name, table, column, _VALUES_NEW, "AWAITING_MANUAL_LLM", "PROCESSING_AI")


def downgrade() -> None:
    for type_name, table, column in _TARGETS:
        _swap(type_name, table, column, _VALUES_OLD, "PROCESSING_AI", "AWAITING_MANUAL_LLM")

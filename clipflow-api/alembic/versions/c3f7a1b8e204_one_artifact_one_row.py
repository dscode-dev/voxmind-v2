"""Um artefato no storage, uma linha.

A sincronização procurava a linha do clipe antes de criar, mas duas sincronizações
simultâneas — o cliente que expirou em 20 segundos e a retentativa dele — passavam as duas
pela procura e inseriam as duas. Numa execução real `final_clip_01.mp4` ficou duplicado com
22 segundos de diferença, e a tela mostrava o mesmo corte duas vezes.

Também apaga as linhas que apontam para um corte que nunca foi renderizado: a sincronização
caía para `file_name` quando `final_file_name` estava ausente, criando
`final_clips/cut_03.mp4`, que não existe. Eram botões de download respondendo 404.

Revision ID: c3f7a1b8e204
Revises: b2d5e38c4a71
"""
from alembic import op

revision = "c3f7a1b8e204"
down_revision = "b2d5e38c4a71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Os fantasmas primeiro: um clipe cujo caminho final ainda carrega o nome do corte
    # intermediário nunca teve arquivo.
    op.execute(
        "DELETE FROM clip_assets "
        "WHERE storage_key LIKE '%/final_clips/cut_%'"
    )
    # Depois as duplicatas, mantendo a mais antiga — é a que qualquer coisa já referenciou.
    op.execute(
        """
        DELETE FROM clip_assets a
        USING clip_assets b
        WHERE a.job_id = b.job_id
          AND a.storage_key = b.storage_key
          AND a.created_at > b.created_at
        """
    )
    op.create_unique_constraint(
        "uq_clip_assets_job_storage_key", "clip_assets", ["job_id", "storage_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_clip_assets_job_storage_key", "clip_assets", type_="unique")

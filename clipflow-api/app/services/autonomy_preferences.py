"""O valor em vigor de cada interruptor de autonomia.

Duas fontes, uma resposta. O `.env` é o **teto** — o que o servidor permite — e a preferência
guardada é a **intenção** do dono. O efetivo é a interseção: um interruptor desligado no
ambiente não pode ser ligado pela tela, e um limite acima do teto é reduzido a ele.

Por que teto e não substituição: desligar publicação no ambiente é a alavanca de quem opera a
máquina, e uma tela que pudesse contrariá-la tornaria o `.env` decorativo. Ligar pela tela o
que o ambiente já permite não tem esse problema — é exatamente a preferência que o dono
deveria governar.

`None` na preferência significa "não decidi", e não `False`. Sem essa distinção, a primeira
gravação de qualquer campo transformaria os padrões do servidor em escolhas do operador.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.settings import (
    AUTOPUBLISH_CEILING_PER_DAY,
    VALID_AUTOPUBLISH_PRIVACY,
    settings,
)
from app.models.autonomy_preference import SINGLETON_ID, AutonomyPreference

# Privacidade não tem "teto" numérico, mas tem ordem de risco: `public` é o único valor que
# expõe o canal, e ele continua atrás do seu próprio interruptor.
_PRIVACY_FALLBACK = "private"


@dataclass(frozen=True)
class Autonomy:
    """O que vale agora, já com o teto aplicado."""

    autopublish_enabled: bool
    autopublish_public_enabled: bool
    default_privacy: str
    max_per_day: int
    metrics_collection_enabled: bool

    # O que o ambiente permite, para a tela poder explicar por que um interruptor não sobe.
    ceiling_autopublish: bool
    ceiling_public: bool
    ceiling_max_per_day: int

    def as_dict(self) -> dict:
        return {
            "autopublish_enabled": self.autopublish_enabled,
            "autopublish_public_enabled": self.autopublish_public_enabled,
            "default_privacy": self.default_privacy,
            "max_per_day": self.max_per_day,
            "metrics_collection_enabled": self.metrics_collection_enabled,
            "ceiling": {
                "autopublish_enabled": self.ceiling_autopublish,
                "autopublish_public_enabled": self.ceiling_public,
                "max_per_day": self.ceiling_max_per_day,
            },
        }


def _row(db: Session) -> AutonomyPreference:
    row = db.get(AutonomyPreference, SINGLETON_ID)
    if row is None:
        row = AutonomyPreference(id=SINGLETON_ID)
        db.add(row)
        db.flush()
    return row


def load(db: Session) -> Autonomy:
    row = _row(db)

    ceiling_autopublish = bool(settings.autopublish_enabled)
    ceiling_public = bool(settings.autopublish_public_enabled)
    ceiling_per_day = min(int(settings.autopublish_max_per_day), AUTOPUBLISH_CEILING_PER_DAY)

    autopublish = (
        ceiling_autopublish
        if row.autopublish_enabled is None
        else (bool(row.autopublish_enabled) and ceiling_autopublish)
    )
    public = (
        ceiling_public
        if row.autopublish_public_enabled is None
        else (bool(row.autopublish_public_enabled) and ceiling_public)
    )

    privacy = (row.default_privacy or settings.autopublish_default_privacy or "").strip().lower()
    if privacy not in VALID_AUTOPUBLISH_PRIVACY:
        privacy = _PRIVACY_FALLBACK
    if privacy == "public" and not public:
        # Um padrão que o interruptor não autoriza seria uma recusa a cada publicação. Cair
        # para `unlisted` mantém o vídeo publicável e fora do alcance do público.
        privacy = "unlisted"

    per_day = (
        ceiling_per_day
        if row.max_per_day is None
        else max(0, min(int(row.max_per_day), ceiling_per_day))
    )

    metrics = (
        bool(settings.metrics_collection_enabled)
        if row.metrics_collection_enabled is None
        else bool(row.metrics_collection_enabled)
    )

    return Autonomy(
        autopublish_enabled=autopublish,
        autopublish_public_enabled=public,
        default_privacy=privacy,
        max_per_day=per_day,
        metrics_collection_enabled=metrics,
        ceiling_autopublish=ceiling_autopublish,
        ceiling_public=ceiling_public,
        ceiling_max_per_day=ceiling_per_day,
    )


def update(db: Session, changes: dict) -> Autonomy:
    """Grava a intenção. O teto é aplicado na leitura, não aqui.

    De propósito: guardar já cortado perderia a intenção do dono no dia em que o teto subir,
    e ele teria de reconfigurar tudo sem saber que precisava.
    """
    row = _row(db)
    for field in (
        "autopublish_enabled",
        "autopublish_public_enabled",
        "default_privacy",
        "max_per_day",
        "metrics_collection_enabled",
    ):
        if field in changes:
            setattr(row, field, changes[field])
    db.add(row)
    db.flush()
    return load(db)

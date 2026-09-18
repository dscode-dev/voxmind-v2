"""Por que este pipeline não está produzindo nada.

É a pergunta que o operador faz, e até agora a tela respondia com silêncio. Um pipeline pode
estar perfeitamente configurado e mesmo assim não fazer nada por qualquer uma de seis razões
que moram em lugares diferentes — uma variável de ambiente, um processo que não subiu, uma
credencial ausente, uma lista vazia. Nenhuma delas aparece olhando o pipeline.

Cada verificação devolve três coisas: se passou, o que significa em português, e o que fazer.
Um diagnóstico que diz "falta a chave de descoberta" e não diz onde colocá-la só move o
problema.

**Ordem importa.** As verificações vêm na sequência em que param o trabalho: nada adianta ter
fontes se o interruptor geral está desligado. A primeira que falha é a que o operador deve
resolver primeiro, e a tela mostra nessa ordem.

**Bloqueio e aviso são coisas diferentes.** Sem fonte, o ciclo não encontra nada — é bloqueio.
Sem canal, ele encontra, corta e para antes de publicar — é aviso: o trabalho acontece, só não
sai do lugar. Juntar os dois faria um pipeline que produz parecer um pipeline quebrado.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.discovery_source import DiscoverySource
from app.models.pipeline import Pipeline
from app.models.publish_target import PublishTarget
from app.publishing.identity import AutomationHeartbeat, PublisherHeartbeat

logger = logging.getLogger(__name__)

# Onde o worker de produção anuncia que está vivo. O mesmo prefixo que `worker/app/main.py`
# usa; lido aqui em vez de importado porque são dois repositórios.
WORKER_KEY_PREFIX = "clipflow:workers"

BLOCKER = "blocker"
WARNING = "warning"


@dataclass
class Check:
    code: str
    ok: bool
    severity: str
    title: str
    detail: str
    action: str | None = None
    href: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "ok": self.ok,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "action": self.action,
            "href": self.href,
        }


@dataclass
class Readiness:
    checks: list[Check] = field(default_factory=list)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == BLOCKER]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == WARNING]

    def as_dict(self) -> dict[str, Any]:
        return {
            # `ready` é sobre produzir cortes, não sobre publicá-los. Um pipeline sem canal
            # produz — e dizer que ele não está pronto esconderia que o trabalho acontece.
            "ready": not self.blockers,
            "blocking": len(self.blockers),
            "warnings": len(self.warnings),
            "checks": [c.as_dict() for c in self.checks],
        }


class PipelineReadinessService:
    """Responde, para um pipeline, o que falta para ele funcionar."""

    def evaluate(self, db: Session, pipeline: Pipeline, *, shared: dict | None = None) -> Readiness:
        """`shared` carrega o que é global — interruptor, processos, chave — para uma lista de
        pipelines não perguntar o mesmo ao Redis uma vez por linha."""
        facts = shared if shared is not None else self.shared_facts()
        automation = dict((pipeline.metadata_json or {}).get("automation") or {})

        checks = [
            Check(
                code="global_switch",
                ok=bool(facts["autonomous_enabled"]),
                severity=BLOCKER,
                title="Interruptor geral da automação",
                detail=(
                    "Ligado: os pipelines podem rodar sozinhos."
                    if facts["autonomous_enabled"]
                    else "Desligado no servidor. Nenhum pipeline roda sozinho enquanto estiver assim."
                ),
                action=None if facts["autonomous_enabled"] else "AUTONOMOUS_PIPELINE_ENABLED=true no .env, e reiniciar a API",
            ),
            Check(
                code="scheduler_alive",
                ok=facts["runners_alive"] > 0,
                severity=BLOCKER,
                title="Processo do agendador",
                detail=(
                    f"{facts['runners_alive']} processo(s) executando os ciclos."
                    if facts["runners_alive"]
                    else "Nenhum processo dá sinal de vida. Nada vai disparar um ciclo."
                ),
                action=None if facts["runners_alive"] else "Verificar se o clipflow-api está no ar",
            ),
            Check(
                code="pipeline_active",
                ok=bool(pipeline.is_active) and automation.get("enabled") is True,
                severity=BLOCKER,
                title="Automação deste pipeline",
                detail=(
                    "Ligada."
                    if pipeline.is_active and automation.get("enabled") is True
                    else "Pausado." if not pipeline.is_active
                    else "O pipeline está ativo, mas a automação dele está desligada — ele só roda quando alguém aperta Executar agora."
                ),
                action=None if (pipeline.is_active and automation.get("enabled") is True) else "Editar o pipeline e ligar a automação",
                href=f"/pipelines/{pipeline.id}",
            ),
            self._sources(db, pipeline, automation),
            Check(
                code="discovery_credential",
                ok=bool(facts["discovery_configured"]),
                severity=BLOCKER,
                title="Chave de busca do YouTube",
                detail=(
                    "Configurada."
                    if facts["discovery_configured"]
                    else "Ausente. A busca não consegue consultar o YouTube, então nenhum vídeo será encontrado."
                ),
                action=None if facts["discovery_configured"] else "YOUTUBE_API_KEY no .env, e reiniciar a API",
            ),
            Check(
                code="production_worker",
                ok=facts["workers_alive"] > 0,
                severity=BLOCKER,
                title="Worker de produção",
                detail=(
                    f"{facts['workers_alive']} worker(s) prontos para cortar."
                    if facts["workers_alive"]
                    else "Nenhum worker vivo. Os vídeos admitidos vão para a fila e ficam lá."
                ),
                action=None if facts["workers_alive"] else "COMPOSE_PROFILES=cpu (ou gpu) no .env, e docker compose up -d",
            ),
            self._channel(db, automation),
        ]
        return Readiness(checks=checks)

    # ------------------------------------------------------------------ partes

    def _sources(self, db: Session, pipeline: Pipeline, automation: dict) -> Check:
        active = (
            db.query(DiscoverySource)
            .filter(
                DiscoverySource.pipeline_id == pipeline.id,
                DiscoverySource.is_active.is_(True),
            )
            .count()
        )
        # Com a busca desligada o pipeline vive de admissão manual, e cobrar fonte dele seria
        # apontar um problema que ele não tem.
        if automation.get("discovery_enabled") is False:
            return Check(
                code="sources",
                ok=True,
                severity=BLOCKER,
                title="Fontes de busca",
                detail="A busca está desligada neste pipeline; ele produz o que você admitir à mão.",
            )
        return Check(
            code="sources",
            ok=active > 0,
            severity=BLOCKER,
            title="Fontes de busca",
            detail=(
                f"{active} fonte(s) ativa(s)."
                if active
                else "Nenhuma fonte ativa. A busca não tem onde procurar."
            ),
            action=None if active else "Adicionar uma fonte na aba Fontes",
            href=f"/pipelines/{pipeline.id}",
        )

    def _channel(self, db: Session, automation: dict) -> Check:
        # O id vem do JSON como texto, e a coluna é UUID. Sem a coerção o SQLAlchemy quebra
        # no bind em vez de não encontrar nada — e um diagnóstico que estoura é pior do que
        # o problema que ele existe para reportar.
        target = None
        target_id = _as_uuid(automation.get("publish_target_id"))
        if target_id is not None:
            target = db.query(PublishTarget).filter(PublishTarget.id == target_id).first()

        publishable = bool(target and target.is_publishable)
        return Check(
            code="channel",
            ok=publishable,
            # Aviso, não bloqueio: sem canal o pipeline encontra, escolhe e corta. Ele só não
            # publica. Tratar isso como quebra faria um pipeline que produz parecer parado.
            severity=WARNING,
            title="Canal de publicação",
            detail=(
                f"Publica em {target.channel_title or target.name}."
                if publishable
                else "Sem canal publicável. Os cortes ficam prontos e esperam alguém publicá-los."
            ),
            action=None if publishable else "Conectar um canal em Publicação",
            href=None if publishable else "/publicacao",
        )

    # ------------------------------------------------------------------ global

    def shared_facts(self) -> dict[str, Any]:
        """O que vale para todos os pipelines, buscado uma vez."""
        return {
            "autonomous_enabled": bool(settings.autonomous_pipeline_enabled),
            "discovery_configured": bool(str(settings.youtube_api_key or "").strip()),
            "runners_alive": len(_alive(AutomationHeartbeat)),
            "workers_alive": len(_alive(_ProductionWorkerHeartbeat)),
        }


def _as_uuid(value: Any) -> uuid.UUID | None:
    """Um id mal formado é "sem canal", nunca uma exceção."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


class _ProductionWorkerHeartbeat(PublisherHeartbeat):
    """O worker de produção, sob o prefixo dele.

    Mesmo mecanismo dos outros dois; só o namespace muda. Escrito aqui em vez de importado do
    worker porque são dois repositórios — e é por isso que o prefixo está numa constante com
    o nome do arquivo que o define do outro lado.
    """

    prefix = WORKER_KEY_PREFIX


def _alive(heartbeat_class) -> list:
    """Quem está vivo, lido do Redis e nunca da configuração.

    "Está configurado" e "está rodando" são afirmações diferentes, e reportar a primeira como
    a segunda é exatamente como um processo morto fica invisível.
    """
    try:
        return heartbeat_class.alive()
    except Exception:  # noqa: BLE001
        logger.warning("readiness_heartbeat_unreadable",
                       extra={"prefix": heartbeat_class.prefix})
        return []

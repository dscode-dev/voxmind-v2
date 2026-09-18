"""As preferências de autonomia que o dono governa.

Os interruptores que decidem se o sistema publica sozinho viviam só no `.env`, e a tela os
mostrava sob "Definido no servidor — somente leitura". Para ligar a única coisa que produz
resultado no canal era preciso editar um arquivo na máquina e reiniciar a API.

Aqui eles viram estado, editável pelo painel. O `.env` continua valendo, mas como **teto**:
um interruptor desligado no ambiente não pode ser ligado pela tela, e um limite configurado
acima do teto do servidor é reduzido a ele. O mesmo padrão que `AUTOPUBLISH_CEILING_*` já
aplicava aos números, agora também aos interruptores.

Uma linha só. Não existem dois donos.
"""
import uuid

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# A linha. Fixa para que ler e escrever nunca dependam de descobrir qual é.
SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-00000000a501")


class AutonomyPreference(Base, TimestampMixin):
    __tablename__ = "autonomy_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=SINGLETON_ID
    )

    # `None` significa "não decidi" e deixa o ambiente responder. Diferente de `False`, que
    # é uma decisão de desligar — a distinção importa para não transformar um padrão do
    # servidor em escolha do operador sem que ele tenha escolhido.
    autopublish_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    autopublish_public_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    default_privacy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    max_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics_collection_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

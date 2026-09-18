"""Assinar uma URL não pode depender de alcançar o servidor.

Assinatura SigV4 é aritmética local. Mas sem uma região declarada o cliente vai buscá-la com
um `GetBucketLocation` — contra o endpoint *público*, que de dentro do contêiner da API não
responde. Cada URL assinada falhava em silêncio depois de ~6 segundos de retentativas.

Isso custou duas coisas ao mesmo tempo:

* o Studio nunca recebia link para baixar um corte, porque a assinatura devolvia `None`;
* a sincronização de artefatos, que assina uma URL por clipe, levava 78 segundos — e o
  worker desiste em 20. A run produzia os cortes e nunca conseguia registrar que terminou.
"""
from __future__ import annotations

import pytest

from app.core.settings import settings
from app.services.asset_url_service import AssetUrlService


@pytest.fixture()
def service():
    return AssetUrlService()


def test_the_region_is_declared_so_nothing_is_asked_of_the_server(service):
    """O cliente carrega a região; com ela, presign não faz rede nenhuma."""
    assert service.client._base_url.region == settings.minio_region
    assert settings.minio_region


def test_a_signed_url_is_produced_without_the_server_being_reachable(service, monkeypatch):
    """O controle do teste acima: se qualquer chamada de rede acontecesse, isto explodiria."""
    import urllib3

    def explode(*args, **kwargs):
        raise AssertionError("presign nao pode tocar a rede")

    monkeypatch.setattr(urllib3.PoolManager, "urlopen", explode)

    url = service.build_signed_url("jobs/abc/final_clips/final_clip_01.mp4")

    assert url is not None
    assert "X-Amz-Signature" in url


def test_the_url_points_at_the_endpoint_the_browser_will_call(service):
    """Assinar contra `minio:9000` e reescrever o host depois invalidaria a assinatura."""
    url = service.build_signed_url("jobs/abc/final_clips/final_clip_01.mp4")

    assert settings.resolved_minio_public_endpoint in url


def test_no_key_is_no_url(service):
    assert service.build_signed_url(None) is None
    assert service.build_signed_url("") is None

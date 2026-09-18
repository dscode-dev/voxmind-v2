"""Um artefato no storage, uma linha — e nenhuma linha sem artefato.

Numa execução real o pacote de entrega listou quatro cortes, mas só dois viraram arquivo. A
sincronização caía para o nome do corte intermediário quando o nome final estava ausente e
criava `final_clips/cut_03.mp4`, que não existe em lugar nenhum: um botão de download que
responde 404.

No mesmo job, `final_clip_01.mp4` apareceu duas vezes. O cliente do worker expira em 20
segundos e repete; as duas chamadas passaram pela procura antes de qualquer uma inserir.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.clip_asset import ClipAsset
from app.models.enums import AssetStatus, ClipAssetType, JobStatus
from app.services.job_artifact_sync import JobArtifactSyncService
from tests.conftest import make_clip_job


def _package(clips):
    return {"clips": clips}


@pytest.fixture()
def service():
    return JobArtifactSyncService()


def test_a_cut_that_was_never_rendered_gets_no_row(db, service):
    job = make_clip_job(db, status=JobStatus.COMPLETED)

    service._sync_clip_assets(
        db,
        job,
        _package([
            {"clip_index": 1, "file_name": "cut_01.mp4", "final_file_name": "final_clip_01.mp4"},
            {"clip_index": 3, "file_name": "cut_03.mp4", "final_file_name": None},
        ]),
        None,
    )
    db.flush()

    keys = [a.storage_key for a in db.query(ClipAsset).filter(ClipAsset.job_id == job.id)]
    assert keys == [f"jobs/{job.id}/final_clips/final_clip_01.mp4"]


def test_syncing_twice_does_not_duplicate(db, service):
    """O caminho normal: a segunda sincronização encontra a linha e a atualiza."""
    job = make_clip_job(db, status=JobStatus.COMPLETED)
    package = _package(
        [{"clip_index": 1, "file_name": "cut_01.mp4", "final_file_name": "final_clip_01.mp4"}]
    )

    service._sync_clip_assets(db, job, package, None)
    db.flush()
    service._sync_clip_assets(db, job, package, None)
    db.flush()

    assert db.query(ClipAsset).filter(ClipAsset.job_id == job.id).count() == 1


def test_the_database_refuses_a_second_row_for_the_same_object(db):
    """A rede de segurança para o caso em que duas sincronizações correm juntas: a procura
    não protege quem passou por ela antes de qualquer inserção acontecer."""
    job = make_clip_job(db, status=JobStatus.COMPLETED)
    key = f"jobs/{job.id}/final_clips/final_clip_01.mp4"

    for _ in range(2):
        db.add(
            ClipAsset(
                job_id=job.id,
                asset_type=ClipAssetType.SHORT_CLIP,
                status=AssetStatus.READY,
                order_index=1,
                storage_key=key,
                start_sec=0,
                end_sec=0,
                duration_sec=0,
            )
        )

    with pytest.raises(IntegrityError):
        db.flush()

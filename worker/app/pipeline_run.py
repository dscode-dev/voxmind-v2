from pathlib import Path
import json
import logging

from .media.downloader import VideoDownloader
from .media.audio_extractor import AudioExtractor
from .media.transcriber import Transcriber
from .settings import settings

log = logging.getLogger("voxmind.worker.pipeline")

def run_pipeline() -> dict:
    work = Path(settings.work_dir)
    videos = work / "videos"
    audio = work / "audio"
    outdir = work / "out"
    for d in (videos, audio, outdir):
        d.mkdir(parents=True, exist_ok=True)

    downloader = VideoDownloader(videos)
    extractor = AudioExtractor(audio)
    transcriber = Transcriber(
        model_size=settings.asr_model_size,
        compute_type=settings.asr_compute_type,
        language=settings.asr_language,
        beam_size=settings.asr_beam_size,
        vad_filter=settings.asr_vad_filter,
    )

    log.info("download.start", extra={"video_url": settings.video_url})
    video_path = downloader.download(settings.video_url)
    log.info("download.done", extra={"video_path": str(video_path)})

    log.info("audio.extract.start", extra={"video_path": str(video_path)})
    audio_path = extractor.extract_wav_16k_mono(video_path)
    log.info("audio.extract.done", extra={"audio_path": str(audio_path)})

    log.info("asr.start", extra={"audio_path": str(audio_path), "model": settings.asr_model_size})
    segments = transcriber.transcribe(audio_path)
    log.info("asr.done", extra={"segments": len(segments)})

    result = {
        "video_url": settings.video_url,
        "video_path": str(video_path),
        "audio_path": str(audio_path),
        "segments": segments,
    }

    transcript_path = outdir / "transcript.json"
    transcript_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("artifact.written", extra={"transcript_path": str(transcript_path)})

    return {"transcript_path": str(transcript_path), "segments": len(segments)}

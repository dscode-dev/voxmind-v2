PIPELINE_STEPS = [
    "DOWNLOAD_FINISHED",
    "TRANSCRIPTION_FINISHED",
    "DIARIZATION_FINISHED",
    "LLM_REQUEST_FINISHED",
    "RENDER_FINISHED",
    "QA_FINISHED",
    "DELIVERY_PACKAGE_READY",
    "JOB_COMPLETED",
]


def calculate_progress(events):
    if not events:
        return 0

    completed_steps = {
        e.event_type.name
        for e in events
        if e.event_type.name in PIPELINE_STEPS
    }

    progress = int((len(completed_steps) / len(PIPELINE_STEPS)) * 100)

    return min(progress, 100)

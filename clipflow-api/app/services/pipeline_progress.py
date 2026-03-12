PIPELINE_STEPS = [
    "DOWNLOAD_STARTED",
    "DOWNLOAD_FINISHED",
    "TRANSCRIPTION_STARTED",
    "TRANSCRIPTION_FINISHED",
    "LLM_REQUEST_STARTED",
    "LLM_REQUEST_FINISHED",
    "CUT_GENERATED",
    "RENDER_STARTED",
    "RENDER_FINISHED",
    "JOB_COMPLETED",
]


def calculate_progress(events):

    if not events:
        return 0

    completed = 0

    for e in events:
        if e.event_type.value in PIPELINE_STEPS:
            completed += 1

    progress = int((completed / len(PIPELINE_STEPS)) * 100)

    return min(progress, 100)
# Evaluation datasets

One directory per case. The runner discovers any directory containing `metadata.json`.

```
voxmind/<case_id>/
  metadata.json                  case_id, topic, clip_mode, video_ratio, job_preset, source_type
  transcript_with_speakers.json  ASR segments with speaker labels (or transcript.json)
  expected.json                  human labels, when they exist
  ai_response.json               optional recorded provider response; replayed verbatim
```

`source_type` must be `real` or `synthetic`, and the evaluation report counts them
separately. **Do not commit video files.** Only structured artifacts belong here.

## Adding a real Voxmind case

1. Run a job and download `jobs/<id>/transcript_with_speakers.json` from MinIO.
2. Create `voxmind/case_NNN_<slug>/` and drop the transcript in.
3. Write `metadata.json` with `"source_type": "real"` and the preset the job used.
4. Optionally add `ai_response.json` (from `jobs/<id>/ai_response.json`) so the case replays
   the exact selection instead of a synthesised one.
5. Optionally label `expected.json`. Only label what you actually watched.

The current corpus is entirely synthetic — see `evaluation/fixtures.py` for why.

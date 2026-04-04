# VoxMind V3 Production Plan

## Objective

Document the next production version of the VoxMind pipeline with focus on:

- more precise and intelligent cuts
- stronger and more predictable hooks
- correct handling of source language
- subtitle reliability and professional quality
- a more operable architecture for a SaaS product

This document assumes:

- no MPV in the workflow
- the current V2 worker remains the operational baseline
- V3 should be implemented incrementally without breaking the working flow

## Current V2 Baseline

What is already working well enough to preserve as a base:

- hook extraction and cut boundaries are much better than before
- final rendering is stable enough and no longer freezing the video
- final videos are being assembled and delivered end-to-end
- the worker, control-plane, API and Studio are already integrated enough for iteration

What is still fragile:

- source language handling is hardcoded and biased toward Portuguese
- subtitle timing is still heuristic-based
- hook choice still depends too much on free-form LLM output
- the current cold open behavior does not match the desired production assembly model
- observability is still too weak for a true SaaS operation

## Main Problems Identified

### 1. Language Handling Is Not Production Ready

Today the ASR language is effectively forced by configuration.

Current issue:

- videos in English are pushed into a Portuguese-oriented flow
- the pipeline may translate or localize output when it should preserve the original language
- subtitle and metadata language are not controlled independently

Production expectation:

- if the video is in English, the whole flow should remain in English by default
- translation should happen only when explicitly requested

### 2. Hook Assembly Model Is Not the Desired One

Expected production model:

1. select the hook
2. cut the hook as a separate asset
3. prepend the hook at the start of the video
4. after the hook, play the full main cut from its original start
5. allow the hook to repeat naturally if it is part of the chosen main cut

Current issue:

- the renderer currently resumes the first cut after the hook excerpt
- this makes the hook behave like a teaser instead of a discrete opening asset

### 3. Subtitles Are Not Trustworthy Enough

Current issue:

- subtitles are still built from segment-level timing
- word/chunk timing is inferred heuristically
- styling is now acceptable enough for MVP iteration, but timing still lacks production reliability

Production expectation:

- subtitle timing must come from real alignment data
- subtitle language must follow source language by default
- styling should be configurable per tenant or brand

### 4. LLM Still Has Too Much Freedom Over Timestamps

Current issue:

- the LLM returns raw cuts and hook timestamps
- the worker then tries to repair or reconcile those decisions
- this is useful for experimentation, but fragile at scale

Production expectation:

- the pipeline should structure safe candidate spans first
- the LLM should select from those spans, not invent timestamps freely

### 5. SaaS Observability Is Still Too Thin

Current issue:

- when a hook, subtitle or cut goes wrong, we still infer the root cause from artifacts manually
- pipeline state is not broken into enough explicit intermediate contracts

Production expectation:

- every important editorial decision should be traceable
- every render should be explainable
- every production issue should be debuggable from stored artifacts

### 6. Clip Mode Fidelity Is Too Weak

Current issue:

- `--long` still inherits too much behavior from short-form logic
- prompt, candidate selection and duration targets still bias the flow toward shorts
- long excerpts should preserve more setup, more context and less aggressive compression

Production expectation:

- `short`, `short_serie` and `long` must behave like materially different editorial modes
- `long` must use larger context windows, larger cuts and more tolerant continuity
- the prompt should describe `long` as normal-video excerpt editing, not as short-form editing

## V3 Principles

### Preserve Source Truth

The transcript is the canonical source.

- hooks must be anchored to transcript spans
- subtitles must be anchored to real aligned timings
- cuts must be anchored to safe boundaries derived from transcript structure

### Use LLM for Selection, Not Freehand Timing

The LLM should decide:

- which narrative spans are strongest
- which hook is strongest
- which spans connect naturally
- which final videos are worth producing

The LLM should not be responsible for:

- arbitrary timestamp invention
- subtitle timing
- low-level render sequencing

### Prefer Explicit Contracts Between Stages

V3 should be decomposed into explicit machine-readable contracts:

1. `transcript`
2. `language_detection`
3. `span_catalog`
4. `hook_candidates`
5. `assembly_plan`
6. `render_plan`
7. `delivery_package`

### Respect Clip Mode As A First-Class Product Parameter

The pipeline should treat clip mode as a fundamental editorial setting, not a light suggestion.

At minimum, clip mode must influence:

- transcript context size
- candidate duration
- target cut duration
- minimum final duration
- continuity rules
- prompt language and instructions

### Preserve Working V2 Behavior While Replacing It

We should replace risky pieces incrementally:

- no big-bang rewrite
- keep V2 available as fallback
- gate V3 features behind flags until stable

## Proposed V3 Architecture

### Stage 1. Ingest And Source Analysis

Inputs:

- video URL or uploaded media

Outputs:

- normalized source media
- source metadata
- detected source language
- optional audio quality diagnostics

New artifact:

- `language_detection.json`

Suggested fields:

- `detected_language`
- `language_confidence`
- `source_language_mode`
- `output_language`
- `subtitle_language`

### Stage 2. Transcript And Speaker Structure

Outputs:

- transcript with speaker attribution
- normalized sentence spans
- paragraph/topic-like groupings

New artifact:

- `span_catalog.json`

Suggested fields per span:

- `span_id`
- `start`
- `end`
- `speaker`
- `text`
- `sentence_count`
- `clean_start`
- `clean_end`
- `continuation_dependency`
- `closure_score`
- `hook_score`
- `topic_signature`
- `language`

### Stage 3. Hook Candidate Generation

The system should derive hook candidates from transcript spans, not from arbitrary text generation.

New artifact:

- `hook_candidates.json`

Suggested fields:

- `hook_id`
- `span_id`
- `start`
- `end`
- `text`
- `duration`
- `speaker`
- `hook_strength_score`
- `clarity_score`
- `isolation_score`
- `continuation_target_span_ids`

### Stage 4. Narrative Assembly Planning

The LLM should choose from structured spans and hooks.

New artifact:

- `assembly_plan.json`

Suggested fields:

- `video_index`
- `source_language`
- `hook_id`
- `hook_span_id`
- `main_sequence_span_ids`
- `assembly_mode`
- `why_this_hook`
- `why_this_sequence`
- `expected_duration`
- `closure_confidence`

Important:

- the LLM should return span IDs
- the pipeline should compute the final timestamps from the selected spans

### Stage 5. Render Plan

The render plan should explicitly model the production assembly behavior.

Required behavior for V3:

- `hook_clip` is a separate opening asset
- `main_cut_1` starts from its original selected start
- hook repetition is allowed if the hook belongs to `main_cut_1`

New render composition model:

- `opening_sequence = [hook_clip, main_cut_1, main_cut_2, ...]`

Instead of:

- `opening_sequence = [hook_excerpt, resumed_main_cut_after_excerpt, ...]`

## Language Policy For Production

### Default Policy

- detect source language automatically
- preserve source language by default
- do not translate automatically

### Explicit Modes

Add a new parameter family for V3:

- `language_mode=auto`
- `language_mode=source`
- `language_mode=force:en`
- `language_mode=force:pt`
- `language_mode=translate:pt`
- `language_mode=translate:en`

Recommended internal representation:

- `source_language`
- `output_language`
- `subtitle_language`

Default behavior:

- `source_language = detected`
- `output_language = source_language`
- `subtitle_language = source_language`

## Subtitle Strategy For V3

### Immediate Goal

Make subtitle timing trustworthy.

### Recommended Technical Direction

Move from segment-based approximation to real alignment.

Options:

- faster-whisper with reliable word timestamps
- forced alignment on top of transcript
- a dedicated alignment stage after ASR

### Production Requirements

- subtitle events tied to true word or phrase timings
- source-language subtitles by default
- style presets by brand or tenant
- deterministic positioning and sizing

Suggested subtitle style presets:

- `card_white_black`
- `minimal_clean`
- `karaoke_focus`
- `brand_custom`

## Hook Strategy For V3

### Rules

- hook must be selected from real transcript spans
- hook must start at the first word of the intended phrase
- hook must end at the last word of the intended phrase
- hook should have an isolation score high enough to make sense alone
- hook should point to a main sequence that fully supports it

### Separate Hook Selection From Narrative Selection

Instead of one LLM response doing everything at once, split the logic:

1. pick hook candidates
2. pick best hook
3. pick main sequence that justifies that hook
4. validate closure and continuity

This will reduce:

- drift
- rework in the worker
- brittle post-hoc repairs

## Cut Intelligence Improvements

### Current Goal

Cuts should be more precise and less dependent on manual repair.

### V3 Direction

Each span should carry editorial signals such as:

- hook potential
- closure potential
- continuation dependency
- interruption risk
- redundancy risk
- speaker continuity confidence

This allows the pipeline to reason about:

- whether a cut can stand alone
- whether it must be preceded by context
- whether it properly closes the topic

### Penalties To Add

- starts with connective
- ends with unresolved question
- depends heavily on previous context
- jumps topic without bridge
- cuts speaker mid-thought
- duplicates narrative beat used by another final video

## Prompt Strategy For V3

The current prompt should be treated as a V2-compatible editorial layer.

For V3:

- the prompt should be shorter
- the transcript should still be available as context
- but the primary selection mechanism should rely on structured spans

The LLM should receive:

- transcript excerpts
- span catalog summary
- hook candidates
- explicit selection rules

The LLM should return:

- `hook_id`
- `span_ids`
- `post metadata`

It should not return:

- fully free-form timestamps

## SaaS Readiness Improvements

### Versioning

Every job should store:

- `pipeline_version`
- `prompt_version`
- `assembly_version`
- `subtitle_version`
- `render_version`

### Tenant Configuration

Support tenant-level defaults for:

- language policy
- subtitle style
- target duration
- hook aggressiveness
- soundtrack policy
- visual filter profile

### Observability

Store these artifacts for every job:

- `language_detection.json`
- `span_catalog.json`
- `hook_candidates.json`
- `assembly_plan.json`
- `render_plan.json`
- `delivery_package.json`

### Reviewability

The Studio and API should eventually expose:

- why a hook was selected
- which spans were used
- which spans were rejected
- whether translation happened
- whether subtitles are source-language or translated

## Execution Plan

### Phase 1. Stabilize Language And Contracts

Scope:

- implement source language detection
- preserve original language by default
- add explicit translation mode
- persist language metadata

Deliverables:

- `language_detection.json`
- `source_language`, `output_language`, `subtitle_language` in delivery artifacts

### Phase 2. Hook As Separate Asset

Scope:

- stop resuming the first cut after the teaser
- prepend hook as its own clip
- replay the full first cut after hook

Deliverables:

- new render plan structure
- deterministic hook replay behavior

### Phase 3. Span Catalog And Structured LLM Selection

Scope:

- generate `span_catalog.json`
- generate `hook_candidates.json`
- change prompt contract from timestamps to IDs

Deliverables:

- LLM returns span references instead of arbitrary time windows

### Phase 4. Real Subtitle Alignment

Scope:

- add word timestamps or forced alignment
- rebuild subtitle generation around aligned timings

Deliverables:

- subtitle timing that follows speech reliably

### Phase 5. SaaS Observability And Tenant Controls

Scope:

- version all key stages
- expose intermediate artifacts in API/Studio
- add tenant-level defaults

Deliverables:

- a more explainable and configurable SaaS pipeline

## Acceptance Criteria For V3

### Language

- English input stays in English by default
- no translation occurs unless requested
- subtitles follow output language policy

### Hooks

- hook starts at the exact first intended word
- hook is rendered as a separate opening clip
- first main cut plays fully after the hook, even if repetition happens

### Cuts

- cuts have cleaner starts and endings
- cuts close the subject more reliably
- continuity is explainable and traceable

### Subtitles

- subtitle timing follows spoken audio closely
- source-language subtitles work by default
- style is consistent and configurable

### SaaS

- jobs are diagnosable from stored artifacts
- configuration can vary safely by tenant
- behavior is versioned and auditable

## Immediate Recommendation

Implement V3 in this order:

1. language policy
2. hook-as-separate-asset
3. span catalog
4. subtitle alignment
5. SaaS observability

This order gives the best balance of:

- product impact
- technical clarity
- low regression risk
- easier rollout from the current working V2 baseline

"""Resolving what gets sent to YouTube, and refusing to send something wrong.

Two jobs, both deliberately outside the provider adapter.

**Precedence.** A title can come from four places. The order is fixed and recorded on every
attempt, so "why did it get that title" is answered by the row rather than by reading code:

    explicit request  >  publish_package.json  >  target defaults  >  system defaults

**Validation, not silent repair.** The editorial metadata was written by an upstream stage a
person may have reviewed. Quietly truncating it to fit an API limit changes what was approved
without telling anyone. So a value that does not fit is a *blocked publication* by default,
and normalisation happens only where it is deterministic and lossless (stripping the angle
brackets YouTube rejects outright, collapsing whitespace, dropping tags past the limit).

The one place this bends: a description over the limit is truncated, because descriptions
carry hashtags and attribution at the end and a hard block on a 5001-character description
would strand a perfectly good video. It is truncated at a word boundary, the fact is recorded
in the snapshot, and the attempt says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.publishing.contracts import PublishMetadata

# YouTube Data API limits.
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS_TOTAL_CHARS = 500
MAX_TAG_CHARS = 30

# The API rejects these outright in titles and descriptions.
FORBIDDEN_CHARS = ("<", ">")

VALID_PRIVACY = frozenset({"private", "unlisted", "public"})

# Safe because it is the least public option the API offers. A default of `public` would mean
# a mistyped request publishes to the channel's subscribers.
DEFAULT_PRIVACY = "private"


class MetadataValidationError(ValueError):
    """The metadata cannot be published as-is. Carries every problem, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class ResolvedMetadata:
    metadata: PublishMetadata
    # Where each field came from, frozen onto the attempt alongside the values.
    sources: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_snapshot(self) -> dict[str, Any]:
        return {
            **self.metadata.as_dict(),
            "sources": self.sources,
            "notes": self.notes,
        }


def resolve(
    *,
    video: dict[str, Any] | None,
    package: dict[str, Any] | None,
    target_config: dict[str, Any] | None,
    overrides: dict[str, Any] | None,
) -> ResolvedMetadata:
    """Apply the precedence chain, then validate the result."""
    video = video or {}
    package = package or {}
    target_config = target_config or {}
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}

    post = video.get("post") or {}
    sources: dict[str, str] = {}
    notes: list[str] = []

    title_raw, title_source = _pick(
        "title",
        overrides.get("title"),
        post.get("title") or package.get("primary_title"),
        target_config.get("default_title"),
        None,
    )
    description_raw, description_source = _pick(
        "description",
        overrides.get("description"),
        _package_description(post, package),
        target_config.get("default_description"),
        "",
    )
    tags_raw, tags_source = _pick(
        "tags",
        overrides.get("tags"),
        _hashtags_to_tags(post.get("hashtags") or package.get("hashtags")),
        target_config.get("default_tags"),
        [],
    )
    privacy, privacy_source = _pick(
        "privacy",
        overrides.get("privacy"),
        None,  # publish_package.json carries editorial text, never a distribution decision
        target_config.get("default_privacy"),
        DEFAULT_PRIVACY,
    )
    category_id, category_source = _pick(
        "category_id",
        overrides.get("category_id"),
        None,
        target_config.get("default_category_id"),
        # Omitted rather than guessed. There is no YouTube category that means "football
        # highlights", and picking one at random mis-files every video on the channel.
        None,
    )
    language, language_source = _pick(
        "language",
        overrides.get("language"),
        _package_language(package),
        target_config.get("default_language"),
        None,
    )
    made_for_kids, kids_source = _pick(
        "made_for_kids",
        overrides.get("made_for_kids"),
        None,  # never inferred from the content
        target_config.get("made_for_kids"),
        False,
    )

    sources.update(
        {
            "title": title_source,
            "description": description_source,
            "tags": tags_source,
            "privacy": privacy_source,
            "category_id": category_source,
            "language": language_source,
            "made_for_kids": kids_source,
        }
    )

    problems: list[str] = []

    title = _normalise_text(title_raw or "")
    if not title:
        problems.append("title_missing")
    elif len(title) > MAX_TITLE_CHARS:
        # Blocked, not truncated: a title is the one field a person definitely wrote to be
        # read whole, and cutting it mid-word is a visible editorial change.
        problems.append(
            f"title_too_long ({len(title)} > {MAX_TITLE_CHARS} chars)"
        )

    description = _normalise_text(description_raw or "", collapse_newlines=False)
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = _truncate_on_word(description, MAX_DESCRIPTION_CHARS)
        notes.append("description_truncated_to_api_limit")

    tags = _clean_tags(tags_raw or [])
    if tags != list(tags_raw or []):
        if len(tags) < len(tags_raw or []):
            notes.append("tags_dropped_to_fit_api_limit")

    privacy = str(privacy or DEFAULT_PRIVACY).strip().lower()
    if privacy not in VALID_PRIVACY:
        problems.append(f"privacy_invalid ({privacy!r})")

    if category_id is not None and not str(category_id).strip().isdigit():
        # YouTube categories are numeric ids; a name like "Sports" is silently ignored by the
        # API, which would file every video under the channel default without saying so.
        problems.append(f"category_id_invalid ({category_id!r}, expected a numeric id)")

    if problems:
        raise MetadataValidationError(problems)

    return ResolvedMetadata(
        metadata=PublishMetadata(
            title=title,
            description=description,
            tags=tags,
            privacy=privacy,
            category_id=str(category_id).strip() if category_id is not None else None,
            language=(str(language).strip() or None) if language else None,
            made_for_kids=bool(made_for_kids),
        ),
        sources=sources,
        notes=notes,
    )


# ------------------------------------------------------------------------ helpers


def _pick(
    name: str, request_value: Any, package_value: Any, target_value: Any, system_value: Any
) -> tuple[Any, str]:
    """The precedence chain, returning the value and which tier supplied it."""
    for value, source in (
        (request_value, "request"),
        (package_value, "publish_package"),
        (target_value, "target_default"),
        (system_value, "system_default"),
    ):
        if value is not None and value != "":
            return value, source
    return system_value, "system_default"


def _package_description(post: dict[str, Any], package: dict[str, Any]) -> str | None:
    """The description plus its hashtags, which is how the package presents a caption.

    Hashtags are preserved rather than stripped: they are part of the editorial output and
    YouTube treats the first few in a description as the video's displayed tags.
    """
    description = str(post.get("description") or package.get("description") or "").strip()
    hashtags = post.get("hashtags") or package.get("hashtags") or []
    tail = " ".join(_as_hashtag(tag) for tag in hashtags if str(tag).strip())
    if description and tail:
        return f"{description}\n\n{tail}"
    return description or tail or None


def _package_language(package: dict[str, Any]) -> str | None:
    language = package.get("language") or {}
    if isinstance(language, dict):
        value = language.get("language") or language.get("code") or language.get("detected")
        return str(value).strip() if value else None
    return str(language).strip() or None


def _hashtags_to_tags(hashtags: Any) -> list[str] | None:
    if not hashtags:
        return None
    return [str(tag).lstrip("#").strip() for tag in hashtags if str(tag).strip()]


def _as_hashtag(tag: Any) -> str:
    text = str(tag).strip()
    return text if text.startswith("#") else f"#{text}"


def _normalise_text(value: str, *, collapse_newlines: bool = True) -> str:
    """Deterministic and lossless: strip characters the API rejects, tidy whitespace."""
    text = str(value)
    for char in FORBIDDEN_CHARS:
        text = text.replace(char, "")
    if collapse_newlines:
        text = re.sub(r"\s+", " ", text)
    else:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate_on_word(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    # Only respect the word boundary if it is not throwing away most of the text.
    return (cut[:boundary] if boundary > limit * 0.8 else cut).rstrip()


def _clean_tags(tags: list[Any]) -> list[str]:
    """Drop what the API would reject, and stop at the total-length budget."""
    cleaned: list[str] = []
    total = 0
    for raw in tags:
        tag = str(raw).lstrip("#").strip()
        if not tag or len(tag) > MAX_TAG_CHARS:
            continue
        # YouTube counts a quoted tag's quotes toward the budget when it contains a space.
        cost = len(tag) + (2 if " " in tag else 0) + (1 if cleaned else 0)
        if total + cost > MAX_TAGS_TOTAL_CHARS:
            break
        cleaned.append(tag)
        total += cost
    return cleaned

import re
from typing import Dict, List


HOOK_PATTERNS = [
    r"\bnunca\b",
    r"\bningu[eé]m\b",
    r"\bo problema\b",
    r"\bo segredo\b",
    r"\bpreste aten[cç][aã]o\b",
    r"\bisso muda tudo\b",
    r"\bmas tem\b",
    r"\bquase ninguém\b",
    r"\byou know\b",
    r"\bthe problem\b",
    r"\bthe truth\b",
    r"\bpay attention\b",
]

CONTINUATION_PATTERNS = [
    r"^\be\b",
    r"^\bmas\b",
    r"^\bent[aã]o\b",
    r"^\bporque\b",
    r"^\bpor isso\b",
    r"^\bso\b",
    r"^\bbut\b",
    r"^\bthen\b",
    r"^\bbecause\b",
]

CLOSURE_PATTERNS = [
    r"[.!?]\s*$",
    r"\bpor isso\b",
    r"\bno final\b",
    r"\ba verdade\b",
    r"\bou seja\b",
    r"\bthat's why\b",
    r"\bin the end\b",
    r"\bthe truth is\b",
]


class SpanCatalogBuilder:

    def build(self, transcript_segments: List[Dict], source_language: str | None = None) -> List[Dict]:
        spans: List[Dict] = []

        for index, segment in enumerate(transcript_segments, start=1):
            text = str(segment.get("text") or "").strip()
            if not text:
                continue

            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", 0.0))
            duration = max(0.0, end - start)
            lowered = text.lower()

            spans.append(
                {
                    "span_id": f"span_{index:04d}",
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "duration": round(duration, 2),
                    "speaker": str(segment.get("speaker") or "UNKNOWN"),
                    "text": text,
                    "sentence_count": self._sentence_count(text),
                    "clean_start": not self._has_continuation_dependency(lowered),
                    "clean_end": self._has_closure(lowered),
                    "continuation_dependency": self._has_continuation_dependency(lowered),
                    "closure_score": self._closure_score(lowered),
                    "hook_score": self._hook_score(lowered),
                    "topic_signature": self._topic_signature(text),
                    "language": source_language,
                }
            )

        return spans

    def build_hook_candidates(self, spans: List[Dict], max_candidates: int = 24) -> List[Dict]:
        ranked = sorted(
            (
                span for span in spans
                if float(span.get("hook_score", 0.0)) > 0.0
            ),
            key=lambda item: (
                float(item.get("hook_score", 0.0)),
                float(item.get("closure_score", 0.0)),
                -float(item.get("duration", 0.0)),
            ),
            reverse=True,
        )

        hook_candidates: List[Dict] = []
        for index, span in enumerate(ranked[:max_candidates], start=1):
            hook_candidates.append(
                {
                    "hook_id": f"hook_{index:04d}",
                    "span_id": span.get("span_id"),
                    "start": span.get("start"),
                    "end": span.get("end"),
                    "duration": span.get("duration"),
                    "speaker": span.get("speaker"),
                    "text": span.get("text"),
                    "hook_strength_score": span.get("hook_score"),
                    "clarity_score": 1.0 if span.get("clean_start") else 0.5,
                    "closure_score": span.get("closure_score"),
                    "topic_signature": span.get("topic_signature"),
                    "language": span.get("language"),
                }
            )
        return hook_candidates

    def _sentence_count(self, text: str) -> int:
        count = len(re.findall(r"[.!?]+", text))
        return max(1, count)

    def _has_continuation_dependency(self, lowered: str) -> bool:
        return any(re.search(pattern, lowered) for pattern in CONTINUATION_PATTERNS)

    def _has_closure(self, lowered: str) -> bool:
        return any(re.search(pattern, lowered) for pattern in CLOSURE_PATTERNS)

    def _closure_score(self, lowered: str) -> float:
        score = 0.0
        if self._has_closure(lowered):
            score += 2.0
        if re.search(r"\bpor que\b|\bporque\b|\bthat's why\b|\bbecause\b", lowered):
            score += 0.5
        return round(score, 2)

    def _hook_score(self, lowered: str) -> float:
        score = 0.0
        for pattern in HOOK_PATTERNS:
            if re.search(pattern, lowered):
                score += 2.0
        if "?" in lowered:
            score += 1.5
        if re.search(r"\bmas\b|\bbut\b|\bhowever\b|\bpor[eé]m\b", lowered):
            score += 0.8
        return round(score, 2)

    def _topic_signature(self, text: str) -> List[str]:
        tokens = [
            token
            for token in re.findall(r"\w+", text.lower())
            if len(token) > 3
        ]
        unique_tokens: List[str] = []
        seen = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            unique_tokens.append(token)
            if len(unique_tokens) >= 6:
                break
        return unique_tokens

from pathlib import Path
from typing import Dict, List


class SoundtrackSelector:

    def __init__(self):
        self.soundtrack_dir = Path(__file__).resolve().parents[2] / "assets" / "soundtracks"

    def select(
        self,
        *,
        cuts: List[Dict],
        post_payload: Dict | None = None,
    ) -> Dict:
        theme = self._detect_theme(cuts, post_payload)
        candidates = [
            self.soundtrack_dir / f"{theme}_bed.mp3",
            self.soundtrack_dir / f"{theme}.mp3",
            self.soundtrack_dir / "generic_bed.mp3",
            self.soundtrack_dir / "generic.mp3",
        ]

        for candidate in candidates:
            if candidate.exists():
                return {
                    "status": "selected",
                    "theme": theme,
                    "file_name": candidate.name,
                    "local_path": str(candidate),
                    "mix_volume": 0.12,
                    "ducking": "voice_priority",
                }

        return {
            "status": "unavailable",
            "theme": theme,
            "file_name": None,
            "local_path": None,
            "mix_volume": 0.0,
            "ducking": "voice_priority",
        }

    SUPPORTED_THEMES = {
        "finance_tension",
        "mystery_tension",
        "political_tension",
        "generic",
    }

    def _detect_theme(self, cuts: List[Dict], post_payload: Dict | None) -> str:
        """Honour the model's own suggestion; otherwise use the neutral bed.

        This used to keyword-match the cut text against literals like "blackrock",
        "deep state", "trump" and "wall street" — a topic map from an old geopolitics job.
        On football content none of them fire, so the branch was dead weight that could only
        mis-fire (e.g. "poder" → political_tension). Choosing a mood from content is a
        semantic judgement: the response schema already asks the model for
        `soundtrack_suggestion`, so that is the input, and the fallback is neutral rather
        than guessed.
        """
        suggested = str((post_payload or {}).get("soundtrack_suggestion") or "").strip().lower()
        if suggested in self.SUPPORTED_THEMES:
            return suggested
        return "generic"

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

    def _detect_theme(self, cuts: List[Dict], post_payload: Dict | None) -> str:
        suggested = str((post_payload or {}).get("soundtrack_suggestion") or "").strip().lower()
        if suggested in {"finance_tension", "mystery_tension", "political_tension", "generic"}:
            return suggested

        text_parts: List[str] = []
        for cut in cuts:
            text_parts.extend(
                [
                    str(cut.get("title") or ""),
                    str(cut.get("hook") or ""),
                    str(cut.get("description") or ""),
                ]
            )

        if post_payload:
            text_parts.extend(
                [
                    str(post_payload.get("title") or ""),
                    str(post_payload.get("hook") or ""),
                    str(post_payload.get("description") or ""),
                ]
            )

        text = " ".join(text_parts).lower()
        if any(token in text for token in {"blackrock", "dinheiro", "trilhões", "economia", "wall street"}):
            return "finance_tension"
        if any(token in text for token in {"deep state", "controle", "sombras", "por trás"}):
            return "mystery_tension"
        if any(token in text for token in {"trump", "política", "governo", "poder"}):
            return "political_tension"
        return "generic"

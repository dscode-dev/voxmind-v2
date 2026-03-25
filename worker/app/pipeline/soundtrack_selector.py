from pathlib import Path
from typing import Dict, List


class SoundtrackSelector:

    def __init__(self):
        self.soundtrack_dir = Path(__file__).resolve().parents[2] / "assets" / "soundtracks"

    def select(
        self,
        *,
        cuts: List[Dict],
        long_video_script: Dict | None,
    ) -> Dict:
        theme = self._detect_theme(cuts, long_video_script)
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

    def _detect_theme(self, cuts: List[Dict], long_video_script: Dict | None) -> str:
        text_parts: List[str] = []
        for cut in cuts:
            text_parts.extend(
                [
                    str(cut.get("title") or ""),
                    str(cut.get("hook") or ""),
                    str(cut.get("description") or ""),
                ]
            )

        if long_video_script:
            text_parts.extend(
                [
                    str(long_video_script.get("title") or ""),
                    str(long_video_script.get("hook") or ""),
                    str(long_video_script.get("context") or ""),
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

import json
from typing import List, Dict


class ManualPromptBuilder:

    def build(self, transcript: List[Dict], candidates: List[Dict], job_id: str) -> str:

        return f"""
Você é um editor especialista em vídeos virais para YouTube Shorts e TikTok.

IMPORTANTE:
Você DEVE incluir o campo "job_id": "{job_id}" no JSON final.

Baseado na transcrição e candidatos abaixo:

Tarefas:

1. Escolher os 3 melhores cortes.
2. Retornar start e end exatos.
3. Gerar:
   - Título altamente chamativo
   - Descrição envolvente
   - 8 hashtags otimizadas
   - Frase curta para thumbnail

Retorne APENAS JSON válido no formato:

{{
  "job_id": "{job_id}",
  "cuts": [
    {{"start": float, "end": float}}
  ],
  "title": "...",
  "description": "...",
  "hashtags": ["#tag1"],
  "thumbnail_text": "..."
}}

=== TRANSCRIÇÃO ===
{json.dumps(transcript, ensure_ascii=False)}

=== CANDIDATOS ===
{json.dumps(candidates, ensure_ascii=False)}
"""
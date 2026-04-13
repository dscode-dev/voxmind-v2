from __future__ import annotations


class RawEditPromptBuilder:
    def build(
        self,
        *,
        job_id: str,
        transcript: list[dict],
        speaker_turns: list[dict] | None = None,
        language: str = "auto",
        edit_brief: str | None = None,
        video_ratio: str = "landscape",
    ) -> str:
        transcript_text = self._format_transcript(transcript)
        turns_text = self._format_turns(speaker_turns or [])
        brief = edit_brief or "Criar uma versão editada, clara, profissional e pronta para publicação."

        return f"""
JOB_ID: {job_id}

Você é um diretor de edição e roteirista sênior para vídeos autorais.
Sua tarefa NÃO é criar cortes curtos independentes.
Sua tarefa é analisar um vídeo bruto autoral, entender a intenção do conteúdo e criar um plano completo de edição para transformar esse material em um vídeo final coeso.

RETORNO

- Retorne apenas JSON válido.
- Não use markdown.
- Não escreva texto fora do JSON.
- Não use aspas duplas dentro de valores de string, a menos que estejam escapadas.
- Mantenha o idioma original do vídeo, a menos que o briefing peça tradução.

CONFIGURAÇÃO

job_id: {job_id}
workflow: raw_authorial_edit
video_ratio: {video_ratio}
language: {language}

BRIEFING DO USUÁRIO

{brief}

OBJETIVO EDITORIAL

- Transformar o vídeo bruto em um vídeo final com começo, desenvolvimento e fechamento.
- Remover enrolação, pausas longas, repetições, trechos fracos e desvios sem perder a sequência lógica.
- Criar um roteiro de montagem que preserve a voz e a intenção do autor.
- Sugerir estrutura narrativa, capítulos, cortes internos, ritmo, legendas, b-roll, inserts, música e thumbnail.
- Se o vídeo bruto estiver confuso, reorganize a ordem editorial usando timestamps reais.

REGRAS IMPORTANTES

- Não invente falas que não existem no vídeo.
- Você pode sugerir narração adicional apenas em `additional_voiceover_suggestions`.
- Todo trecho usado na montagem precisa ter `source_start` e `source_end`.
- Nunca comece ou termine um bloco no meio de uma frase.
- Prefira uma edição final completa a muitos pedaços soltos.
- O vídeo final pode reorganizar trechos, mas cada transição precisa explicar por que a ordem funciona.
- Marque trechos a remover em `remove_segments`.
- Marque trechos essenciais em `keep_segments`.

OUTPUT JSON FLEXÍVEL

O exemplo abaixo mostra o formato esperado. A quantidade de capítulos, blocos e sugestões deve ser decidida por você conforme o material.

{{
  "job_id": "{job_id}",
  "workflow": "raw_authorial_edit",
  "language": "{language}",
  "video_ratio": "{video_ratio}",
  "post": {{
    "title": "título do vídeo final",
    "description": "descrição para postagem",
    "hashtags": ["#tag1", "#tag2"],
    "thumbnail": "ideia principal de thumbnail",
    "target_platforms": ["youtube", "instagram", "tiktok"],
    "posting_notes": "observações de publicação"
  }},
  "editorial_strategy": {{
    "core_message": "mensagem central do vídeo",
    "target_audience": "público provável",
    "narrative_arc": "como o vídeo deve começar, desenvolver e fechar",
    "retention_strategy": "como manter atenção ao longo do vídeo",
    "final_payoff": "qual conclusão deve ficar no final"
  }},
  "final_video_plan": {{
    "estimated_duration_sec": 180,
    "opening_hook": {{
      "text": "frase ou momento que deve abrir o vídeo",
      "source_start": 12.0,
      "source_end": 24.0,
      "reason": "por que este hook abre melhor"
    }},
    "chapters": [
      {{
        "chapter_index": 1,
        "title": "nome do capítulo",
        "purpose": "função narrativa",
        "source_start": 30.0,
        "source_end": 95.0,
        "edit_notes": "como montar este trecho"
      }}
    ],
    "timeline": [
      {{
        "order": 1,
        "source_start": 12.0,
        "source_end": 24.0,
        "role": "hook | setup | development | proof | payoff | cta",
        "transition_after": "hard_cut | fade | punch_in | b_roll | none",
        "caption_emphasis": "texto curto para destacar na legenda",
        "broll_suggestion": "sugestão de imagem de apoio ou null",
        "reason": "por que este bloco entra aqui"
      }}
    ],
    "remove_segments": [
      {{
        "source_start": 100.0,
        "source_end": 112.0,
        "reason": "pausa longa, repetição, desvio ou trecho fraco"
      }}
    ],
    "keep_segments": [
      {{
        "source_start": 12.0,
        "source_end": 24.0,
        "reason": "momento essencial para retenção"
      }}
    ]
  }},
  "script_rewrite": {{
    "final_script_summary": "resumo do roteiro final montado",
    "chapter_scripts": [
      {{
        "chapter_index": 1,
        "spoken_content_summary": "o que é dito neste capítulo",
        "editing_instruction": "como editar este capítulo"
      }}
    ],
    "additional_voiceover_suggestions": []
  }},
  "style_guide": {{
    "subtitle_style": "descrição da legenda",
    "music_style": "trilha sugerida",
    "color_filter": "filtro visual sugerido",
    "pace": "ritmo da edição",
    "graphics": ["sugestão de cards, setas, títulos ou inserts"]
  }},
  "quality_checks": [
    "o vídeo final fecha o assunto",
    "o hook está no início",
    "não há cortes no meio de frase"
  ]
}}

TRANSCRIÇÃO COMPLETA COM TIMESTAMPS

{transcript_text}

TURNOS DE SPEAKER

{turns_text}

INSTRUÇÃO FINAL

Retorne somente o JSON final.
Decida a duração e a estrutura real com base na transcrição.
Não retorne `shorts_content`, pois este fluxo não é de cortes curtos.
"""

    def _format_transcript(self, transcript: list[dict]) -> str:
        lines = []
        for segment in transcript:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
            speaker = segment.get("speaker") or "UNKNOWN"
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"[{self._time(start)} - {self._time(end)}] {speaker}: {text}")
        return "\n".join(lines)

    def _format_turns(self, turns: list[dict]) -> str:
        lines = []
        for turn in turns[:200]:
            start = float(turn.get("start", 0.0))
            end = float(turn.get("end", start))
            speaker = turn.get("speaker") or "UNKNOWN"
            lines.append(f"[{self._time(start)} - {self._time(end)}] {speaker}")
        return "\n".join(lines) or "Sem turnos de speaker disponíveis."

    def _time(self, value: float) -> str:
        total = max(0, int(value))
        minutes = total // 60
        seconds = total % 60
        return f"{minutes:02d}:{seconds:02d}"

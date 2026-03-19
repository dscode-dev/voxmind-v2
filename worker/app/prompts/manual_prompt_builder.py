from typing import Dict, List

from app.prompts.prompt_context import build_candidate_context, build_transcript_context


class ManualPromptBuilder:

    def __init__(self, max_context_chars: int | None = None):
        if max_context_chars is not None:
            self.max_context_chars = max_context_chars
            return

        from app.settings import settings

        self.max_context_chars = settings.llm_max_chars

    def build(
        self,
        transcript: List[Dict],
        candidates: List[Dict],
        job_id: str,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
    ) -> str:
        transcript_context = build_transcript_context(
            transcript=transcript,
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.72),
        )
        candidate_context = build_candidate_context(
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.28),
        )

        return f"""
JOB_ID: {job_id}

Você é um editor sênior de conteúdo para Shorts, TikTok, Reels e vídeos longos.

Sua função é selecionar os melhores cortes do vídeo preservando integridade narrativa,
continuidade de fala e contexto suficiente para o espectador entender cada trecho.

REGRA CRÍTICA

Retorne APENAS JSON válido.
Não use markdown.
Não escreva explicações fora do JSON.

CONFIGURAÇÃO

job_id: {job_id}
clip_mode: {clip_mode}
video_ratio: {video_ratio}

{self._build_mode_instructions(clip_mode)}

{self._build_ratio_instructions(video_ratio)}

REGRAS NARRATIVAS OBRIGATÓRIAS

- Nunca comece no meio de frase.
- Nunca termine antes do fechamento da ideia.
- Nunca corte no meio do turno de fala de um locutor se isso quebrar o sentido.
- Preserve continuidade entre falas relacionadas.
- Em diálogos, garanta que a troca entre speakers continue compreensível.
- Prefira trechos com setup, desenvolvimento e payoff claros.
- Evite cortes redundantes entre si; cada corte precisa trazer uma ideia distinta.
- Se houver dúvida entre um trecho muito chamativo e um trecho mais completo, prefira o mais completo.
- Você pode ajustar timestamps em aproximadamente ±8 segundos para capturar início natural, desenvolvimento e fechamento.
- Os candidatos são pistas, não limites rígidos.

REGRAS ESPECÍFICAS DE LOCUTOR

- O transcript inclui speaker labels como SPEAKER_01, SPEAKER_02, etc.
- Use esses labels para entender diálogos, mudanças de contexto e continuidade.
- Prefira cortes que respeitem a conclusão do speaker atual.
- Evite trocar de speaker exatamente no início ou no fim do corte sem contexto suficiente.

QUALIDADE DO CORTE

- começo natural
- desenvolvimento claro
- payoff, conclusão, revelação ou fechamento
- contexto suficiente para funcionar no modo solicitado
- retenção alta nos primeiros segundos
- coerência editorial entre hook, title, description e thumbnail
- diversidade entre os cortes selecionados

OUTPUT JSON

{{
  "job_id": "{job_id}",
  "clip_mode": "{clip_mode}",
  "video_ratio": "{video_ratio}",
  "shorts_content": [
    {{
      "start": 10.5,
      "end": 45.3,
      "hook": "frase forte do início do corte",
      "reason": "por que esse trecho respeita narrativa, speaker continuity e clip_mode",
      "narrative_role": "hook | setup | development | payoff",
      "title": "título curto e impactante",
      "description": "descrição curta",
      "hashtags": ["#tag1", "#tag2", "#tag3"],
      "thumbnail": "ideia de thumbnail",
      "merge_group": "story_1"
    }}
  ],
  "long_video_script": {{
    "title": "título sugerido",
    "hook": "gancho inicial do vídeo",
    "context": "contextualização clara do tema",
    "development": "desenvolvimento narrativo principal",
    "twist": "ponto de virada ou revelação",
    "conclusion": "conclusão reflexiva",
    "narration_style": "ritmo e tom da narração"
  }}
}}

TRANSCRIPT RELEVANTE COM SPEAKERS

{transcript_context}

CANDIDATOS PRIORIZADOS

{candidate_context}

INSTRUÇÃO FINAL

Retorne apenas o JSON final.
"""

    def _build_mode_instructions(self, clip_mode: str) -> str:
        if clip_mode == "short":
            return """
MODO: SHORT

- Gere cortes independentes.
- Cada corte precisa funcionar sozinho.
- Cada corte deve ter início, meio e fim.
- Prefira 30 a 60 segundos, mas preserve o fechamento se precisar expandir.
- merge_group deve ser null na maioria dos casos.
"""

        if clip_mode == "long":
            return """
MODO: LONG

- Gere blocos que possam compor uma narrativa maior.
- Os cortes podem depender da continuidade do bloco anterior.
- Preserve ordem cronológica e fluidez entre trechos.
- Use merge_group quando vários cortes fizerem parte da mesma história.
"""

        return """
MODO: SHORT_SERIE

- Gere cortes conectados que formem uma sequência coesa.
- Os cortes podem depender do anterior, mas juntos precisam fechar a ideia.
- Todos os trechos relacionados devem compartilhar merge_group.
- Pense em setup, desenvolvimento e payoff.
"""

    def _build_ratio_instructions(self, video_ratio: str) -> str:
        if video_ratio == "landscape":
            return """
FORMATO: LANDSCAPE

- Considere um vídeo horizontal com mais espaço de contexto visual.
- Você pode aceitar trechos ligeiramente mais contemplativos se a narrativa compensar.
"""

        return """
FORMATO: PORTRAIT

- Considere um vídeo vertical com alta exigência de retenção rápida.
- Prefira trechos mais diretos, claros e fortes logo no início.
"""

from typing import Dict, List

from app.prompts.prompt_context import (
    build_candidate_context,
    build_candidate_neighborhood_context,
    build_timeline_context,
    build_transcript_context,
)


class ManualPromptBuilder:

    def __init__(self, max_context_chars: int | None = None):
        from app.settings import settings

        self.prompt_max_candidates = settings.prompt_max_candidates
        self.prompt_max_segments_per_candidate = settings.prompt_max_segments_per_candidate
        self.render_min_clip_duration_sec = settings.render_min_clip_duration_sec
        self.qa_max_clip_duration_sec = settings.qa_max_clip_duration_sec

        if max_context_chars is not None:
            self.max_context_chars = max_context_chars
            return

        self.max_context_chars = settings.llm_max_chars

    def build(
        self,
        transcript: List[Dict],
        candidates: List[Dict],
        job_id: str,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
    ) -> str:
        has_named_speakers = any(
            (segment.get("speaker") or "").strip().upper() not in {"", "UNKNOWN"}
            for segment in transcript
        )
        transcript_context = build_transcript_context(
            transcript=transcript,
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.5),
            max_candidates=self.prompt_max_candidates,
            max_segments_per_candidate=self.prompt_max_segments_per_candidate,
        )
        timeline_context = build_timeline_context(
            transcript=transcript,
            max_chars=int(self.max_context_chars * 0.2),
        )
        neighborhood_context = build_candidate_neighborhood_context(
            transcript=transcript,
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.16),
            max_candidates=self.prompt_max_candidates,
        )
        candidate_context = build_candidate_context(
            candidates=candidates,
            max_chars=int(self.max_context_chars * 0.14),
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

{self._build_duration_rules(clip_mode, video_ratio)}

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
- Os candidatos são pistas fortes e prioritárias, não limites rígidos.
- Dê atenção especial aos candidatos marcados com `source = clipsai`, porque eles representam blocos narrativos detectados automaticamente no transcript.
- Se o melhor corte estiver fora dos candidatos priorizados, siga a narrativa e escolha assim mesmo.
- Pense que os cortes serão montados em um único vídeo final, não publicados isoladamente.
- Preserve ponte de contexto entre cortes consecutivos da mesma história.
- Prefira uma sequência cronológica coesa dentro do mesmo arco narrativo.
- Evite saltos grandes de tempo entre cortes, a menos que isso seja indispensável para o payoff final.
- O último corte deve fechar o assunto com uma conclusão clara, payoff ou fechamento verbal forte.

{self._build_speaker_guidance(has_named_speakers)}

QUALIDADE DO CORTE

- começo natural
- desenvolvimento claro
- payoff, conclusão, revelação ou fechamento
- contexto suficiente para funcionar no modo solicitado
- retenção alta nos primeiros segundos
- coerência editorial entre hook, title, description e thumbnail
- diversidade entre os cortes selecionados
- evite títulos genéricos sem sujeito concreto ou sem tese clara
- prefira títulos e hooks que mencionem explicitamente o personagem, instituição ou conflito principal
- quando possível, use uma frase forte real do transcript como base do hook

OUTPUT JSON

{{
  "job_id": "{job_id}",
  "clip_mode": "{clip_mode}",
  "video_ratio": "{video_ratio}",
  "story_map": {{
    "core_topic": "assunto central do vídeo",
    "central_conflict": "qual tensão ou pergunta move a narrativa",
    "hook_strategy": "por que o hook escolhido é o melhor para abrir o vídeo final",
    "sequence_logic": [
      "como o corte 1 prepara o terreno",
      "como o corte 2 desenvolve a ideia",
      "como o corte final fecha o assunto"
    ],
    "final_payoff": "qual frase, revelação ou conclusão deve encerrar o vídeo"
  }},
  "post": {{
    "title": "título principal do vídeo final",
    "hook": "gancho principal do vídeo final; será usado na abertura teaser",
    "description": "descrição final para postagem",
    "hashtags": ["#tag1", "#tag2", "#tag3"],
    "thumbnail": "ideia de thumbnail principal",
    "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null"
  }},
  "shorts_content": [
    {{
      "start": 10.5,
      "end": 45.3,
      "safe_start": 10.5,
      "safe_end": 45.3,
      "reason": "por que esse trecho respeita narrativa, speaker continuity e clip_mode",
      "narrative_role": "hook | setup | development | payoff",
      "merge_group": "story_1",
      "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
      "transition_after": "hard_cut | punch_in | whoosh | fade | none"
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

TIMELINE GERAL DO VÍDEO

{timeline_context}

VIZINHANÇA DOS CANDIDATOS

{neighborhood_context}

CANDIDATOS PRIORIZADOS

{candidate_context}

INSTRUÇÃO FINAL

Retorne apenas o JSON final.
Use `story_map` para mostrar que você entendeu o arco do vídeo inteiro antes de escolher os cortes.
Use transcript, timeline, candidatos heurísticos e candidatos do ClipsAI como contexto forte para decidir com autonomia.
Os campos de social media devem existir apenas em "post", não repetidos dentro de cada corte.
Se algum campo novo não se aplicar, retorne null, string vazia ou lista vazia.
"""

    def _build_speaker_guidance(self, has_named_speakers: bool) -> str:
        if not has_named_speakers:
            return """
REGRAS SOBRE LOCUTOR

- O transcript não tem labels de speaker confiáveis neste job.
- Não invente trocas de locutor.
- Baseie a continuidade principalmente em frases completas, contexto e fechamento da ideia.
"""

        return """
REGRAS ESPECÍFICAS DE LOCUTOR

- O transcript inclui speaker labels como SPEAKER_01, SPEAKER_02, etc.
- Use esses labels para entender diálogos, mudanças de contexto e continuidade.
- Prefira cortes que respeitem a conclusão do speaker atual.
- Evite trocar de speaker exatamente no início ou no fim do corte sem contexto suficiente.
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

    def _build_duration_rules(self, clip_mode: str, video_ratio: str) -> str:
        min_duration = self.render_min_clip_duration_sec
        max_duration = self.qa_max_clip_duration_sec

        if clip_mode == "short_serie" and video_ratio == "portrait":
            return f"""
REGRAS DE DURAÇÃO

- Cada corte deve ter no mínimo {min_duration} segundos.
- Para este modo, prefira cortes entre 26 e 35 segundos.
- Não retorne cortes com menos de {min_duration} segundos.
- Só ultrapasse 35 segundos se isso for necessário para fechar a ideia.
- Nunca exceda {max_duration} segundos.
"""

        if clip_mode == "short":
            return f"""
REGRAS DE DURAÇÃO

- Cada corte deve ter no mínimo {min_duration} segundos.
- Prefira cortes entre 28 e 45 segundos.
- Nunca exceda {max_duration} segundos.
"""

        return f"""
REGRAS DE DURAÇÃO

- Cada corte deve ter no mínimo {min_duration} segundos.
- Nunca exceda {max_duration} segundos.
"""

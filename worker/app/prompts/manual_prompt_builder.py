from typing import Dict, List

from app.prompts.prompt_context import build_transcript_context


class ManualPromptBuilder:

    def __init__(self, max_context_chars: int | None = None):
        from app.settings import settings

        self.prompt_max_candidates = settings.prompt_max_candidates
        self.prompt_max_segments_per_candidate = settings.prompt_max_segments_per_candidate
        self.prompt_long_max_segments_per_candidate = settings.prompt_long_max_segments_per_candidate
        self.render_min_clip_duration_sec = settings.render_min_clip_duration_sec
        self.qa_max_clip_duration_sec = settings.qa_max_clip_duration_sec
        self.render_min_long_video_duration_sec = settings.render_min_long_video_duration_sec

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
        content_language: str = "pt",
    ) -> str:
        content_language = (content_language or "pt").strip().lower()
        has_named_speakers = any(
            (segment.get("speaker") or "").strip().upper() not in {"", "UNKNOWN"}
            for segment in transcript
        )
        transcript_context = build_transcript_context(
            transcript=transcript,
            candidates=[],
            max_chars=int(self.max_context_chars * 0.80),
            max_candidates=self.prompt_max_candidates,
            max_segments_per_candidate=(
                self.prompt_long_max_segments_per_candidate
                if clip_mode == "long"
                else self.prompt_max_segments_per_candidate
            ),
            context_padding_sec=48 if clip_mode == "long" else 32,
            min_total_segments=42 if clip_mode == "long" else 28,
        )

        if content_language.startswith("en"):
            return self._build_english_prompt(
                transcript_context=transcript_context,
                job_id=job_id,
                clip_mode=clip_mode,
                video_ratio=video_ratio,
                has_named_speakers=has_named_speakers,
            )

        return f"""
JOB_ID: {job_id}

Você é um editor sênior de vídeo.
Sua tarefa é analisar as transcrições e escolher os melhores pedaços para montar vídeos com alto potencial de retenção.
Os pedaços escolhidos precisam pertencer ao mesmo contexto ou se conectar de forma natural.
Em cada vídeo final, você também deve escolher um trecho específico que funcione como hook e chame a atenção do usuário nos primeiros segundos.

RETORNO

- Retorne apenas JSON válido.
- Não use markdown.
- Não escreva texto fora do JSON.
- Não use aspas duplas dentro de valores de string, a menos que estejam escapadas.

CONFIGURAÇÃO

job_id: {job_id}
clip_mode: {clip_mode}
video_ratio: {video_ratio}

{self._build_mode_instructions(clip_mode)}

{self._build_ratio_instructions(video_ratio)}

{self._build_duration_rules(clip_mode, video_ratio)}

O QUE VOCÊ DEVE FAZER

- Escolha os melhores trechos do transcript para montar até 3 vídeos finais.
- Cada vídeo final deve contar uma ideia clara do começo ao fim.
- Os cortes de um mesmo vídeo final devem compartilhar o mesmo contexto ou se conectar de forma natural.
- Cada vídeo final deve ter um hook forte no começo, desenvolvimento claro e fechamento real.
- Prefira menos vídeos bons do que vários vídeos fracos.
- Use o transcript como contexto principal.
- Você pode ajustar timestamps em aproximadamente ±8 segundos para capturar início natural e fechamento.

REGRAS EDITORIAIS

- Nunca comece no meio de frase.
- Nunca termine no meio do assunto.
- Não corte no meio de uma troca de speaker se isso quebrar o sentido.
- Prefira blocos completos e cronologicamente coerentes.
- Evite saltos grandes de tempo sem ponte narrativa clara.
- Evite vídeos redundantes entre si.
- Se houver dúvida entre vários vídeos desconexos e menos vídeos fortes, prefira menos vídeos fortes.
- Se usar 2 cortes no mesmo vídeo final, o segundo deve continuar naturalmente o primeiro e aprofundar o mesmo assunto.
- Em `short_serie`, prefira 2 cortes conectados por vídeo final quando houver continuação forte no material.
- Só use 1 corte único quando um bloco sozinho já entregar hook, desenvolvimento e fechamento dentro da duração ideal.
- O hook deve ser uma frase falada forte, completa e reconhecível.
- O `post.hook` deve estar totalmente dentro do primeiro corte.
- Informe também `hook_start` e `hook_end` com a minutagem exata, em segundos, onde esse hook aparece.
- Não descreva um hook sem informar a minutagem exata dele.
- `hook_start` deve marcar o começo real da frase do hook e `hook_end` o fim real dessa frase, sem janela folgada.
- Prefira hooks com cerca de 3 a 8 segundos de fala contínua.
- `shorts_content[0]` deve começar antes ou exatamente no ponto em que o hook começa.
- O último corte deve fechar a ideia com payoff, conclusão ou fechamento verbal claro.

{self._build_speaker_guidance(has_named_speakers)}

OUTPUT JSON

A estrutura abaixo é apenas um exemplo de formato.
Não copie a quantidade de itens do exemplo por padrão.
Você deve decidir quantos vídeos finais retornar e quantos cortes usar em cada `shorts_content`, de acordo com o material.

{{
  "job_id": "{job_id}",
  "final_videos": [
    {{
      "video_index": 1,
      "title": "título principal do vídeo final 1",
      "hook": "gancho principal do vídeo final 1",
      "hook_source_cut_index": 0,
      "hook_start": 10.5,
      "hook_end": 16.8,
      "description": "descrição final para postagem",
      "hashtags": ["#tag1", "#tag2", "#tag3"],
      "thumbnail": "ideia de thumbnail principal",
      "soundtrack_suggestion": "political_tension | mystery_tension | finance_tension | generic",
      "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
      "shorts_content": [
        {{
          "start": 10.5,
          "end": 45.3,
          "safe_start": 10.5,
          "safe_end": 45.3,
          "reason": "por que este trecho respeita narrativa, contexto e continuidade",
          "narrative_role": "hook | setup | development | payoff",
          "merge_group": "story_1",
          "continuity_note": "como este corte se conecta ao restante do vídeo final",
          "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
          "transition_after": "hard_cut | punch_in | whoosh | fade | none"
        }}
      ]
    }}
  ]
}}

CONTEXTO PRINCIPAL

TRANSCRIPT RELEVANTE COM SPEAKERS

{transcript_context}

INSTRUÇÃO FINAL

Retorne apenas o JSON final.
Retorne `final_videos` com até 3 vídeos finais bem conectados e prontos para montagem.
Cada item de `final_videos` deve trazer diretamente:
`title`, `hook`, `hook_start`, `hook_end`, `description`, `hashtags`, `thumbnail`, `soundtrack_suggestion`, `speaker_focus` e `shorts_content`.
Cada item de `final_videos` deve ter preferencialmente 2 cortes conectados em `shorts_content` quando houver continuação forte.
Use uma parte em segundos dos cortes escolhidos para servir de hook na tentativa de chamar a atenção do usuário nos primeiros segundos
Valide a duração total de cada `final_video` antes de responder: o total precisa ficar entre {self._response_min_total_duration(clip_mode)} e 120 segundos.
Se algum `final_video` passar de 120 segundos, encurte o último corte desse vídeo antes de responder.
Prefira fechar o assunto corretamente, mesmo que isso leve o vídeo para além de 1 minuto.
Você pode ir até 120 segundos apenas quando essa extensão for necessária para concluir a ideia sem cortar o assunto.
Não replique mecanicamente a quantidade de itens mostrada no exemplo do `OUTPUT JSON`.
Decida a quantidade real de cortes com base no contexto e na força narrativa do material.
Se não houver material forte para 3 vídeos bons, retorne apenas 1 ou 2.
Se algum campo não se aplicar, retorne null, string vazia ou lista vazia.
"""

    def _build_english_prompt(
        self,
        *,
        transcript_context: str,
        job_id: str,
        clip_mode: str,
        video_ratio: str,
        has_named_speakers: bool,
    ) -> str:
        return f"""
JOB_ID: {job_id}

You are a senior video editor.
Your task is to analyze the transcript and choose the best excerpts to assemble high-retention final videos.
Chosen excerpts must belong to the same context or connect naturally.
For each final video, you must also choose a specific spoken excerpt that works as the opening hook in the first seconds.

RETURN FORMAT

- Return valid JSON only.
- Do not use markdown.
- Do not write any text outside the JSON.
- Do not use unescaped double quotes inside string values.

CONFIGURATION

job_id: {job_id}
clip_mode: {clip_mode}
video_ratio: {video_ratio}
content_language: en
output_language: en
subtitle_language: en

{self._build_mode_instructions_en(clip_mode)}

{self._build_ratio_instructions_en(video_ratio)}

{self._build_duration_rules_en(clip_mode)}

WHAT YOU MUST DO

- Choose the best transcript excerpts to assemble up to 3 final videos.
- Each final video must tell one clear idea from beginning to end.
- Cuts inside the same final video must share the same context or connect naturally.
- Each final video must have a strong opening hook, clear development and real closure.
- Prefer fewer strong videos over several weak ones.
- Use the transcript as the main context.
- You may adjust timestamps by about ±8 seconds to capture natural starts and endings.

EDITORIAL RULES

- Never start in the middle of a sentence.
- Never end in the middle of the subject.
- Do not cut in the middle of a speaker exchange if that breaks meaning.
- Prefer complete and chronologically coherent blocks.
- Avoid big time jumps without a clear narrative bridge.
- Avoid redundant videos.
- If the choice is between disconnected videos and fewer stronger videos, prefer fewer stronger videos.
- If using 2 cuts in the same final video, the second must continue the first naturally and deepen the same topic.
- In `short_serie`, prefer 2 connected cuts per final video when strong continuation exists.
- Use a single cut only when one block alone already delivers hook, development and closure.
- The hook must be a strong, complete and recognizable spoken sentence.
- `post.hook` must be fully contained inside the first cut.
- Also provide `hook_start` and `hook_end` with the exact timestamp of the hook in seconds.
- `hook_start` must mark the real beginning of the hook sentence and `hook_end` the real end of that same sentence, without a loose window.
- Prefer hooks with about 3 to 8 seconds of continuous speech.
- `shorts_content[0]` must start before or exactly where the hook starts.
- The last cut must close the idea with a payoff, conclusion or clear verbal closure.

{self._build_speaker_guidance_en(has_named_speakers=has_named_speakers)}

OUTPUT JSON

The structure below is only an example of shape.
Do not mechanically copy the number of items from the example.
You must decide how many final videos to return and how many cuts each `shorts_content` needs.

{{
  "job_id": "{job_id}",
  "final_videos": [
    {{
      "video_index": 1,
      "title": "main title",
      "hook": "opening hook",
      "hook_source_cut_index": 0,
      "hook_start": 10.5,
      "hook_end": 16.8,
      "description": "posting description",
      "hashtags": ["#tag1", "#tag2"],
      "thumbnail": "thumbnail idea",
      "soundtrack_suggestion": "political_tension | mystery_tension | finance_tension | generic",
      "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
      "shorts_content": [
        {{
          "start": 10.5,
          "end": 45.3,
          "safe_start": 10.5,
          "safe_end": 45.3,
          "reason": "why this cut respects narrative, context and continuity",
          "narrative_role": "hook | setup | development | payoff",
          "merge_group": "story_1",
          "continuity_note": "how this cut connects to the rest of the final video",
          "speaker_focus": "SPEAKER_01 | SPEAKER_02 | null",
          "transition_after": "hard_cut | punch_in | whoosh | fade | none"
        }}
      ]
    }}
  ]
}}

MAIN CONTEXT

TRANSCRIPT WITH SPEAKERS

{transcript_context}

FINAL INSTRUCTION

Return only the final JSON.
Return `final_videos` with up to 3 well-connected final videos ready for assembly.
Each `final_videos` item must directly include:
`title`, `hook`, `hook_start`, `hook_end`, `description`, `hashtags`, `thumbnail`, `soundtrack_suggestion`, `speaker_focus` and `shorts_content`.
Validate the total duration of each `final_video` before responding: it must stay between {self._response_min_total_duration(clip_mode)} and 120 seconds.
If there is only enough strong material for 1 or 2 good final videos, return only 1 or 2.
"""

    def _build_speaker_guidance_en(self, has_named_speakers: bool) -> str:
        if not has_named_speakers:
            return """
SPEAKER RULES

- This transcript does not contain reliable speaker labels.
- Do not invent speaker switches.
- Base continuity mainly on complete phrases, context and closure.
"""

        return """
SPEAKER RULES

- The transcript contains speaker labels such as SPEAKER_01, SPEAKER_02, etc.
- Use them to understand dialogue, context changes and continuity.
- Prefer cuts that respect the conclusion of the current speaker.
- Avoid switching speakers exactly at the start or end of a cut without enough context.
"""

    def _build_mode_instructions_en(self, clip_mode: str) -> str:
        if clip_mode == "short":
            return """
MODE: SHORT

- Generate independent cuts.
- Each cut must work on its own.
- Each cut should have beginning, development and ending.
"""
        if clip_mode == "long":
            return """
MODE: LONG

- Generate larger excerpts for a broader narrative.
- Keep more setup, context and closure than a short-form output.
- Preserve chronology and continuity between cuts.
"""
        return """
MODE: SHORT_SERIE

- Generate connected cuts that form one cohesive sequence.
- Cuts may depend on the previous one, but together they must close the idea.
- Related cuts should share the same merge_group.
"""

    def _build_ratio_instructions_en(self, video_ratio: str) -> str:
        if video_ratio == "landscape":
            return """
FORMAT: LANDSCAPE

- Consider a horizontal video with more room for contextual pacing.
"""
        return """
FORMAT: PORTRAIT

- Consider a vertical video with higher pressure for fast retention.
"""

    def _build_duration_rules_en(self, clip_mode: str) -> str:
        max_duration = self.qa_max_clip_duration_sec
        if clip_mode == "long":
            return f"""
DURATION RULES

- Each final video must be at least {self.render_min_long_video_duration_sec} seconds long.
- Prefer final videos between 100 and {max_duration} seconds.
- Keep enough context so the result feels like a strong excerpt from a normal video, not a short.
- Never exceed {max_duration} seconds.
"""
        return f"""
DURATION RULES

- Each final video must be at least 60 seconds long.
- Prefer final videos between 60 and 90 seconds.
- Never exceed {max_duration} seconds.
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

- Gere vídeos com contexto mais amplo e montagem menos agressiva que short.
- Os cortes podem depender da continuidade do bloco anterior.
- Preserve ordem cronológica, contexto e fluidez entre trechos.
- Prefira blocos mais completos, com mais setup, desenvolvimento e fechamento.
- Use merge_group quando vários cortes fizerem parte da mesma história.
- Evite pensar como short viral; pense como corte forte de vídeo normal, ainda dinâmico, mas com mais contexto.
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

- Cada vídeo final deve ter no mínimo 60 segundos no total.
- Para este modo, prefira vídeos finais entre 60 e 90 segundos.
- Se usar 2 cortes conectados no mesmo vídeo final, cada corte interno deve ter pelo menos 12 segundos.
- Cada vídeo final pode usar 1 ou 2 cortes conectados se isso melhorar hook, contexto e fechamento.
- Só fique abaixo de 60 segundos se realmente não houver continuação forte no material.
- Você pode ultrapassar 75 segundos somente quando isso for necessário para concluir o assunto com clareza.
- Nunca exceda {max_duration} segundos.
"""

        if clip_mode == "long":
            return f"""
REGRAS DE DURAÇÃO

- Cada vídeo final deve ter no mínimo {self.render_min_long_video_duration_sec} segundos no total.
- Para este modo, prefira vídeos finais entre 120 e {max_duration} segundos.
- Cada corte interno deve ter contexto suficiente para soar como parte de um vídeo normal, não como short recortado.
- Prefira 2 ou 3 cortes conectados quando isso melhorar contexto, progressão e fechamento.
- Só use 1 corte único quando ele sozinho entregar hook, desenvolvimento e conclusão com duração forte.
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

    def _response_min_total_duration(self, clip_mode: str) -> int:
        if clip_mode == "long":
            return int(self.render_min_long_video_duration_sec)
        return 60

from typing import Dict, List

from app.prompts.prompt_context import build_transcript_context


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
            candidates=[],
            max_chars=int(self.max_context_chars * 0.80),
            max_candidates=self.prompt_max_candidates,
            max_segments_per_candidate=self.prompt_max_segments_per_candidate,
        )

        return f"""
JOB_ID: {job_id}

Você é um editor sênior de vídeo curto.
Sua tarefa é analisar as transcrições e escolher os melhores pedaços para montar vídeos curtos com alto potencial de retenção.
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
Valide a duração total de cada `final_video` antes de responder: o total precisa ficar entre 60 e 120 segundos.
Se algum `final_video` passar de 120 segundos, encurte o último corte desse vídeo antes de responder.
Prefira fechar o assunto corretamente, mesmo que isso leve o vídeo para além de 1 minuto.
Você pode ir até 120 segundos apenas quando essa extensão for necessária para concluir a ideia sem cortar o assunto.
Não replique mecanicamente a quantidade de itens mostrada no exemplo do `OUTPUT JSON`.
Decida a quantidade real de cortes com base no contexto e na força narrativa do material.
Se não houver material forte para 3 vídeos bons, retorne apenas 1 ou 2.
Se algum campo não se aplicar, retorne null, string vazia ou lista vazia.
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

- Cada vídeo final deve ter no mínimo 60 segundos no total.
- Para este modo, prefira vídeos finais entre 60 e 90 segundos.
- Se usar 2 cortes conectados no mesmo vídeo final, cada corte interno deve ter pelo menos 12 segundos.
- Cada vídeo final pode usar 1 ou 2 cortes conectados se isso melhorar hook, contexto e fechamento.
- Só fique abaixo de 60 segundos se realmente não houver continuação forte no material.
- Você pode ultrapassar 75 segundos somente quando isso for necessário para concluir o assunto com clareza.
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

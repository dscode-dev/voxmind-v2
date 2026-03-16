import json
from typing import List, Dict


class ManualPromptBuilder:

    def build(
        self,
        transcript: List[Dict],
        candidates: List[Dict],
        job_id: str,
        clip_mode: str = "short_serie",
        video_ratio: str = "portrait",
    ) -> str:

        mode_instructions = self._build_mode_instructions(clip_mode)
        ratio_instructions = self._build_ratio_instructions(video_ratio)

        return f"""
JOB_ID: {job_id}

Você é um **editor profissional de conteúdo viral e estrategista de retenção para YouTube Shorts, TikTok, Reels e vídeos longos**.

Seu trabalho é analisar a transcrição e identificar os melhores momentos do vídeo para gerar cortes de alta qualidade narrativa.

⚠️ REGRA CRÍTICA

Retorne **APENAS JSON válido**.

Não escreva texto fora do JSON.
Não use markdown.
Não explique nada antes ou depois.

Depois de gerar o conteúdo:

Salve o resultado em um arquivo chamado:

response.json

━━━━━━━━━━━━━━━━━━
CONFIGURAÇÃO DO JOB
━━━━━━━━━━━━━━━━━━

JOB_ID: {job_id}

clip_mode: {clip_mode}
video_ratio: {video_ratio}

{mode_instructions}

{ratio_instructions}

━━━━━━━━━━━━━━━━━━
REGRA MAIS IMPORTANTE
━━━━━━━━━━━━━━━━━━

Independentemente do modo, NUNCA gere cortes que:

• comecem no meio de uma frase
• terminem no meio de uma frase
• terminem antes da conclusão da ideia
• dependam fortemente do trecho anterior para fazer sentido
• deixem o assunto pela metade

Todo corte ou grupo de cortes deve preservar a integridade narrativa.

Se necessário, você pode ajustar os timestamps alguns segundos
antes ou depois do candidato sugerido para capturar:

• começo natural da fala
• desenvolvimento suficiente
• fechamento da ideia

Você pode ajustar os timestamps em aproximadamente ±8 segundos
quando isso for necessário para manter o sentido completo.

━━━━━━━━━━━━━━━━━━
OBJETIVO
━━━━━━━━━━━━━━━━━━

Gerar:

1️⃣ Cortes do vídeo original de acordo com o clip_mode
2️⃣ Sugestões de MERGE entre cortes quando fizer sentido
3️⃣ Um roteiro editorial para vídeo longo

━━━━━━━━━━━━━━━━━━
CRITÉRIO DE QUALIDADE DOS CORTES
━━━━━━━━━━━━━━━━━━

Um bom corte deve:

• ter começo natural
• desenvolver a ideia principal
• terminar com conclusão, revelação, payoff ou fechamento claro
• funcionar bem no formato solicitado
• manter contexto suficiente para o espectador entender o trecho

Priorize momentos com:

• revelações
• histórias pessoais
• momentos de tensão
• opiniões fortes
• perguntas provocativas
• contrastes de ideias
• viradas narrativas
• reflexões fortes
• frases memoráveis

Evite:

• trechos neutros
• partes muito explicativas sem payoff
• trechos quebrados
• momentos sem começo ou sem conclusão
• cortes que parecem apenas pedaços soltos de conversa

━━━━━━━━━━━━━━━━━━
USO DOS CANDIDATOS
━━━━━━━━━━━━━━━━━━

Os candidatos priorizados abaixo são apenas pontos de partida.

Você NÃO está limitado a eles.

Você pode:

• expandir o início
• expandir o fim
• ajustar timestamps
• ignorar candidatos fracos
• combinar candidatos próximos quando fizer sentido narrativo

O objetivo não é obedecer rigidamente aos candidatos.
O objetivo é escolher os MELHORES cortes possíveis mantendo narrativa completa.

━━━━━━━━━━━━━━━━━━
MERGE ENTRE CORTES
━━━━━━━━━━━━━━━━━━

Se dois ou mais cortes fizerem parte da mesma narrativa,
eles devem compartilhar o mesmo valor em:

merge_group

Exemplo:

merge_group: "story_1"

Use merge_group quando os cortes:

• pertencem à mesma ideia
• fazem parte da mesma história
• podem ser unidos para formar um conteúdo mais completo

Se o corte for totalmente independente:

merge_group: null

━━━━━━━━━━━━━━━━━━
PARTE 1 — CORTES
━━━━━━━━━━━━━━━━━━

Identifique os melhores cortes de acordo com o modo solicitado.

Para cada corte informe:

• start
• end
• hook
• reason
• title
• description
• hashtags
• thumbnail
• merge_group

━━━━━━━━━━━━━━━━━━
PARTE 2 — ROTEIRO DE VÍDEO LONGO
━━━━━━━━━━━━━━━━━━

Crie um roteiro editorial para um vídeo longo novo
inspirado no tema geral da transcrição.

Duração aproximada:

8 a 12 minutos.

O roteiro deve incluir:

• title
• hook
• context
• development
• twist
• conclusion
• narration_style

━━━━━━━━━━━━━━━━━━
FORMATO DA RESPOSTA
━━━━━━━━━━━━━━━━━━

Retorne APENAS JSON válido neste formato:

{{
  "job_id": "{job_id}",
  "clip_mode": "{clip_mode}",
  "video_ratio": "{video_ratio}",
  "shorts_content": [
    {{
      "start": 10.5,
      "end": 45.3,
      "hook": "frase forte do início do corte",
      "reason": "por que esse trecho tem potencial e respeita o modo solicitado",
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

━━━━━━━━━━━━━━━━━━
TRANSCRIÇÃO COMPLETA
━━━━━━━━━━━━━━━━━━

{json.dumps(transcript, ensure_ascii=False, indent=2)}

━━━━━━━━━━━━━━━━━━
CANDIDATOS PRIORIZADOS
━━━━━━━━━━━━━━━━━━

{json.dumps(candidates, ensure_ascii=False, indent=2)}

━━━━━━━━━━━━━━━━━━
INSTRUÇÃO FINAL
━━━━━━━━━━━━━━━━━━

1. Gere o JSON final seguindo EXATAMENTE o formato especificado.
2. Salve o resultado como **response.json**.
3. Envie o arquivo **response.json** de volta no Telegram.

JOB_ID: {job_id}
"""

    def _build_mode_instructions(self, clip_mode: str) -> str:

        if clip_mode == "short":
            return """
MODO SOLICITADO: SHORT

Você deve gerar cortes independentes.

Cada corte precisa funcionar sozinho, como um vídeo completo.

Regras desse modo:

• cada corte deve ser individual
• cada corte deve ter começo, desenvolvimento e fim
• um corte não deve depender do anterior
• priorize cortes entre 30 e 60 segundos
• se necessário, o corte pode ficar um pouco maior para preservar o fechamento
• merge_group deve ser null na maioria dos casos
• só use merge_group se houver uma justificativa narrativa muito forte, mas a preferência aqui é por cortes independentes
"""

        if clip_mode == "long":
            return """
MODO SOLICITADO: LONG

Você deve gerar vários cortes que possam compor um vídeo mais longo.

Regras desse modo:

• os cortes não precisam ser totalmente independentes
• eles podem representar blocos contínuos de uma narrativa maior
• pense em blocos como introdução, desenvolvimento, argumento, conclusão
• preserve continuidade entre trechos
• não quebre o raciocínio no meio
• priorize cortes mais longos que permitam manter contexto
• use merge_group quando múltiplos cortes fizerem parte da mesma narrativa maior
• a prioridade aqui é construir uma sequência coesa para um vídeo longo
"""

        return """
MODO SOLICITADO: SHORT-SERIE

Você deve gerar vários cortes conectados narrativamente,
que depois possam ser unidos em um único short final.

Regras desse modo:

• os cortes devem fazer parte da mesma história ou da mesma linha de raciocínio
• os cortes individualmente podem não ser totalmente independentes
• juntos, eles devem formar um único conteúdo coeso
• todos os cortes relacionados devem compartilhar o mesmo merge_group
• a sequência final deve preservar começo, desenvolvimento e fechamento
• a soma dos cortes conectados deve ser suficiente para formar um short final com pelo menos 1 minuto
• nunca deixe a história ou o assunto pela metade
• pense em sequência narrativa: setup → desenvolvimento → payoff
"""

    def _build_ratio_instructions(self, video_ratio: str) -> str:

        if video_ratio == "landscape":
            return """
FORMATO SOLICITADO: LANDSCAPE

Considere que o vídeo final será usado em formato horizontal / tela ampla.
Isso não muda os timestamps diretamente, mas pode influenciar levemente o tipo de trecho escolhido,
priorizando momentos que funcionem bem em tela maior.
"""

        return """
FORMATO SOLICITADO: PORTRAIT

Considere que o vídeo final será usado em formato vertical / shorts / reels / tiktok.
Isso não muda os timestamps diretamente, mas reforça a necessidade de trechos mais diretos,
envolventes e com retenção forte.
"""
import json
from typing import List, Dict


class ManualPromptBuilder:

    def build(self, transcript: List[Dict], candidates: List[Dict], job_id: str) -> str:

        return f"""
JOB_ID: {job_id}

Você é um **editor profissional de conteúdo viral e estrategista de retenção para YouTube Shorts, TikTok e Reels**.

Seu trabalho é analisar a transcrição e identificar **os melhores momentos do vídeo** para gerar cortes que sejam:

• envolventes  
• narrativos  
• emocionalmente interessantes  
• potencialmente virais  

⚠️ REGRA CRÍTICA

Retorne **APENAS JSON válido**.

Não escreva texto fora do JSON.  
Não use markdown.  
Não explique nada antes ou depois.

Depois de gerar o conteúdo:

Salve o resultado em um arquivo chamado:

response.json

━━━━━━━━━━━━━━━━━━
OBJETIVO
━━━━━━━━━━━━━━━━━━

Gerar:

1️⃣ Cortes virais do vídeo original  
2️⃣ Sugestões de MERGE entre cortes  
3️⃣ Um roteiro editorial para vídeo longo  

━━━━━━━━━━━━━━━━━━
CRITÉRIOS PARA CORTES VIRAIS
━━━━━━━━━━━━━━━━━━

Cada corte deve:

• ter duração entre **30 e 60 segundos**  
• começar com **gancho forte nos primeiros segundos**  
• conter **uma ideia completa**  
• despertar **curiosidade ou tensão narrativa**  

Evite:

• cortes que terminam no meio de uma ideia  
• trechos muito explicativos  
• partes neutras sem emoção  

Priorize:

• revelações  
• histórias pessoais  
• momentos de tensão  
• opiniões fortes  
• perguntas provocativas  
• contrastes de ideias  

━━━━━━━━━━━━━━━━━━
ESTRUTURA IDEAL DO CORTE
━━━━━━━━━━━━━━━━━━

Sempre que possível, siga esta estrutura narrativa:

0–3s → frase forte / gancho  
3–15s → construção de curiosidade  
15–40s → desenvolvimento  
40–60s → revelação ou reflexão  

━━━━━━━━━━━━━━━━━━
CONTEXTO NARRATIVO
━━━━━━━━━━━━━━━━━━

Sempre tente identificar mini-histórias dentro do vídeo.

Essas histórias geralmente seguem a estrutura:

SETUP → apresenta o contexto  
CONFLICT → apresenta o problema  
REVEAL → revela algo importante  

Quando múltiplos trechos fizerem parte da mesma sequência narrativa,
agrupá-los usando:

merge_group

Exemplo:

merge_group: "story_1"

Isso indica que esses cortes podem ser combinados
para formar um vídeo maior mantendo coerência narrativa.

Se um corte for independente:

merge_group: null

━━━━━━━━━━━━━━━━━━
MERGE ENTRE CORTES
━━━━━━━━━━━━━━━━━━

Se dois ou três cortes fizerem parte da mesma história,
eles devem compartilhar o mesmo valor de merge_group.

Exemplo:

Corte A → setup  
Corte B → conflito  
Corte C → revelação  

Todos com:

merge_group: "story_1"

Isso permite criar um vídeo mais longo unindo esses cortes.

━━━━━━━━━━━━━━━━━━
PARTE 1 — CORTES VIRAIS
━━━━━━━━━━━━━━━━━━

Identifique entre **3 e 6 cortes** com maior potencial viral.

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
  "shorts_content": [
    {{
      "start": 10.5,
      "end": 45.3,
      "hook": "frase forte do início do corte",
      "reason": "por que esse trecho tem potencial viral",
      "title": "título curto e impactante",
      "description": "descrição curta para shorts",
      "hashtags": ["#tag1", "#tag2", "#tag3"],
      "thumbnail": "ideia de thumbnail em poucas palavras",
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
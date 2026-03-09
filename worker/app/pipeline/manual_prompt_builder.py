import json
from typing import List, Dict


class ManualPromptBuilder:

    def build(self, transcript: List[Dict], candidates: List[Dict], job_id: str) -> str:

        return f"""
JOB_ID: {job_id}

Você é um editor e roteirista especialista em conteúdo viral para YouTube Shorts, TikTok e YouTube de formato longo.

Sua missão é analisar a transcrição abaixo e produzir dois tipos de conteúdo:

1. CORTES VIRAIS do vídeo original
2. ROTEIRO para um vídeo longo novo, baseado no tema geral da transcrição

IMPORTANTE:
- Os cortes devem usar timestamps reais do vídeo original.
- O roteiro do vídeo longo deve ser editorialmente transformativo.
- Retorne APENAS JSON válido.
- Inclua obrigatoriamente o mesmo job_id no JSON final.

⚠️ REGRA CRÍTICA:
Sua resposta FINAL deve ser um **arquivo JSON válido**.
Não escreva texto fora do JSON.
Não use markdown.
Não explique nada antes ou depois do JSON.

Depois de gerar o conteúdo, **salve o resultado em um arquivo chamado**:

response.json

━━━━━━━━━━━━━━━━━━
PARTE 1 — CORTES VIRAIS
━━━━━━━━━━━━━━━━━━

Identifique de 2 até 5 cortes com maior potencial viral.

Cada corte deve:
- ter gancho forte nos primeiros segundos
- despertar curiosidade, surpresa, conflito ou tensão
- ter impacto emocional ou intelectual
- funcionar isoladamente
- ter duração ideal entre 20 e 50 segundos

Priorize momentos com:
- revelações
- erros ou segredos
- frases inesperadas
- opiniões fortes
- viradas narrativas
- contraste de ideias
- tensão emocional
- perguntas implícitas

Evite:
- trechos lentos
- explicações longas
- momentos neutros
- partes sem impacto

Para cada corte, informe:

- start
- end
- hook
- reason
- title
- description
- hashtags
- thumbnail

━━━━━━━━━━━━━━━━━━
PARTE 2 — ROTEIRO DE VÍDEO LONGO
━━━━━━━━━━━━━━━━━━

Crie um roteiro para um vídeo longo novo, de 8 a 12 minutos, inspirado no tema geral da transcrição.

Esse roteiro deve ser:

- editorialmente transformativo
- adequado para narração por voz humana ou IA
- claro, natural e envolvente
- sem mencionar que veio de uma transcrição

O roteiro deve incluir:

- title
- hook
- context
- development
- twist
- conclusion
- narration_style

━━━━━━━━━━━━━━━━━━
FORMATO DA RESPOSTA
━━━━━━━━━━━━━━━━━━

Retorne APENAS JSON válido neste formato:

{{
  "job_id": "{job_id}",
  "shorts_content": [
    {{
      "start": 12.5,
      "end": 38.4,
      "hook": "frase forte do início do corte",
      "reason": "por que esse trecho tem potencial viral",
      "title": "título curto e impactante",
      "description": "descrição curta para shorts",
      "hashtags": ["#tag1", "#tag2", "#tag3"],
      "thumbnail": "ideia de thumbnail em poucas palavras"
    }}
  ],
  "long_video_script": {{
    "title": "título sugerido",
    "hook": "gancho inicial de abertura",
    "context": "explicação clara do tema",
    "development": "desenvolvimento narrativo principal",
    "twist": "ponto de virada ou revelação",
    "conclusion": "conclusão reflexiva",
    "narration_style": "dicas breves de ritmo e tom para narração"
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
2. Salve o resultado como um arquivo chamado **response.json**.
3. Envie o arquivo **response.json** de volta no Telegram para continuar o pipeline.

Este JSON será usado automaticamente para finalizar o JOB_ID:

{job_id}
"""
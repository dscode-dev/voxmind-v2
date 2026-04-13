from __future__ import annotations

import re
import json
import os
from datetime import datetime, timezone

import httpx

from app.core.settings import settings


class ScriptAgentService:
    agent_version = "script-agent-v1-local"

    def build_prompt(self, payload: dict) -> str:
        return (
            "Você é um estrategista sênior de conteúdo para redes sociais.\n"
            "Gere um pacote completo de roteiro seguindo o contrato JSON do ClipFlow.\n"
            f"Tema: {payload.get('topic')}\n"
            f"Plataforma: {payload.get('platform')}\n"
            f"Público: {payload.get('target_audience')}\n"
            f"Objetivo: {payload.get('objective')}\n"
            f"Tom: {payload.get('tone')}\n"
            f"Idioma: {payload.get('language')}\n"
            f"Duração alvo: {payload.get('target_duration_sec')} segundos\n"
        )

    def generate(self, payload: dict) -> dict:
        if settings.script_agent_provider.lower() == "openai":
            generated = self._generate_with_openai(payload)
            if generated:
                return generated
        return self._generate_local(payload)

    def _generate_with_openai(self, payload: dict) -> dict | None:
        api_key = settings.script_agent_openai_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None

        prompt = self.build_prompt(payload)
        schema_instruction = (
            "Retorne apenas JSON válido, sem markdown, com as chaves: "
            "agent_version, generated_at, platform, language, target_duration_sec, script_title, "
            "core_angle, hook_options, final_hook, title_options, thumbnail_options, full_script, "
            "spoken_lines, scene_plan, description_options, hashtags, cta_options, posting_plan, "
            "recording_instructions, editing_instructions. "
            "Respeite o idioma informado. Não traduza para outro idioma."
        )

        try:
            response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.script_agent_openai_model,
                    "temperature": 0.7,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": "Você é um agente especialista em roteiro, social video e planejamento de postagem.",
                        },
                        {"role": "user", "content": f"{schema_instruction}\n\n{prompt}"},
                    ],
                },
                timeout=settings.script_agent_timeout_sec,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            output = json.loads(content)
            return self._normalize_output(output, payload, source="openai")
        except Exception:
            return None

    def _generate_local(self, payload: dict) -> dict:
        topic = self._clean(payload.get("topic") or "tema principal")
        platform = self._clean(payload.get("platform") or "instagram_reels")
        audience = self._clean(payload.get("target_audience") or "pessoas interessadas no tema")
        objective = self._clean(payload.get("objective") or "gerar atenção e incentivar ação")
        tone = self._clean(payload.get("tone") or "direto, claro e confiante")
        language = self._clean(payload.get("language") or "pt-BR")
        duration = int(payload.get("target_duration_sec") or 45)
        duration = max(20, min(duration, 600))

        core_angle = f"mostrar por que {topic} importa agora para {audience}"
        final_hook = f"Se você ainda ignora {topic}, pode estar perdendo uma vantagem enorme."
        title_base = self._title_case(topic)

        spoken_lines = [
            final_hook,
            f"O ponto central é simples: {topic} não é só uma ideia bonita, é uma decisão prática.",
            f"Para {audience}, isso muda a forma de pensar, produzir e agir.",
            f"O erro comum é tentar resolver tudo de uma vez, sem clareza de objetivo.",
            f"Comece escolhendo uma promessa específica: {objective}.",
            "Depois, transforme essa promessa em uma sequência curta: problema, virada, exemplo e ação.",
            f"Com um tom {tone}, a mensagem fica mais humana e mais fácil de lembrar.",
            "No final, a pessoa precisa sair com uma próxima ação clara.",
            f"Se esse tema faz sentido para você, salve este roteiro e use {topic} como ponto de partida.",
        ]

        output = {
            "agent_version": self.agent_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "platform": platform,
            "language": language,
            "target_duration_sec": duration,
            "script_title": f"{title_base}: o roteiro pronto para postar",
            "core_angle": core_angle,
            "hook_options": [
                final_hook,
                f"O maior erro sobre {topic} é começar pelo lugar errado.",
                f"Antes de falar sobre {topic}, você precisa entender isso.",
            ],
            "final_hook": final_hook,
            "title_options": [
                f"{title_base}: o erro que quase ninguém percebe",
                f"Como usar {topic} com mais clareza",
                f"O jeito simples de explicar {topic}",
            ],
            "thumbnail_options": [
                f"Texto curto: {self._thumbnail_phrase(topic)}",
                "Rosto em close com expressão de descoberta e fundo limpo",
                "Antes/depois visual mostrando confusão versus clareza",
            ],
            "full_script": "\n".join(spoken_lines),
            "spoken_lines": spoken_lines,
            "scene_plan": self._scene_plan(spoken_lines),
            "description_options": [
                f"Um roteiro direto para explicar {topic} com clareza e transformar atenção em ação.",
                f"Se você quer falar sobre {topic} sem enrolar, use esta estrutura.",
            ],
            "hashtags": self._hashtags(topic, platform),
            "cta_options": [
                "Salve para usar depois.",
                "Comente qual parte você aplicaria primeiro.",
                "Compartilhe com alguém que precisa organizar essa ideia.",
            ],
            "posting_plan": {
                "platform": platform,
                "timezone": "America/Recife",
                "best_time_windows": ["11:30-13:00", "18:00-20:30"],
                "best_weekdays": ["terça", "quarta", "quinta"],
                "posting_frequency_note": "Publique em janela de maior atenção e reaproveite o tema em 2 variações nos próximos 7 dias.",
            },
            "recording_instructions": [
                "Grave em plano médio ou close-up, olhando direto para a câmera.",
                "Faça pausas curtas entre frases para facilitar cortes.",
                "Dê ênfase no hook e na frase de fechamento.",
            ],
            "editing_instructions": [
                "Abra com o hook sem vinheta.",
                "Use cortes secos para remover pausas longas.",
                "Adicione legenda destacada nas frases de problema, virada e CTA.",
                "Use B-roll simples apenas quando reforçar o exemplo.",
            ],
        }
        return self._normalize_output(output, payload, source="local")

    def _normalize_output(self, output: dict, payload: dict, source: str) -> dict:
        output = dict(output or {})
        output.setdefault("agent_version", f"script-agent-v1-{source}")
        output.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
        output.setdefault("platform", self._clean(payload.get("platform") or "instagram_reels"))
        output.setdefault("language", self._clean(payload.get("language") or "pt-BR"))
        output.setdefault("target_duration_sec", int(payload.get("target_duration_sec") or 45))
        for key in (
            "hook_options",
            "title_options",
            "thumbnail_options",
            "spoken_lines",
            "scene_plan",
            "description_options",
            "hashtags",
            "cta_options",
            "recording_instructions",
            "editing_instructions",
        ):
            output.setdefault(key, [])
        output.setdefault("posting_plan", {})
        output["_generation_source"] = source
        return output

    def _scene_plan(self, spoken_lines: list[str]) -> list[dict]:
        roles = ["hook", "setup", "problem", "turn", "example", "framework", "credibility", "payoff", "cta"]
        return [
            {
                "scene_index": index,
                "goal": roles[index - 1] if index - 1 < len(roles) else "development",
                "visual_direction": "close-up talking head" if index == 1 else "talking head with subtle punch-in",
                "spoken_text": line,
            }
            for index, line in enumerate(spoken_lines, start=1)
        ]

    def _hashtags(self, topic: str, platform: str) -> list[str]:
        tokens = [token for token in re.findall(r"[a-zA-ZÀ-ÿ0-9]+", topic.lower()) if len(token) > 3]
        tags = [f"#{token[:28]}" for token in tokens[:4]]
        base = ["#conteudo", "#roteiro", "#criadores"]
        if "youtube" in platform:
            base.append("#youtube")
        if "tiktok" in platform:
            base.append("#tiktok")
        return list(dict.fromkeys(tags + base))[:8]

    def _thumbnail_phrase(self, topic: str) -> str:
        words = re.findall(r"[a-zA-ZÀ-ÿ0-9]+", topic)
        phrase = " ".join(words[:3]) or "ideia forte"
        return phrase.upper()

    def _title_case(self, value: str) -> str:
        return " ".join(word.capitalize() for word in value.split())

    def _clean(self, value: object) -> str:
        return str(value or "").strip()

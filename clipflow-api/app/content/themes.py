"""O catálogo de temas que o sistema já conhece ao subir.

Um tema é o que a busca procura e o que o modelo recebe como assunto do vídeo. Digitado do
zero toda vez, ele vira "futebol", "Futebol", "futebol brasileiro" e "fut br" — quatro
pipelines que deveriam concordar e não concordam, e uma busca que rende resultados diferentes
por causa de uma maiúscula.

**Em código, não em tabela.** O catálogo é uma sugestão, não um dado do operador: ninguém o
edita, nada aponta para ele por chave estrangeira, e uma tabela semeada no arranque só
acrescentaria uma forma de o código e o banco discordarem. O que o operador escreve continua
livre — o `theme` do pipeline é texto, e a opção "Outros" existe justamente para o que não
está aqui.

As palavras-chave vêm junto porque são a mesma decisão editorial: escolher "Fórmula 1" e ter
de inventar os termos de busca na mão é escolher metade da coisa.
"""
from __future__ import annotations

from typing import Any

# Agrupados pelo que o operador reconhece, não pelo que o sistema faz com eles.
THEMES: list[dict[str, Any]] = [
    {
        "group": "Futebol",
        "items": [
            {
                "id": "futebol_brasileiro",
                "label": "Futebol brasileiro",
                "keywords": ["brasileirão", "série a brasil", "gols", "melhores momentos"],
            },
            {
                "id": "futebol_internacional",
                "label": "Futebol internacional",
                "keywords": ["champions league", "libertadores", "gols", "melhores momentos"],
            },
            {
                "id": "serie_a_italiana",
                "label": "Serie A italiana",
                "keywords": ["serie a", "milan", "inter", "juventus", "melhores momentos"],
            },
            {
                "id": "premier_league",
                "label": "Premier League",
                "keywords": ["premier league", "highlights", "goals"],
            },
            {
                "id": "futebol_entrevistas",
                "label": "Entrevistas e coletivas",
                "keywords": ["coletiva pós-jogo", "entrevista", "declaração"],
            },
        ],
    },
    {
        "group": "Outros esportes",
        "items": [
            {
                "id": "formula_1",
                "label": "Fórmula 1",
                "keywords": ["fórmula 1", "grand prix", "melhores momentos"],
            },
            {
                "id": "ufc_mma",
                "label": "UFC e MMA",
                "keywords": ["ufc", "mma", "nocaute", "melhores momentos"],
            },
            {
                "id": "nba_basquete",
                "label": "Basquete e NBA",
                "keywords": ["nba", "basquete", "highlights"],
            },
        ],
    },
    {
        "group": "Conteúdo falado",
        "items": [
            {
                "id": "podcast_cortes",
                "label": "Cortes de podcast",
                "keywords": ["podcast", "corte", "entrevista"],
            },
            {
                "id": "noticias",
                "label": "Notícias e atualidades",
                "keywords": ["notícia", "reportagem", "análise"],
            },
            {
                "id": "games_esports",
                "label": "Games e eSports",
                "keywords": ["gameplay", "esports", "melhores jogadas"],
            },
        ],
    },
]


def as_payload() -> list[dict[str, Any]]:
    """O catálogo como a API o entrega. `Outros` não vem daqui: é uma escolha da interface,
    não um tema — a tela oferece o campo livre, e o que for digitado é gravado como está."""
    return THEMES


def keywords_for(theme_id: str) -> list[str]:
    """As palavras-chave sugeridas para um tema do catálogo, ou nada se não for um deles."""
    for group in THEMES:
        for item in group["items"]:
            if item["id"] == theme_id:
                return list(item["keywords"])
    return []

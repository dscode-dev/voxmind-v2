"""Editorial agent (placeholder).

TODO(Phase 5+): select/refine cuts and narrative structure. Will reuse the cuts prompt +
schema from ``app.prompts.builder`` and run through the provider router. No logic yet.
"""
from __future__ import annotations

from app.agents.base_agent import BaseAgent


class EditorialAgent(BaseAgent):
    name = "editorial"
    # TODO(Phase 5+): implement editorial selection/refinement.

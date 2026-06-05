"""AI execution layer for the worker.

Worker business logic must never call a model API directly — it only knows
``provider_router.generate_json(...)``. Providers (OpenAI today, an optional local node next)
are pluggable behind :class:`app.ai.providers.base_provider.AIProvider`.
"""

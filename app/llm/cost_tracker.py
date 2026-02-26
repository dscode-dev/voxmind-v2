from __future__ import annotations

def approx_tokens(text: str) -> int:
    # Rough heuristic: ~4 chars per token (guardrail only).
    return max(1, len(text) // 4)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    # Placeholder pricing table (guardrail). We'll replace with real tables later.
    pricing = {
        "gpt-4o": (5.0e-6, 15.0e-6),        # input, output per token (placeholder)
        "gpt-4o-mini": (0.15e-6, 0.6e-6),   # placeholder
    }
    in_rate, out_rate = pricing.get(model, (1e-6, 2e-6))
    return (input_tokens * in_rate) + (output_tokens * out_rate)

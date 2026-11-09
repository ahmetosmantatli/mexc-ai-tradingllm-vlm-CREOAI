"""Per-model free-tier caps for the models the router actually calls.

These are the operator's 9 Nov 2026 free-tier numbers. A known model cannot be
widened by a higher env or route value. Unknown models stay callable. A single
request larger than a known TPM is not sent. Two API keys are two accounts:
limits are never added together to make one larger request.
"""
import math


GROQ_FREE = (30, 8000, 1000, 200_000)
GEMMA = (30, 16_000, 14_400, 0)

# (provider, model) -> rpm, tpm, rpd, tpd. tpd 0 means no local daily token cap.
KNOWN_LIMITS = {
    ('groq', 'openai/gpt-oss-20b'): GROQ_FREE,
    ('groq', 'openai/gpt-oss-120b'): GROQ_FREE,
    ('groq', 'qwen/qwen3.8-27b'): GROQ_FREE,
    ('gemini', 'gemini-3.5-flash-lite'): (15, 250_000, 500, 0),
    ('gemini', 'gemini-3.1-flash-lite'): (15, 250_000, 500, 0),
    ('gemini', 'gemma-4-26b-a4b-it'): GEMMA,
    ('gemini', 'gemma-4-31b-it'): GEMMA,
    ('gemini', 'gemini-2.5-flash'): (5, 250_000, 20, 0),
    ('gemini', 'gemini-2.5-flash-lite'): (10, 250_000, 20, 0),
    ('gemini', 'gemini-3.6-flash'): (5, 250_000, 20, 0),
    ('gemini', 'gemini-3.8-flash'): (5, 250_000, 20, 0),
}

# Only this TPM class may receive a compact decision packet.
COMPACT_TPM = 8000


def cap_limits(provider, model, limits):
    """Known ceilings win over a wider config. A tighter config still applies."""
    known = KNOWN_LIMITS.get((provider, model))
    if not known:
        return tuple(limits)
    capped = []
    for configured, ceiling in zip(limits, known):
        if not ceiling:
            capped.append(configured)
        elif not configured:
            capped.append(ceiling)
        else:
            capped.append(min(configured, ceiling))
    return tuple(capped)


def estimate_input_tokens(system, user, images=None):
    """Input-token estimate. Live Gemini counted about 1.6 bytes per token on pa-context-v4."""
    text = (system or '') + (user or '')
    return math.ceil(len(text.encode('utf-8')) / 1.6) + 2048 * len(images or [])

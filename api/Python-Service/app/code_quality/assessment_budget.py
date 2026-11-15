"""Token budgeting for bounded assessment requests."""

from functools import lru_cache

ASSESSMENT_BUDGET_HELP = (
    "Open Settings → Models and providers → Assessment request token limit, "
    "increase the limit if your provider allows it, save, and retry. "
    "The model context window also limits the request. If it is too small, "
    "use a model with a larger context or reduce assessment evidence."
)


@lru_cache(maxsize=1)
def _gpt_oss_encoding():
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_harmony")
    except Exception:
        return None


def request_token_bound(system: str, prompt: str, model: str) -> int:
    text = system + "\n" + prompt
    if model.startswith("openai/gpt-oss-") or model.startswith("gpt-oss-"):
        encoding = _gpt_oss_encoding()
        if encoding is not None:
            return len(encoding.encode_ordinary(text)) + 64
    return len(text.encode("utf-8")) + 64

"""Token budgeting for bounded assessment requests."""

from functools import lru_cache


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

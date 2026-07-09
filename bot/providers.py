import re
import time
from bot.clients import ai
from bot.config import (
    AI_MAX_TOKENS,
    AI_REQUEST_TIMEOUT,
    AI_RETRIES,
    HF_REQUEST_TIMEOUT,
    HF_SPACE_ID,
    HF_TOKEN,
    MODEL,
)
from bot.preferences import get_provider

# HF Gradio knobs — hardcoded defaults for ArmGPT
# 80 tokens at ~5 tok/s ≈ 16s. Must finish well inside Telegram's webhook
# timeout (~60s) accounting for HF cold-start jitter and network round-trips.
HF_LENGTH = 100
HF_TEMPERATURE = 0.6
HF_TOP_K = 30


def _call_main(messages: list, retries: int = AI_RETRIES):
    """Call the OpenAI-compatible API with bounded retries.

    Each attempt is capped by AI_REQUEST_TIMEOUT and the per-attempt timeout
    is dynamically reduced if the wall-clock budget is shrinking, so total
    elapsed time stays under Telegram's ~60s webhook window even on the worst path.
    """
    deadline = time.monotonic() + AI_REQUEST_TIMEOUT * retries + retries
    for attempt in range(retries):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("AI provider deadline exceeded")
        timeout = min(AI_REQUEST_TIMEOUT, remaining)
        try:
            response = ai.chat.completions.create(
                model=MODEL,
                messages=messages,
                timeout=timeout,
                max_tokens=AI_MAX_TOKENS,
            )
            return response.choices[0].message.content
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = min(2**attempt, max(0, deadline - time.monotonic()))
            print(
                f"AI call failed (attempt {attempt + 1}/{retries}): {e} — retrying in {wait}s"
            )
            time.sleep(wait)


def _last_user_message(messages: list) -> str:
    """Return only the most recent user message.

    ArmGPT is a base completion model trained on raw Armenian text — it has
    no concept of chat turns. Feeding it a "User: ...\\nAssistant:" transcript
    just confuses it. Pass the bare user prompt and let the model continue.
    """
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "")
    return ""


def _strip_html(text: str) -> str:
    """Remove HTML tags from Gradio output."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _call_hf(messages: list) -> str:
    """Call the Hugging Face Gradio space. No retry — HF is slow."""
    from gradio_client import Client

    prompt = _last_user_message(messages)
    # httpx_kwargs caps every underlying HTTP call (config fetch + predict)
    # so a hung Space can't wedge the PA worker past Telegram's webhook
    # timeout — without it, dedupe pre-claim would silently swallow retries.
    client = Client(
        HF_SPACE_ID,
        hf_token=HF_TOKEN or None,
        httpx_kwargs={"timeout": HF_REQUEST_TIMEOUT},
    )
    result = client.predict(
        prompt,
        HF_LENGTH,
        HF_TEMPERATURE,
        HF_TOP_K,
        api_name="/generate",
    )
    # Gradio predict returns the final yielded value. For this space it's a
    # tuple (html_output, status_text). We only want the text.
    if isinstance(result, (tuple, list)) and len(result) >= 1:
        text = result[0]
    else:
        text = result
    text = _strip_html(str(text))
    # Remove the echoed prompt if the model includes it
    if text.startswith(prompt):
        text = text[len(prompt) :].strip()
    return text or "(empty response from ArmGPT)"


def generate(user_id: int, messages: list) -> str:
    """Dispatch to the user's chosen AI provider and return a reply string."""
    provider = get_provider(user_id)
    if provider == "hf":
        return _call_hf(messages)
    return _call_main(messages)

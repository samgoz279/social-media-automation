"""
claude_client.py — All interactions with the Anthropic API.

Each public function corresponds to one logical step in the agent flow:
ideation, drafting, revision. Prompts live in prompts.py; this file is
only responsible for making the calls and returning clean Python objects.
"""

import json
import os
import re
import time
from typing import Callable, TypeVar

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from src.prompts import (
    IDEATION_SYSTEM,
    build_drafting_system,
    build_revision_system,
)

# ─────────────────────────────────────────────────────────────────────────────
# CLIENT SETUP
# ─────────────────────────────────────────────────────────────────────────────

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024

# The Anthropic() constructor reads ANTHROPIC_API_KEY from the environment
# automatically. We instantiate once at module load — the client is reusable
# across calls and handles connection pooling internally.
_client = Anthropic()


# ─────────────────────────────────────────────────────────────────────────────
# RETRY HELPER
# ─────────────────────────────────────────────────────────────────────────────
# Transient failures (network blips, rate limits) shouldn't crash the CLI.
# We retry up to 3 times with exponential backoff. Auth and bad-request
# errors are NOT retried — those are bugs, not flakes.

T = TypeVar("T")


def _with_retry(fn: Callable[[], T], max_attempts: int = 3) -> T:
    """Run fn() with exponential backoff on transient API errors."""
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except (APIConnectionError, RateLimitError) as e:
            last_error = e
            if attempt < max_attempts - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                time.sleep(wait)
        except APIError as e:
            # Non-retryable API errors (auth, bad request, etc.) — fail fast.
            raise RuntimeError(f"Anthropic API error: {e}") from e
    raise RuntimeError(
        f"Anthropic API failed after {max_attempts} attempts: {last_error}"
    ) from last_error


# ─────────────────────────────────────────────────────────────────────────────
# CORE CALL
# ─────────────────────────────────────────────────────────────────────────────
# Single chokepoint for hitting the API. Every public function below routes
# through here. If we ever want streaming, logging, or token accounting,
# this is the only place we change.

def _call_claude(system: str, user_message: str, temperature: float) -> str:
    """Send a single-turn request to Claude and return the text response."""

    def _do_call():
        return _client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )

    response = _with_retry(_do_call)

    # The response.content is a list of content blocks. For a plain text
    # response there's exactly one TextBlock. We defensively check anyway.
    if not response.content:
        raise RuntimeError("Claude returned an empty response.")

    text = response.content[0].text.strip()
    if not text:
        raise RuntimeError("Claude returned an empty text block.")

    return text


# ─────────────────────────────────────────────────────────────────────────────
# JSON PARSING
# ─────────────────────────────────────────────────────────────────────────────
# Even with strict prompt instructions, models occasionally wrap JSON in
# ```json fences or add a sentence of preamble. Strip the noise before parsing.

def _extract_json_array(text: str) -> list:
    """Pull a JSON array out of a model response, tolerating common wrappers."""
    # Strip markdown code fences if present.
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        # Otherwise, grab the first [...] block we can find.
        bracketed = re.search(r"\[.*\]", text, re.DOTALL)
        if bracketed:
            text = bracketed.group(0)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Could not parse ideas as JSON. Raw response:\n{text}"
        ) from e


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def generate_ideas(topic: str) -> list[dict]:
    """
    Generate exactly 3 post ideas for the given topic.

    Each idea is a dict with keys: platform, tone, concept.
    Raises RuntimeError on API or parsing failure.
    """
    user_message = f"Topic: {topic}\n\nGenerate 3 post ideas as specified."
    raw = _call_claude(
        system=IDEATION_SYSTEM,
        user_message=user_message,
        temperature=1.0,  # high — we want variety across the 3 ideas
    )

    ideas = _extract_json_array(raw)

    # Validate shape. Better to fail loudly here than to crash later in the CLI.
    if not isinstance(ideas, list) or len(ideas) != 3:
        raise RuntimeError(f"Expected 3 ideas, got: {ideas}")
    for i, idea in enumerate(ideas, 1):
        missing = {"platform", "tone", "concept"} - idea.keys()
        if missing:
            raise RuntimeError(f"Idea {i} missing keys: {missing}")

    return ideas


def draft_post(idea: dict) -> str:
    """Draft a full post based on a selected idea."""
    user_message = (
        f"Platform: {idea['platform']}\n"
        f"Tone: {idea['tone']}\n"
        f"Concept: {idea['concept']}\n\n"
        f"Write the post."
    )
    return _call_claude(
        system=build_drafting_system(idea["platform"]),
        user_message=user_message,
        temperature=0.7,  # moderate — voice consistency matters more than novelty
    )


def revise_post(previous_draft: str, feedback: str, platform: str) -> str:
    """Revise an existing draft based on user feedback."""
    user_message = (
        f"Previous draft:\n---\n{previous_draft}\n---\n\n"
        f"Feedback: {feedback}\n\n"
        f"Return the revised post."
    )
    return _call_claude(
        system=build_revision_system(platform),
        user_message=user_message,
        temperature=0.4,  # low — we want minimal, targeted changes
    )
"""Gemini access for every AI feature: one client, one fallback policy, and
the model chain each feature walks.

Try the best model first, step down when Google refuses one, remember the
refusal.  One `generate()` for Dexter and the Leakage analysis alike — two
copies of a retry policy drift the first time one of them is fixed.

The chains (checked against ai.google.dev on 2026-09-28): 2.0 Flash is shut
down, the 2.5 family is limited to projects that already used it, and 3.1 Pro
has no free tier.  The free tier's quota is per MODEL, so the two chains are
built not to compete: the Leakage analysis — the one that matters — starts
from the strongest models, and Dexter, which only reads numbers out of a
snapshot, starts from the lite models with the largest quota.  They meet only
at the tail, when everything else is exhausted (a test keeps it that way).

Gemini exposes no "remaining quota" endpoint, so the policy is REACTIVE: it
asks the best model and learns from the refusal —

  * ``limit: 0``            no free-tier quota on this model  → skip for 24 h
  * 429 / RESOURCE_EXHAUSTED rate limited                     → skip for Google's retryDelay
  * 503 / UNAVAILABLE        overloaded                        → skip for 30 s
  * 404 / NOT_FOUND          model does not exist (shut down)  → skip for the session
  * 403 / PERMISSION_DENIED  model not open to this project    → skip for 24 h
  * anything else            a real error (bad request, …)     → stop, report it

The cooldowns are kept in a dict the caller owns (session state), shared by
every feature: a model exhausted for Dexter is exhausted for everyone, since
they spend the same API key's quota.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import streamlit as st

logger = logging.getLogger(__name__)

# Lazy import — the app must boot even without the dependency installed.
try:
    from google import genai
    from google.genai import types
    AVAILABLE = True
except ImportError:                                                     # pragma: no cover
    genai = None
    types = None
    AVAILABLE = False

# Session-state key holding {model: unix time the cooldown ends}.
COOLDOWN_KEY = "ai_exhausted_models"

# The strongest first.  3.1 Pro answers only on a paid plan: on the free tier
# it refuses with `limit: 0` once and is skipped for a day, so a paid key
# would be used automatically and a free one costs one refused call a day.
ANALYSIS_CHAIN: list[str] = [
    "gemini-3.1-pro-preview",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
]

# Dexter answers from a snapshot of numbers: a lite model reads it well and
# has the largest free quota, and it leaves the strong models to the analysis.
DEXTER_CHAIN: list[str] = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]


@st.cache_resource(show_spinner=False)
def model_status() -> dict[str, tuple[str, float]]:
    """{model: (what happened the last time it was asked, when)} for the whole
    process — so a page can say which model answered and why a stronger one
    did not, instead of leaving "is it using the best model?" to a guess."""
    return {}


def _note(model: str, outcome: str) -> None:
    try:
        model_status()[model] = (outcome, time.time())
    except Exception:
        logger.debug("model status unavailable", exc_info=True)


@st.cache_resource(show_spinner=False)
def shared_cooldowns() -> dict[str, float]:
    """Cooldowns for the whole process, not one session: a quota refusal is
    about the API key, so every visitor should skip the refused model."""
    return {}

_RETRY_DELAY_RE = re.compile(
    r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s",
    re.IGNORECASE,
)


def parse_retry_delay(err_str: str, default: float = 60.0) -> float:
    """Pull a `retryDelay: 16s` value out of a Gemini RESOURCE_EXHAUSTED error.

    The SDK exposes the original Google API error payload as the exception
    string, so a regex over the string body is the easiest way to grab the
    `RetryInfo` hint without depending on private SDK internals.
    """
    m = _RETRY_DELAY_RE.search(err_str)
    return float(m.group(1)) if m else default


def api_key() -> str | None:
    try:
        return st.secrets["GEMINI_API_KEY"]
    except (KeyError, FileNotFoundError):
        return None


@st.cache_resource(show_spinner=False)
def client(key: str):
    """One Gemini Client per app process — its underlying HTTP pool is shared.

    `@st.cache_resource` rather than `st.session_state`: Streamlit serialises
    session_state values on each rerun, which closes the client's httpx pool
    ("Cannot send a request, as the client has been closed").
    """
    return genai.Client(api_key=key)


def ready() -> bool:
    return AVAILABLE and api_key() is not None


@dataclass
class Result:
    """What one `generate()` call produced.

    `model` is set exactly when a model answered — `text` may still be empty.
    When every candidate failed `model` is None and `error` holds the last
    error string, which `failure_message()` turns into something readable.
    """
    text: str | None
    model: str | None
    error: str = ""


def generate(contents, config, models: list[str],
             cooling: dict[str, float]) -> Result:
    """Ask each model in turn until one answers (see the module docstring)."""
    key = api_key()
    if not (AVAILABLE and key):
        return Result(None, None, "NOT_CONFIGURED")

    now = time.time()
    last_err = ""
    asked = False
    for model in models:
        if cooling.get(model, 0.0) > now:
            continue
        asked = True
        try:
            response = client(key).models.generate_content(
                model=model, contents=contents, config=config,
            )
            _note(model, "answered")
            return Result((response.text or "").strip(), model)
        except Exception as exc:
            err_str = str(exc)
            last_err = err_str
            if "limit: 0" in err_str:
                # Account-level (no free tier on this model): nothing to do
                # server-side, retry tomorrow.
                cooling[model] = now + 24 * 3600
                _note(model, "no quota on this plan (limit: 0)")
                logger.info("Model %s has no quota (limit: 0) — trying next", model)
                continue
            if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
                cooling[model] = now + parse_retry_delay(err_str, default=60.0)
                _note(model, "rate-limited")
                logger.info("Model %s rate-limited — trying next", model)
                continue
            if ("503" in err_str or "UNAVAILABLE" in err_str
                    or "overload" in err_str.lower()):
                cooling[model] = now + 30.0
                _note(model, "overloaded")
                logger.info("Model %s overloaded (503) — trying next", model)
                continue
            if "404" in err_str or "NOT_FOUND" in err_str:
                cooling[model] = now + 9_999_999
                _note(model, "not found")
                logger.info("Model %s not found — trying next", model)
                continue
            if "403" in err_str or "PERMISSION_DENIED" in err_str:
                # A model Google keeps for other projects (the 2.5 family is
                # limited to past users).  Not an error in the request: the
                # next model may well answer.
                cooling[model] = now + 24 * 3600
                _note(model, "not open to this project")
                logger.info("Model %s not open to this project — trying next", model)
                continue
            # A different error will not be cured by another model — stop
            # rather than burn the rest of the chain on it.
            _note(model, "error: " + err_str.split("\n", 1)[0][:160])
            logger.exception("Unexpected Gemini error from %s", model)
            break
    return Result(None, None, last_err if asked else "ALL_COOLING")


def failure_message(last_err: str) -> str:
    """The most useful thing to tell a user when no model answered."""
    if last_err == "NOT_CONFIGURED":
        return "⚠️ **Gemini is not configured.**  Add `GEMINI_API_KEY` to the secrets."
    if last_err == "ALL_COOLING":
        return ("⚠️ **Every model refused recently and is resting** (rate limit or no "
                "quota).  Nothing was asked this time; try again in a few minutes.")
    if "limit: 0" in last_err:
        return (
            "⚠️ **Your Google AI Studio account has no free-tier quota** "
            "for the available models (`limit: 0` on every fallback).  "
            "Common for new EU/UK accounts: the free tier exists, but "
            "needs billing enabled on the Google Cloud project to be "
            "unlocked.  Linking a card does **NOT** charge you under "
            "the free tier — it just unlocks the quota.\n\n"
            "Fix: [console.cloud.google.com/billing]"
            "(https://console.cloud.google.com/billing)."
        )
    if "RESOURCE_EXHAUSTED" in last_err or "429" in last_err:
        return (
            "⚠️ **All fallback models hit their rate limit.**  Wait "
            "a minute and try again — RPM resets every 60 seconds, "
            "RPD resets at midnight UTC."
        )
    if ("503" in last_err or "UNAVAILABLE" in last_err
            or "overload" in last_err.lower()):
        return (
            "⚠️ **Gemini is temporarily overloaded.**  All fallback "
            "models reported high demand on the free tier.  Wait "
            "30-60 seconds and try again — this is server-side and "
            "usually clears in under a minute."
        )
    if "404" in last_err or "NOT_FOUND" in last_err:
        return (
            "⚠️ **No usable Gemini model found.**  Set `GEMINI_MODEL` "
            "in `secrets.toml` to a valid one (e.g. `gemini-2.5-flash`)."
        )
    short = (last_err or "no error captured").split("\n", 1)[0][:240]
    return f"⚠️ Error from Gemini: `{short}`"

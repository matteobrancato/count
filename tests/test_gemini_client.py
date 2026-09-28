"""The Gemini fallback policy Dexter runs on.

No network: Gemini is replaced by a fake.  What is locked here is which model
is asked and when one is skipped — the behaviour that keeps Dexter answering
on the free tier when Google refuses a model.
"""
from __future__ import annotations

import pytest

from src import gemini_client as gc


# ── the shared fallback policy ───────────────────────────────────────────────
class _FakeModels:
    def __init__(self, behaviour: dict):
        self.behaviour = behaviour
        self.calls: list[str] = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        outcome = self.behaviour[model]
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {"text": outcome})()


@pytest.fixture
def fake_gemini(monkeypatch):
    def install(behaviour: dict) -> _FakeModels:
        models = _FakeModels(behaviour)
        monkeypatch.setattr(gc, "AVAILABLE", True)
        monkeypatch.setattr(gc, "api_key", lambda: "test-key")
        monkeypatch.setattr(gc, "client", lambda key: type("C", (), {"models": models})())
        return models
    return install


class TestFallbackPolicy:
    CHAIN = ("best", "good", "last")

    def test_the_best_model_answers_first(self, fake_gemini):
        m = fake_gemini({"best": " ok ", "good": "no", "last": "no"})
        res = gc.generate([], None, self.CHAIN, {})
        assert (res.model, res.text, m.calls) == ("best", "ok", ["best"])

    def test_a_rate_limited_model_is_skipped_for_googles_retry_delay(self, fake_gemini):
        m = fake_gemini({"best": Exception("429 RESOURCE_EXHAUSTED retryDelay: '16s'"),
                         "good": "ok", "last": "no"})
        cooling: dict = {}
        res = gc.generate([], None, self.CHAIN, cooling)
        assert res.model == "good" and m.calls == ["best", "good"]
        assert 10 < cooling["best"] - gc.time.time() <= 16

    def test_no_quota_skips_the_model_for_a_day(self, fake_gemini):
        fake_gemini({"best": Exception("429 quota limit: 0"), "good": "ok", "last": "no"})
        cooling: dict = {}
        gc.generate([], None, self.CHAIN, cooling)
        assert cooling["best"] - gc.time.time() > 23 * 3600

    def test_overload_and_not_found_step_down_too(self, fake_gemini):
        m = fake_gemini({"best": Exception("503 UNAVAILABLE"),
                         "good": Exception("404 NOT_FOUND"), "last": "ok"})
        res = gc.generate([], None, self.CHAIN, {})
        assert res.model == "last" and m.calls == list(self.CHAIN)

    def test_a_real_error_stops_instead_of_burning_the_chain(self, fake_gemini):
        m = fake_gemini({"best": Exception("400 INVALID_ARGUMENT file too large"),
                         "good": "ok", "last": "ok"})
        res = gc.generate([], None, self.CHAIN, {})
        assert res.model is None and m.calls == ["best"]
        assert "INVALID_ARGUMENT" in gc.failure_message(res.error)

    def test_a_model_still_cooling_down_is_not_even_asked(self, fake_gemini):
        m = fake_gemini({"best": "no", "good": "ok", "last": "no"})
        res = gc.generate([], None, self.CHAIN, {"best": gc.time.time() + 60})
        assert res.model == "good" and m.calls == ["good"]

    def test_not_configured_says_so(self, monkeypatch):
        monkeypatch.setattr(gc, "api_key", lambda: None)
        res = gc.generate([], None, self.CHAIN, {})
        assert res.model is None and "GEMINI_API_KEY" in gc.failure_message(res.error)


class TestDexterUsesThePolicy:
    """Dexter's behaviour must not change: same chain, same retry parsing, and
    its reply path goes through generate()."""

    def test_dexters_chain_is_unchanged(self):
        from src.ui import chat_assistant as ca
        assert ca._FALLBACK_CHAIN == ["gemini-2.5-flash", "gemini-2.5-flash-lite",
                                      "gemini-2.0-flash"]

    def test_dexter_uses_the_shared_implementation(self):
        import inspect

        from src.ui import chat_assistant as ca
        # No private copy of the retry parsing may survive in Dexter: two copies
        # of a policy drift the first time one of them is fixed.
        assert "parse_retry_delay" not in inspect.getsource(ca)
        src = inspect.getsource(ca._generate_pending_response)
        assert "gemini_client.generate(" in src
        assert "gemini_client.failure_message(" in src

    def test_dexter_remembers_refusals_in_the_shared_table(self):
        import inspect

        from src.ui import chat_assistant as ca
        assert "gemini_client.COOLDOWN_KEY" in inspect.getsource(ca._generate_pending_response)

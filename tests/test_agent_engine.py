"""AgentEngine: fake-model unit tests + a papertrade integration run (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from rhagent.engine import AgentEngine, Decision
from rhagent.papertrade import PaperTrader


def _hist(closes):
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes},
        index=pd.date_range("2024-01-01", periods=len(closes), freq="D"),
    )


def test_targets_and_reason_carried():
    hist = _hist([10, 11, 12, 13, 14, 15, 16])
    for tgt in (-1, 0, 1):
        fake = lambda p, t=tgt: json.dumps({"target": t, "reason": f"go {t}"})
        # allow_short=True so the -1 target carries through unclamped (default is
        # now long-only); this test checks target/reason plumbing, not clamping.
        d = AgentEngine(complete=fake, allow_short=True).decide("NVDA", hist, 0.0)
        assert isinstance(d, Decision)
        assert d.target == float(tgt)
        assert f"go {tgt}" in d.reason


def test_allow_short_clamps():
    hist = _hist([10, 11, 12])
    fake = lambda p: json.dumps({"target": -1, "reason": "short"})
    assert AgentEngine(complete=fake, allow_short=False).decide("X", hist, 0.0).target == 0.0
    assert AgentEngine(complete=fake, allow_short=True).decide("X", hist, 0.0).target == -1.0


def test_parse_fail_holds_current_pos():
    hist = _hist([10, 11, 12])
    d = AgentEngine(complete=lambda p: "not json at all").decide("X", hist, 1.0)
    assert d.target == 1.0
    assert "parse-fail" in d.reason
    assert "ValueError" in d.reason  # names the real exception, not just "parse-fail"


def test_default_complete_uses_configured_max_tokens(monkeypatch):
    """AgentEngine() with no explicit max_tokens must reach the API with
    cfg.agent.max_tokens (config.yaml has 16000) -- not a hardcoded default
    that silently ignores config.yaml."""
    import openai

    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            msg = SimpleNamespace(content='{"target": 1, "reason": "ok"}')
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeClient)

    hist = _hist([10, 11, 12])
    d = AgentEngine().decide("X", hist, 0.0)  # complete=None -> lazy nvidia client

    assert d.target == 1.0
    assert captured["max_tokens"] == 16000  # from config.yaml, not a hardcoded 256


def test_json_extraction_prefers_last_brace_span():
    """A reasoning model can echo the prompt's own example braces in its
    chain-of-thought before the real answer; extraction must not choke on
    prose sitting between an earlier stray '{...}' and the final one."""
    hist = _hist([10, 11, 12])
    raw = (
        'Reminder: reply as {"target": -1 | 0 | 1, "reason": "..."}. '
        'Thinking it over... {"target": 1, "reason": "final answer"}'
    )
    d = AgentEngine(complete=lambda p: raw).decide("X", hist, 0.0)
    assert d.target == 1.0
    assert "final answer" in d.reason


def _rate_limit_error():
    import httpx
    from openai import RateLimitError

    req = httpx.Request("POST", "https://x/y")
    resp = httpx.Response(429, request=req, json={"error": {"message": "slow down"}})
    return RateLimitError("slow down", response=resp, body={"error": {"message": "slow down"}})


def test_rate_limit_retries_then_succeeds_without_real_sleep():
    calls = {"n": 0}

    def flaky(p):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _rate_limit_error()
        return json.dumps({"target": 1, "reason": "ok"})

    slept = []
    hist = _hist([10, 11, 12])
    d = AgentEngine(complete=flaky, sleep=slept.append).decide("X", hist, 0.0)
    assert d.target == 1.0
    assert calls["n"] == 3          # two failures + the retry that succeeded
    assert slept                    # backoff was invoked, just not a real sleep


def test_rate_limit_exhausted_is_distinguishable_and_holds():
    def always_429(p):
        raise _rate_limit_error()

    hist = _hist([10, 11, 12])
    d = AgentEngine(complete=always_429, sleep=lambda s: None).decide("X", hist, 1.0)
    assert d.target == 1.0          # falls back to holding current_pos
    assert "rate-limited" in d.reason


def test_timeout_is_distinguishable():
    import httpx
    from openai import APITimeoutError

    def times_out(p):
        raise APITimeoutError(httpx.Request("POST", "https://x/y"))

    hist = _hist([10, 11, 12])
    d = AgentEngine(complete=times_out, sleep=lambda s: None).decide("X", hist, 1.0)
    assert d.target == 1.0
    assert "timeout" in d.reason


class _Source:
    def __init__(self, frames):
        self._frames = frames

    def bars(self):
        return self._frames


def test_integration_trades_written(tmp_path):
    idx = pd.date_range("2024-01-01", periods=4, freq="D")
    frames = {
        "AAA": pd.DataFrame(
            {"open": [10, 11, 12, 13], "high": [10, 11, 12, 13],
             "low": [10, 11, 12, 13], "close": [10, 11, 12, 13]}, index=idx),
        "BBB": pd.DataFrame(
            {"open": [20, 19, 18, 17], "high": [20, 19, 18, 17],
             "low": [20, 19, 18, 17], "close": [20, 19, 18, 17]}, index=idx),
    }
    fake = lambda p: json.dumps({"target": 1, "reason": "long it"})
    trader = PaperTrader(
        engine=AgentEngine(complete=fake, sleep=lambda s: None), source=_Source(frames),
        out_dir=tmp_path,
    )
    run_dir = trader.run()

    lines = (run_dir / "trades.jsonl").read_text().splitlines()
    assert lines
    for line in lines:
        assert json.loads(line)["trade_id"]

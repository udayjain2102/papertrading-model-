"""Interactive dashboard server: signals, config, and a real (mock-broker) run.

Self-contained: seeds a tiny synthetic price cache + config so it never touches
the gitignored data/ or journal/.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dashboard_server as ds  # noqa: E402


def _seed(tmp: Path) -> tuple[Path, Path, Path]:
    data = tmp / "data"
    data.mkdir()
    idx = pd.date_range("2024-01-01", periods=60, freq="D")
    for sym, base in [("AAA", 100.0), ("BBB", 50.0)]:
        close = base + pd.Series(range(60)).mul(0.1).values
        close[-1] = base  # last bar dips back → a live signal to report
        pd.DataFrame({"date": idx, "open": close, "high": close, "low": close,
                      "close": close, "volume": 1e6}).to_csv(data / f"{sym}.csv", index=False)
    cfg = tmp / "config.yaml"
    cfg.write_text(
        "strategy:\n  name: mean_reversion\n  params: {}\n"
        "  overlay: conviction\n  universe: [AAA, BBB]\n")
    base_dir = tmp / "papertrade"
    base_dir.mkdir()
    return cfg, data, base_dir


@pytest.fixture()
def server(tmp_path):
    cfg, data, base_dir = _seed(tmp_path)
    ds.Handler.config_path = cfg
    ds.Handler.data_dir = data
    ds.Handler.base_dir = base_dir
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ds.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read().decode()


def _post(url, body, ctype="application/json"):
    req = urllib.request.Request(url, data=body.encode(),
                                 headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_signals(server):
    status, body = _get(server + "/api/signals")
    assert status == 200
    rows = json.loads(body)
    syms = {r["symbol"] for r in rows}
    assert syms == {"AAA", "BBB"}
    assert all("zscore" in r for r in rows)  # mean_reversion exposes entry


def test_config_read_and_bad_write(server):
    status, body = _get(server + "/api/config")
    assert status == 200 and "mean_reversion" in body
    status, body = _post(server + "/api/config", "key: [unclosed")
    assert status == 400 and "error" in json.loads(body)


def test_run_streams_and_is_serialized(server):
    # agent under MOCK_AGENT=true: no API key, flows through the real pipeline.
    import os
    os.environ["MOCK_AGENT"] = "true"
    status, body = _post(server + "/api/run", json.dumps({"action": "agent"}))
    assert status == 200, body

    # a second run while the first is active → 409
    s2, _ = _post(server + "/api/run", json.dumps({"action": "agent"}))
    assert s2 == 409

    off, grew, done, rc = 0, False, False, None
    for _ in range(100):
        _, lb = _get(server + f"/api/logs?offset={off}")
        log = json.loads(lb)
        if log["chunk"]:
            grew = True
        off = log["offset"]
        if log["done"]:
            done, rc = True, log["rc"]
            break
        time.sleep(0.2)
    assert done and grew and rc == 0

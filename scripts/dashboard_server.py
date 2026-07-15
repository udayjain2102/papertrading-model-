"""Interactive dashboard server: the static dashboard, plus buttons that run it.

The static `make_dashboard.py` only reflects state. This wraps its rendered HTML
in a live control layer served by stdlib http.server -- no new dependencies:

    PYTHONPATH=src python scripts/dashboard_server.py     # localhost:8765

You can trigger the system's cached-data runs (paper-trade, forward tick,
compare/bake-off, agent session), watch their stdout stream, see a per-symbol
signal table computed from the same strategy code the engine uses, read the
live account snapshot, and edit config.yaml.

What it deliberately can't do: refresh the price cache or fetch live account
data -- both need the Robinhood MCP token, which lives in the Claude MCP loop,
not here. Account/positions is shown from `journal/account.json`, a snapshot the
MCP loop writes; the server only reads it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

import make_dashboard  # noqa: E402  (sibling script)
from rhagent.data import get_bars  # noqa: E402
from rhagent.strategies import build  # noqa: E402

# ── run actions → existing CLIs (repo root, PYTHONPATH=src) ─────────────────
# Only cached-data / .env-key runs; nothing here needs the MCP.

def _papertrade_cmd(p: dict) -> list[str]:
    engine = str(p.get("engine", "mean_reversion"))
    symbols = str(p.get("symbols", "all"))
    days = str(int(p.get("days", 400)))
    return [sys.executable, "-m", "rhagent.papertrade",
            "--engine", engine, "--symbols", symbols, "--days", days]


ACTIONS = {
    "papertrade": _papertrade_cmd,
    "forward": lambda p: [sys.executable, "-m", "rhagent.forward"],
    "bakeoff": lambda p: [sys.executable, "-m", "rhagent.compare"],
    "agent": lambda p: [sys.executable, "-m", "rhagent.runner"],
}


class Job:
    """One running CLI, stdout+stderr merged into a temp file we tail by offset.

    ponytail: a single global job slot (one local user) -- POST /api/run 409s
    while one is active. Swap for a keyed dict + queue if concurrent runs matter.
    """

    _lock = threading.Lock()
    current: "Job | None" = None

    def __init__(self, action: str, cmd: list[str], data_dir: Path):
        self.action = action
        self.log_path = Path(tempfile.mkstemp(prefix="rhdash-", suffix=".log")[1])
        self._fh = open(self.log_path, "w")
        env = {"PYTHONPATH": str(ROOT / "src")}
        import os
        env = {**os.environ, **env}
        self.proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=self._fh, stderr=subprocess.STDOUT,
            env=env, text=True,
        )

    @classmethod
    def start(cls, action: str, params: dict, data_dir: Path) -> "Job | None":
        with cls._lock:
            if cls.current and cls.current.proc.poll() is None:
                return None
            cmd = ACTIONS[action](params)
            cls.current = Job(action, cmd, data_dir)
            return cls.current

    def read_from(self, offset: int) -> tuple[str, int, bool, int | None]:
        text = self.log_path.read_text(errors="replace")
        chunk = text[offset:]
        rc = self.proc.poll()
        return chunk, len(text), rc is not None, rc


# ── per-symbol signal table (same strategy code the engine uses) ────────────

def compute_signals(config_path: Path, data_dir: Path) -> list[dict]:
    # only the strategy block matters here -- don't couple to the full config
    # schema (limits/agent), which live-signal display has no use for.
    strat_cfg = (yaml.safe_load(config_path.read_text()) or {}).get("strategy")
    if not strat_cfg:
        return []
    strat = build(strat_cfg["name"], strat_cfg.get("params") or {})
    universe = strat_cfg.get("universe", [])
    entry = getattr(strat, "entry", None)
    # cached-only: every CSV exists, so get_bars never hits the MCP.
    bars = get_bars(universe, "", "", cache_dir=str(data_dir))
    rows = []
    for sym in universe:
        df = bars.get(sym)
        if df is None or len(df) < getattr(strat, "lookback", 20) + 1:
            rows.append({"symbol": sym, "status": "insufficient bars"})
            continue
        score = float(strat.signal(df).iloc[-1])
        pos = float(strat.target(df))
        # mean_reversion's signal is -z, so z = -score. entry fires when z < -entry
        # i.e. score > entry; distance>0 means the long trigger is met.
        row = {
            "symbol": sym,
            "score": round(score, 3),
            "position": "long" if pos > 0 else ("short" if pos < 0 else "flat"),
        }
        if entry is not None:
            row["zscore"] = round(-score, 3)
            row["dist_to_entry"] = round(score - float(entry), 3)
        rows.append(row)
    # closest to (or past) a long entry first; unknown-status rows sink.
    rows.sort(key=lambda r: r.get("score", -1e9), reverse=True)
    return rows


# ── page: static body + interactive layer ──────────────────────────────────

def render_page(base_dir: Path) -> str:
    try:
        body = make_dashboard.render_all(base_dir)
    except SystemExit:
        body = make_dashboard._page(
            "Interactive dashboard",
            "<p class='sub'>No paper-trade runs yet. Use the controls above to "
            "start one.</p>", "rhagent interactive dashboard")
    # render_all returns a full <html>; inject our control layer after <h1>.
    inject = _CONTROLS + _PANELS
    body = body.replace("</h1>", "</h1>\n" + inject, 1)
    return body.replace("</body>", _SCRIPT + "</body>")


_CONTROLS = """
<div id="ctl">
  <div class="ctlrow">
    <select id="pt-engine">
      <option>mean_reversion</option><option>momentum</option>
      <option>linreg</option><option>pairs</option>
    </select>
    <input id="pt-symbols" value="all" size="8" title="symbols (or 'all')">
    <input id="pt-days" value="400" size="5" title="days">
    <button onclick="run('papertrade')">Paper-trade</button>
    <button onclick="run('forward')">Forward tick</button>
    <button onclick="run('bakeoff')">Bake-off</button>
    <button onclick="run('agent')">Agent session</button>
    <span id="jobstat" class="sub"></span>
  </div>
  <pre id="console" hidden></pre>
</div>
"""

_PANELS = """
<div id="panels">
  <div class="panel"><h2>Account snapshot</h2><div id="account" class="sub">loading…</div></div>
  <div class="panel"><h2>Config (config.yaml)</h2>
    <textarea id="cfg" spellcheck="false"></textarea>
    <div><button onclick="saveCfg()">Save config</button>
      <span id="cfgstat" class="sub"></span></div></div>
  <div class="panel"><h2>Live signals</h2>
    <div class="tblscroll"><div id="signals" class="sub">loading…</div></div></div>
</div>
<style>
#ctl{margin:8px 0 20px}
.ctlrow{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
#ctl button,.panel button{background:var(--accent);color:#fff;border:0;border-radius:6px;
  padding:6px 12px;cursor:pointer;font-size:13px}
#ctl select,#ctl input{background:var(--panel);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:5px 8px}
#console{background:#000;color:#3fb950;padding:12px;border-radius:6px;max-height:280px;
  overflow:auto;white-space:pre-wrap;margin-top:10px;font:12px/1.45 ui-monospace,monospace}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:14px 16px;margin:14px 0}
#cfg{width:100%;height:180px;background:#0b0e12;color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:10px;font:12px/1.45 ui-monospace,monospace;margin-bottom:8px}
#signals table{border-collapse:collapse;width:100%}
#signals td,#signals th{padding:3px 10px;border-bottom:1px solid var(--line);text-align:right}
#signals td:first-child,#signals th:first-child{text-align:left}
</style>
"""

_SCRIPT = """
<script>
let poll=null;
async function run(action){
  const params={engine:pt_engine.value,symbols:pt_symbols.value,days:pt_days.value};
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action,params})});
  if(r.status===409){jobstat.textContent='a run is already active';return;}
  const {job_id}=await r.json();
  console.hidden=false;console.textContent='';jobstat.textContent=action+' running…';
  let off=0;clearInterval(poll);
  poll=setInterval(async()=>{
    const l=await(await fetch('/api/logs?offset='+off)).json();
    if(l.chunk){console.textContent+=l.chunk;off=l.offset;console.scrollTop=console.scrollHeight;}
    if(l.done){clearInterval(poll);jobstat.textContent=action+' finished (rc='+l.rc+')';
      loadSignals();refreshTables();}
  },600);
}
async function refreshTables(){
  const html=await(await fetch('/')).text();
  const doc=new DOMParser().parseFromString(html,'text/html');
  for(const id of ['allruns']){
    const fresh=doc.getElementById(id);const cur=document.getElementById(id);
    if(fresh&&cur)cur.replaceWith(fresh);
  }
}
async function loadAccount(){
  const a=await(await fetch('/api/account')).json();
  if(!a.exists){account.innerHTML="No snapshot yet — the Claude MCP loop writes "
    +"<code>journal/account.json</code>.";return;}
  const d=a.data;
  account.innerHTML="<b>as of "+a.as_of+"</b><br>buying power $"+(d.buying_power_usd??'?')
    +" · deployed $"+(d.total_position_value_usd??'?')
    +" · realized P&L today $"+(d.realized_pnl_today_usd??'?')
    +"<br>positions: "+(d.positions?Object.entries(d.positions).map(([k,v])=>k+' $'+v).join(', '):'—');
}
async function loadCfg(){cfg.value=await(await fetch('/api/config')).text();}
async function saveCfg(){
  const r=await fetch('/api/config',{method:'POST',body:cfg.value});
  const j=await r.json();cfgstat.textContent=r.ok?'saved':('error: '+j.error);
  if(r.ok)loadSignals();
}
async function loadSignals(){
  const rows=await(await fetch('/api/signals')).json();
  let h="<table><tr><th>symbol</th><th>z</th><th>score</th><th>dist→entry</th><th>pos</th></tr>";
  for(const r of rows){h+="<tr><td>"+r.symbol+"</td><td>"+(r.zscore??'')+"</td><td>"
    +(r.score??r.status??'')+"</td><td>"+(r.dist_to_entry??'')+"</td><td>"+(r.position??'')+"</td></tr>";}
  signals.innerHTML=h+"</table>";
}
loadAccount();loadCfg();loadSignals();
</script>
"""


# ── HTTP ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    config_path = ROOT / "config.yaml"
    data_dir = ROOT / "data"
    base_dir = ROOT / "journal" / "papertrade"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, render_page(self.base_dir).encode(), "text/html; charset=utf-8")
        elif u.path == "/api/signals":
            self._json(compute_signals(self.config_path, self.data_dir))
        elif u.path == "/api/account":
            self._json(self._account())
        elif u.path == "/api/config":
            self._send(200, self.config_path.read_bytes(), "text/plain; charset=utf-8")
        elif u.path == "/api/logs":
            self._logs(parse_qs(u.query))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        if u.path == "/api/config":
            try:
                yaml.safe_load(raw.decode())
            except yaml.YAMLError as e:
                return self._json({"error": str(e)}, 400)
            self.config_path.write_bytes(raw)
            self._json({"ok": True})
        elif u.path == "/api/run":
            req = json.loads(raw or b"{}")
            action = req.get("action")
            if action not in ACTIONS:
                return self._json({"error": f"unknown action {action}"}, 400)
            job = Job.start(action, req.get("params", {}), self.data_dir)
            if job is None:
                return self._json({"error": "a run is already active"}, 409)
            self._json({"job_id": job.action})
        else:
            self._json({"error": "not found"}, 404)

    def _logs(self, q):
        job = Job.current
        if job is None:
            return self._json({"chunk": "", "offset": 0, "done": True, "rc": None})
        offset = int(q.get("offset", ["0"])[0])
        chunk, new_off, done, rc = job.read_from(offset)
        self._json({"chunk": chunk, "offset": new_off, "done": done, "rc": rc})

    def _account(self):
        # Snapshot shape (written by the Claude MCP loop, read-only here):
        # {"buying_power_usd", "total_position_value_usd",
        #  "realized_pnl_today_usd", "positions": {symbol: value_usd}}
        # -- i.e. the raw Robinhood MCP get_account payload.
        p = ROOT / "journal" / "account.json"
        if not p.exists():
            return {"exists": False}
        as_of = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        return {"exists": True, "as_of": as_of.strftime("%Y-%m-%d %H:%M UTC"),
                "data": json.loads(p.read_text())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dashboard_server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--base-dir", default=str(ROOT / "journal" / "papertrade"))
    args = ap.parse_args(argv)
    Handler.config_path = Path(args.config)
    Handler.data_dir = Path(args.data_dir)
    Handler.base_dir = Path(args.base_dir)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"dashboard: http://127.0.0.1:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

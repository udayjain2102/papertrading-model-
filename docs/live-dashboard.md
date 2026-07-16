# Live dashboard — see every loop and every score as it happens

## The gap

`scripts/make_dashboard.py` is a *post-mortem* view: it reads finished ledgers
under `journal/papertrade/` and `journal/forward/` and renders completed runs.
While a run is in flight you see nothing — not which symbol is being scored,
not what the gate decided, not why a decision came out flat. This doc specifies
the live layer: what's on screen, where each number comes from, and how it
updates.

## How it works (one sentence)

Every stage of the system appends one JSON line to `journal/events.jsonl` as it
happens; the dashboard regenerates from that file and auto-refreshes in the
browser every few seconds.

No server, no websockets, no database. The pieces:

1. **Emit** — a ~10-line `emit(kind, **fields)` helper in `rhagent/journal.py`
   that appends `{"ts": ..., "kind": ..., ...}` to `journal/events.jsonl`.
   Call sites: `forward.py` (tick loop), `gate/gate.py` (per-config scoring),
   `papertrade.py` (per-symbol replay), `runner.py` (cron tick), `refresh.py`
   (cache refresh). Append-only, one line per event — crash-safe and
   `tail -f`-able.
2. **Render** — `make_dashboard.py --live` adds a "Now" section built from the
   last N events, and injects `<meta http-equiv="refresh" content="5">` so the
   open browser tab re-pulls the file.
3. **Regenerate** — while a run is active, a `watch`-style loop rebuilds the
   HTML whenever `events.jsonl` grows:
   `while sleep 3; do python scripts/make_dashboard.py --live; done`
   (or a `--watch` flag doing the same with `os.stat` mtime polling).

So "live" = event log + regenerate + browser meta-refresh. Latency is a few
seconds, which matches a system whose fastest loop is one decision per bar.

## Event vocabulary

Each line is `{"ts": ISO-8601, "kind": ..., ...}`. The kinds map 1:1 to what
you'd want to watch scroll by:

| kind | emitted by | payload |
|---|---|---|
| `loop_start` / `loop_end` | forward.py, runner.py | run_id, engine, universe size; end adds duration + summary |
| `refresh` | refresh.py | symbols fetched, days, cache hits vs MCP calls (NVIDIA-style rate limits make this worth watching) |
| `score` | gate/gate.py, search loop | strategy, params, is_icir, oos_icir, bonf_p, dsr, viable, reason |
| `decision` | forward.py `_agent_positions`, engine | symbol, prev position → target, reason text, cached-or-fresh |
| `order` / `fill` | executor.py, broker | symbol, side, qty, price, broker (mock/mcp) |
| `tick_result` | forward.py | date, net return for the day, cumulative equity |
| `halt` / `error` | guardrails.py, runner.py | reason (kill switch, rate limit, exception) |

## What's on the page

Top to bottom — the existing dashboard sections stay; the live block goes first.

### 1. Status bar (always visible, answers "is it alive?")
- Last event age ("12s ago" green / "3h ago" red — reuse the existing
  `_status_class` freshness logic).
- Current phase: refreshing / scoring / deciding / idle.
- HALT indicator (kill-switch file present?), broker in use (mock vs MCP),
  rate-limit budget consumed this loop.

### 2. Live loop feed
Reverse-chronological table of the last ~200 events, colored by kind
(decisions blue, fills green/red, errors red). This is the "watch it think"
view — every `decision` row shows the symbol, old→new position, and the
agent's reason string, so you can literally read each call as it lands.

### 3. Scoring board (the "each scoring" view)
One row per config the gate/search scored in the current loop, straight from
`score` events — the same columns `GateRow` already has: params, in-sample
ICIR, out-of-sample ICIR, Bonferroni p vs threshold, deflated Sharpe,
**viable yes/no with the failure reason**. Sorted viable-first. A counter tile
("scored 128 · viable 0") makes the gate's verdict impossible to miss.

### 4. Positions & today's decisions
Per-symbol grid for the universe: current target position, yesterday's, the
last decision reason, and whether it came from the pos-cache or a fresh agent
call. This is exactly the data `pos_<sym>.csv` files hold — the live page just
shows the last row of each.

### 5. Equity tick-by-tick
The existing SVG equity curve, but for the forward record with the newest
`tick_result` appended — plus a "today" tile: today's net return in $ and %.

### 6. Everything already there
Forward track records, research-run comparison, bake-off table, per-run
ledgers/failure buckets — unchanged, below the live block.

## Deliberate non-features

- **No web server / websockets** — meta-refresh on a static file is one line
  and survives reboots; add a server only if 5s latency ever actually hurts.
- **No event schema/versioning** — it's a local jsonl the dashboard alone
  reads; renderer skips unknown kinds.
- **No retention policy yet** — at ~1 loop/day the file grows by a few KB/day.
  Rotate when it's ever slow to parse, not before.
- **No separate live page** — one `dashboard.html`, live section on top, so
  there is still exactly one place to look.

## Build order

1. `emit()` in `journal.py` + call sites in `forward.py` (loop, decision,
   tick_result) — immediately makes `tail -f journal/events.jsonl` a usable
   dashboard on its own.
2. `--live` section + meta-refresh in `make_dashboard.py`.
3. `score` events from the gate, scoring-board section.
4. `--watch` regeneration loop (until then: rerun the script by hand).

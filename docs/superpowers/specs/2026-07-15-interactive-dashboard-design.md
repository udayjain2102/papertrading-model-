# Interactive dashboard — design

## Goal

Turn the read-only static dashboard (`scripts/make_dashboard.py`) into a live
control panel: trigger the system's runs from the page, watch their output
stream, and see state that isn't rendered today (account snapshot, per-symbol
signals, editable config).

## Constraints (from the codebase)

- **Cached-data runs work locally; MCP runs do not.** Paper-trade, forward
  tick, the compare/bake-off, and the agent session all run off the cached
  CSVs (`data/`) and/or the NVIDIA key in `.env`. Refreshing the price cache and
  fetching live account data need the Robinhood MCP (`ROBINHOOD_MCP_TOKEN`),
  which is not present locally — that path stays in the Claude MCP loop.
- Therefore: **no "refresh price cache" button.** Account/positions is shown
  from a **snapshot file** written by the MCP loop, not fetched by the server.
- The static `make_dashboard.py` must keep working unchanged (CI / paper-cron
  publish `journal/dashboard.html` from it). The server reuses its renderers.

## Architecture

One new file: **`scripts/dashboard_server.py`**, stdlib `http.server`
(`ThreadingHTTPServer`) — **no new dependencies**. Run from repo root:

```
PYTHONPATH=src python scripts/dashboard_server.py   # serves localhost:8765
```

It imports `make_dashboard` for the data-table HTML and adds an interactive
layer (control bar, log console, account + signals panels, config editor) plus
a small JSON API. Localhost-only; no auth.

### Endpoints

| Method + path        | Does                                                          |
| -------------------- | ------------------------------------------------------------ |
| `GET /`              | Full page: `make_dashboard.render_all` body + control layer  |
| `GET /api/signals`   | Per-symbol z-score / position / distance to entry-exit (JSON)|
| `GET /api/account`   | `journal/account.json` snapshot + its mtime (JSON)           |
| `GET /api/config`    | Raw `config.yaml` text                                       |
| `POST /api/config`   | Write `config.yaml` (yaml.safe_load parse-check first)       |
| `POST /api/run`      | Start one run `{action, params}` → `{job_id}`                |
| `GET /api/logs?job=&offset=` | New stdout bytes since `offset` + `done` flag        |

### Run actions → existing CLIs (nothing reimplemented)

| action      | command (cwd = repo root, `PYTHONPATH=src`)                      |
| ----------- | --------------------------------------------------------------- |
| `papertrade`| `python -m rhagent.papertrade --engine <e> --symbols <s> --days <n>` |
| `forward`   | `python -m rhagent.forward`                                     |
| `bakeoff`   | `python -m rhagent.compare`                                     |
| `agent`     | `python -m rhagent.runner` (real NVIDIA; local broker is the mock account) |

### Job model

**One run at a time — a single global job slot** (a module-level lock + current
job). A `POST /api/run` while a job is active returns `409`. Each job runs the
CLI via `subprocess.Popen` with stdout+stderr merged into a temp log file under
`$TMPDIR`; `/api/logs` tails that file by byte offset (poll, no websockets).
`done` is set when the process exits, carrying the return code.

`ponytail:` global single-job slot — fine for one local user; swap for a keyed
job dict + queue if concurrent runs are ever needed.

### Panels (client-side JS, vanilla, inlined)

- **Control bar** — action buttons; paper-trade has engine/symbols/days inputs.
  On click → `POST /api/run`, then poll `/api/logs` into the **log console**.
  On `done`, reload the data tables via `GET /` (swap `#allruns` + bake-off
  section) so new results show without a manual refresh.
- **Account** — polls `/api/account`; shows "as of <mtime>", buying power,
  deployed, positions, realized P&L. If the file is missing: a note that the
  Claude MCP loop hasn't written a snapshot yet.
- **Signals** — table of the 65 names from `/api/signals`: z-score, in-trade vs
  flat, distance to entry/exit, sorted by proximity to a signal.
- **Config editor** — textarea of `config.yaml`; Save → `POST /api/config`;
  server rejects unparseable YAML with the error message.

### Signals computation (server-side, reuses engine code)

`/api/signals` loads `config.yaml`, resolves the configured strategy from the
registry, loads each universe symbol's cached bars (`rhagent.data`), and calls
the strategy to get the current z-score and target position. Distance-to-signal
is derived from the strategy's entry/exit thresholds. Uses the same code the
engine uses, so numbers match the live decision path. Symbols with too little
history are listed as "insufficient bars", not errored.

## Testing / verification

`tests/test_dashboard_server.py`:

1. Start the server on an ephemeral port in a thread.
2. `GET /api/signals` → 200, JSON list, each row has symbol + zscore.
3. `GET /api/config` → 200 and contains `strategy`; `POST /api/config` with
   broken YAML → 400.
4. `POST /api/run` with a **no-API** action (agent under `MOCK_AGENT=true`, or a
   1-symbol paper-trade over cached data) → `job_id`; poll `/api/logs` until
   `done`; assert the log grew and return code is 0.
5. A second `POST /api/run` while one is active → 409.

Also: `python scripts/make_dashboard.py` still renders the static file
unchanged (existing behavior, spot-checked).

## Out of scope (say the word to add)

Auth, multi-user, concurrent runs, run-history beyond the journal, editing files
other than `config.yaml`, and a live price-cache refresh button (needs the MCP
token).

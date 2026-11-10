# Extreme Autopilot

One brief, autonomous execution in a dedicated Windows VM. Azərbaycan dilində panel, CLI and authenticated API.

**Status: implementation preview, not an accepted production release.** Live OpenAI/Ollama + dedicated Windows VM acceptance and the full 24-hour soak must pass before claiming the complete plan is verified. See [VALIDATION.md](VALIDATION.md).

v0.2.0 includes a regression-backed correctness/recovery audit. See [AUDIT.md](AUDIT.md) for reproduced defects, fixes and limits. Existing installations must stop both services and upgrade the coordinator and worker together; see [upgrade instructions](docs/SETUP.md#upgrading-from-v010).

## What it does

- Accepts one trimmed brief of 200–50,000 Unicode codepoints. No runtime questions or approval inbox.
- Plans verifiable goals, uses local Ollama first and OpenAI for escalation/vision/independent final verification.
- Executes sequential browser, Windows UI, file and bounded process tools in a dedicated worker.
- Uses API/text, elements and keyboard first; mouse is fallback. Observations carry freshness identifiers.
- Journals commands before execution, caches results before acknowledgement and fences uncertain actions after a crash.
- Retains a compact per-goal evidence index across long runs, even after earlier actions leave recent model context.
- Tracks cloud reservations/usage, stops new paid requests at the configured application budget and exposes reports/artifacts.
- Provides the same job lifecycle through the panel, CLI and HTTP API. Default: 24 hours, $5, 2,000 tool operations.
- Uses idempotent job submission and a single active worker-session lease. Uncertain input outcomes prohibit further mutations in that task and are reported as partial.

## Quick start

Python 3.11+ and Node 22.12+ (22/24/26 supported release lines) are required. Install from the source checkout:

```powershell
.\scripts\Install-Coordinator.ps1
```

Follow [the setup guide](docs/SETUP.md) to configure two tokens, model IDs, current pricing and the separate worker VM. Then open [the local panel](http://127.0.0.1:8765). The panel token stays only in page memory.

```powershell
# Use the venv executable directly; shell activation is optional.
.\.venv\Scripts\autopilot.exe --env-file C:\AutopilotData\coordinator.env doctor --models
.\.venv\Scripts\autopilot.exe --env-file C:\AutopilotData\coordinator.env submit --file examples\code.txt
.\.venv\Scripts\autopilot.exe --env-file C:\AutopilotData\coordinator.env jobs
.\.venv\Scripts\autopilot.exe --env-file C:\AutopilotData\coordinator.env status JOB_ID
.\.venv\Scripts\autopilot.exe --env-file C:\AutopilotData\coordinator.env report JOB_ID
```

The frontend build is included in built wheels when `npm run build` is run **before** `python -m build`. Source checkouts must build it once. There are no remote fonts, scripts or CDN assets in the panel.

## API

Coordinator: loopback HTTP port 8765 with `Authorization: Bearer EA_API_TOKEN`. VM gateway: HTTPS port 8766 with a separate `EA_WORKER_TOKEN`; it exposes **only** worker routes.

| Method | Coordinator route | Purpose |
|---|---|---|
| GET | `/api/health` | Worker connectivity |
| GET | `/api/openapi.json` | Machine-readable schema |
| POST | `/api/jobs` | `{ "prompt": "...200+ codepoints...", "budget_usd": 5, "max_seconds": 86400 }` |
| GET | `/api/jobs` | Most recent 100 jobs |
| GET | `/api/jobs/{id}` | State, plan, usage and report |
| POST | `/api/jobs/{id}/cancel` | Stop dispatch and signal cancellation |
| GET | `/api/jobs/{id}/events?after=N` | Cursor-based private event log |
| GET | `/api/jobs/{id}/stream` | Authenticated SSE progress metadata |
| GET | `/api/jobs/{id}/screen` | Latest collected JPEG; not live video |
| GET | `/api/jobs/{id}/report` | Final structured report |
| GET | `/api/jobs/{id}/artifacts/{action_id}` | Captured output file |

States: `queued → running ↔ recovering → completed / partial / blocked / failed / cancelled`.
`POST /api/jobs` accepts an optional `Idempotency-Key` (16–128 ASCII letters/digits/hyphens/underscores). Reuse the exact key, brief and limits to retry a lost response without creating a second job; changed payloads return 409. The panel retains the key while retrying its current submission; a full page reset may discard it, so inspect existing jobs before manually resubmitting. CLI failures print the key for `submit --request-key KEY` retries.

Worker protocol 2 requires `X-Worker-Session` on poll/heartbeat/result calls; the supplied worker handles it automatically. Another live session receives 409. Lease expiry is 45 seconds; an interrupted command remains subject to journal/redelivery checks. `/api/health` returns 503 if the scheduler has stopped. Reports expose pending operations and unsettled billing instead of implying that cancellation undoes completed effects.
Cancellation is cooperative: running process trees are stopped on the next heartbeat (normally ≤3 seconds plus network latency); in-flight UI/library operations may run until their bounded timeout. Already submitted external side effects cannot be undone.

## Development

```powershell
python -m pip install -e ".[dev,worker]"
ruff check .
pytest --cov=autopilot --cov-report=term-missing
cd web
npm ci
npm test
npm run build
cd ..
python -m build
python scripts/soak.py --hours 24 --out C:/AutopilotData/soak.json
```

`tests/test_browser_live.py` is opt-in via `EA_TEST_BROWSER=1` after installing Chromium. It exercises a real browser with a synthetic local website and no paid model. It does not count as live model acceptance.

## Practical boundaries

Arbitrary Python/Node code runs with the **worker OS user's privileges**. Workspace path checks are not an OS sandbox. A dedicated disposable VM is the security boundary; do not run the worker on your everyday desktop. Initial permission quotes and destination checks are defense in depth, not proof against all prompt injection. [SECURITY.md](SECURITY.md) explains the trust model and known limitations.

Only explicit external destinations in the initial brief can be used for external-write intent. Missing permissions, credentials, CAPTCHAs and unresolved obstacles produce partial/blocked reports without asking another question. Model verification reduces false completion; it cannot guarantee semantic correctness.

Cloud cost is calculated with configured per-model prices and provider usage. Missing usage or a lost response is conservatively charged at the reservation. Prices must be kept current; the application budget is not a provider billing guarantee. Local inference still consumes hardware/electricity. No speed or zero-error guarantee is claimed.

Captured individual artifacts are limited to 512 KB; file-write calls to 64 KB. Browser and process outputs are bounded. Large deliverables remain in the VM workspace and require a separately scoped export workflow; v0.1 does not silently upload arbitrary directories.

Protocol references: [OpenAI JSON/structured output](https://developers.openai.com/api/docs/guides/structured-outputs), [Ollama chat](https://docs.ollama.com/api/chat), [Playwright Python](https://playwright.dev/python/docs/intro), [Windows UI Automation via pywinauto](https://pywinauto.readthedocs.io/en/latest/getting_started.html).

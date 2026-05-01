# Current Project Handoff

## Basic Info

- date: 2026-04-30
- branch: `main`
- remote: `origin/main`
- latest pushed commit: `ab85a70 Add gateway cross-day resume scenario`
- current phase: `v0.4-dev / recovery, gateway, subagent quality, and log-analysis foundation`

## Can Development Recover Next Time?

Yes. The current repo can recover in a new place or new session because the important state is now in committed files, tests, and module docs.

To resume elsewhere:

```powershell
git clone https://github.com/chaojixiaoyezi/my-agent.git
cd my-agent
python -m pip install -e .
python -m pytest
```

If the repo already exists:

```powershell
git pull
python -m pytest
```

Fast sanity checks:

```powershell
python scripts\check_doc_sync.py
python -m pytest agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_scenario_gateway_resume.py agent_py_agent\tests\test_cli_reference.py agent_py_agent\tests\test_doc_sync.py
python -m agent_py_agent scenario-test --case gateway-cross-day-resume
```

## What Was Just Completed

- Gateway request/response JSON is now a first-class recovery fact source.
- `memory-resume` reports `gateway_fact_sources`.
- Auto recovery context can include gateway request/response JSON paths.
- `scenario-test --case gateway-cross-day-resume` starts a real background gateway, sends a real `gateway ask`, simulates cross-day archive clues, then verifies `memory-resume` can recover the request/response JSON.
- `scenario-test --case parent-subagent-cross-day-resume` creates a real subagent task, runs a real runner/tool loop with a deterministic backend, simulates cross-day archive clues, then verifies `memory-resume` can recover task fact sources.
- Gateway request worker now continues if processing lease writing fails, so observability file failures do not strand user work in `processing`.
- If LocalStore captured a transient `requests/processing/<id>.json` path, resume prefers terminal `requests/done` or `requests/failed` request files.

## Verified Tests

- `python3 -m pytest -q` -> `251 passed`.
- `python3 scripts/check_doc_sync.py` -> `DOC_SYNC_PASS`.
- `git diff --check` -> passed.
- Focused gateway/memory/doc test set -> `16 passed`.
- Wider memory/gateway focused set -> `76 passed`.
- Parent/subagent runner recovery focused test set -> `2 passed`.
- CLI reference focused test -> `1 passed`.

## Recovery Map

Read these first in a new session:

- `STATUS.md`: broad project status and next-step map.
- `HANDOFF_current-state.md`: this cross-session recovery note.
- `docs/modules/memory/02-progress.md`: memory recovery progress, solved problems, tests, remaining work.
- `docs/modules/subagent/02-progress.md`: subagent runner/workflow progress, solved problems, tests, remaining work.
- `docs/modules/gateway/02-progress.md`: gateway process/recovery progress, solved problems, tests, remaining work.
- `ACCEPTANCE.md`: parent-session acceptance record.
- `EVIDENCE.md`: commands and evidence behind acceptance.

Useful commands:

```powershell
my-agent status
my-agent timeline --limit 20
my-agent memory-resume "继续 gateway 恢复" --json
my-agent scenario-test --case gateway-cross-day-resume
my-agent scenario-test --case parent-subagent-cross-day-resume
```

## Next Version Prep

Recommended next slice:

1. More gateway bad-weather scenarios.
   - Multi request worker.
   - Delayed response.
   - Stop/restart while a request is in processing.
   - Stale lease recovery after worker interruption.

2. Subagent workflow integration.
   - Wire workflow router/compiler/parent gate into real task creation with manual-confirm or dry-run-first policy.
   - Keep the user-facing goal simple; the system should choose templates, contracts, context packs, and worker topology.

3. Log-analysis execution bridge.
   - Add manual-confirm CLI or parent-session command that turns LOG work-order plans into real `SubAgentTask` records.
   - Replace placeholder evidence readers with bounded audited evidence-ref reading before real analyst execution.

4. Real-model parent/subagent recovery smoke.
   - Re-run `parent-subagent-cross-day-resume` or an equivalent manual drill with the configured external model, not only the deterministic scenario backend.

## Remaining Risks

- `STATUS.md` still contains older broad history below the latest recovery section; treat this handoff and module docs as the most current recovery entry.
- Real cross-midnight waiting is not tested; cross-day is simulated with fixed archive timestamps.
- Multi-machine gateway, HTTP/WebSocket gateway, and remote sync are still future work.

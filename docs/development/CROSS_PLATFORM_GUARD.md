# Cross-platform Guard

This project keeps macOS as the primary development path while protecting a
minimum Windows PowerShell path from silent regressions.

## CI Scope

`.github/workflows/cross-platform-guard.yml` runs on:

- `macos-latest`
- `windows-latest`

The guard intentionally runs focused tests instead of the full long-running
suite. It covers process liveness, gateway daemon controls, watchdog behavior,
subagent policy command rendering, and patch-apply test execution.

## Local Windows Check

Run from PowerShell:

```powershell
python -m compileall -q agent_py_agent scripts
python -m pytest agent_py_agent/tests/test_process_control.py agent_py_agent/tests/test_daemon_control.py agent_py_agent/tests/test_supervisor.py agent_py_agent/tests/test_watchdog.py agent_py_agent/tests/test_dispatch_loop.py agent_py_agent/tests/test_policy_checks.py agent_py_agent/tests/test_subagent_policies.py agent_py_agent/tests/test_subagent_manager_core.py -q
python scripts/check_doc_sync.py
python scripts/check_code_size.py --mode warn
git diff --check
```

## Command Rules

- Prefer `python` or the installed `my-agent` console script in user-facing
  commands.
- Avoid requiring `python3` on Windows; it may resolve to the Microsoft Store
  shim instead of the active interpreter.
- Shell helpers under `scripts/*.sh` remain macOS/Linux helpers unless a
  matching PowerShell or Python entrypoint is added.

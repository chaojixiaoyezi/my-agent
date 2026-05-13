# Home Runtime Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the single-user `~/.my-agent` home layout participate in real startup, runs, task workspaces, daily memory, key memory, lessons, and risk-config behavior.

**Architecture:** Keep existing repo-relative storage compatible, then add a home-runtime bridge around it. `SimpleAgent` initializes home paths once, `JsonlMemory` mirrors dialogue to daily files, run finalization creates task workspace refs, and `PromptBuilder` reads lightweight home context without bulk-loading every lesson.

**Tech Stack:** Python standard library, dataclasses, JSONL, pytest, existing config normalizers.

---

### Task 1: Startup Home Bootstrap

**Files:**
- Modify: `agent_py_agent/agent/core.py`
- Modify: `agent_py_agent/agent/user_space/home_layout.py`
- Test: `agent_py_agent/tests/test_home_runtime_bootstrap.py`

- [ ] Add a failing test that `SimpleAgent` with `my_agent_home` creates the owner home, seed files, and core directories.
- [ ] Add a minimal runtime helper that calls `ensure_my_agent_home` during `SimpleAgent.__init__` and stores `agent.home_paths`.
- [ ] Run `python3 -m pytest -q agent_py_agent/tests/test_home_runtime_bootstrap.py`.

### Task 2: Run Task Workspace

**Files:**
- Create: `agent_py_agent/agent/user_space/run_workspace.py`
- Modify: `agent_py_agent/agent/agent_core/_finalization_service.py`
- Test: `agent_py_agent/tests/test_home_runtime_bootstrap.py`

- [ ] Add a failing test that a saved run creates `workspace/tasks/{date}/{task_slug}/outputs` and `runtime` under `my_agent_home`.
- [ ] Implement a small bundle-based run workspace writer that records `task.yaml`, `state.json`, and `timeline.jsonl` without reading artifact bodies.
- [ ] Call it from finalization only when `do_save` is true.

### Task 3: Daily Memory Mirror

**Files:**
- Modify: `agent_py_agent/agent/memory_store/jsonl.py`
- Modify: `agent_py_agent/agent/core.py`
- Test: `agent_py_agent/tests/test_home_runtime_bootstrap.py`

- [ ] Add a failing test that each saved memory record is also appended to `memory/daily/YYYY-MM-DD.jsonl`.
- [ ] Add optional daily mirror support to `JsonlMemory` while preserving the old `memory_path`.
- [ ] Wire `SimpleAgent` to pass `home_paths.memory_daily_dir` when enabled.

### Task 4: Key Memory And Lessons Context

**Files:**
- Modify: `agent_py_agent/agent/prompting_parts/builder.py`
- Test: `agent_py_agent/tests/test_home_runtime_bootstrap.py`

- [ ] Add a failing test that `memory.md` is included in prompt context and matching lesson files are included by filename stem.
- [ ] Implement lightweight home context loading with config-backed lesson limits.
- [ ] Avoid reading every lesson file on every prompt.

### Task 5: Risk Config Enforcement

**Files:**
- Modify: `agent_py_agent/agent/settings/home_config.py`
- Modify: `agent_py_agent/agent/settings/services/_normalize_home_fields.py`
- Modify: `agent_py_agent/agent/user_space/provider_space.py`
- Modify: `agent_py_agent/config/agent_config.yaml`
- Test: `agent_py_agent/tests/test_provider_space.py`
- Test: `agent_py_agent/tests/test_config_normalize.py`

- [ ] Add tests for home runtime booleans/limits and provider trash retention cleanup.
- [ ] Normalize new config fields and use provider trash retention from config.
- [ ] Document the new knobs in `agent_config.yaml`.

# File Writing Rules

LLM: Any new write path must have one clear owner and one clear storage class.

给人看的解释：
文件写入最容易制造幽灵状态。本规则要求写入位置、格式、生命周期都可解释。

## Storage Classes

- Source code: committed under package or script directories.
- Governance docs: committed under `docs/`.
- Runtime state: ignored under `agent_py_agent/data/`, `agent_py_agent/memory/`, or `agent_py_agent/memory_archive/`.
- Test output: written to pytest `tmp_path` or temporary sandbox only.

## Rules

- Do not write runtime state next to source files.
- Do not commit local DBs, token profiles, raw memory, cache directories, or generated reports.
- If a feature needs a new durable format, document it before implementation.
- If a write can fail, surface the failure to the caller unless the caller explicitly accepts best effort.


# Memory V2 Layout

This note records the current memory boundary after the 2026-06-03 cleanup.

## Main / Root Agent

The main agent owns the long-term memory surface under its owner home:

```text
~/.my-agent/owners/local/main/
  AGENTS.md
  SOUL.md
  USER.md
  memory.md
  memory-hot.md
  memory/
    store.jsonl
    ops.jsonl
    daily/
    long_term/
      memory.jsonl
    lessons/
    routing/
      INDEX.md
    indexes/
    runtime_refs/
  audit/
    YYYY-MM-DD.jsonl
  blobs/
    tool_outputs/
    files/
  tasks/YYYY-MM-DD/<task-slug>/
    output/
    work/
```

`AGENTS.md`, `SOUL.md`, `USER.md`, `memory.md`, and `memory-hot.md` stay human-facing prompt entry files.
`memory/store.jsonl` is the consolidated machine-readable long-term fact store for grep, RAG, and later vector indexing.
`memory/ops.jsonl` records explicit memory operations such as remember, revise, revoke, import, export, or promotion.
`memory/daily/` is the human-scale daily memory area.
`audit/YYYY-MM-DD.jsonl` is the raw black-box event stream for turns, tools, gateway, hooks, and runtime events.
`blobs/tool_outputs/` holds large externalized tool outputs; lightweight records keep only previews, hashes, scopes, and refs.

Runtime reads and writes use `audit/` and `blobs/` as the canonical paths.

## Subagent

A subagent does not own long-term memory. Its files are task-local and exist for audit, recovery, and parent review:

```text
tasks/YYYY-MM-DD/<task-slug>/work/agents/<run-id>/
  state.json
  events.jsonl
  artifacts.jsonl
  task.md
  summary.md
  final_report.md
  findings.jsonl
  compact/
  memory_gate/
```

`state.json` is the latest task-local state.
`events.jsonl` is the subagent lifecycle/audit stream, including memory candidates.
`artifacts.jsonl` lists task-local outputs and evidence refs.
`compact/` is the new compact area; `compactions/` remains for compatibility with existing code.
`memory_gate/` is still the compatibility review queue. It never auto-promotes into long-term memory. If a subagent finds something worth keeping, it emits a candidate for parent/root review.

## Role Template Prototype

Prototype subagent templates can live under `agent/subagents/role_template_catalog/examples/`.
They may define a lightweight working style and preloaded skill names, but the current runtime does not load these YAML prototypes yet. Existing subagent dispatch still uses the current role template path.

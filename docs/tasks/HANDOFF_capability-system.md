# Capability System Branch Handoff

Branch: `会话运行时/capability-system`

This branch adds the first complete capability layer for tool/skill/MCP
discovery, routing, learning drafts, and runtime execution. It is intended to be
merged after other architecture-cleanup branches review the changed boundaries
below.

## Summary

The branch implements a unified capability catalog so the model can discover
available tools, skills, and MCP tools before acting. It also adds a safe skill
learning flow: the model can create reviewable skill drafts, but learned skills
do not become active until an explicit lifecycle promote command runs.

The branch does not add runtime dependencies and does not implement the later
fine-grained human approval policy. Current safety is based on existing tool
boundaries, capability grant scope, explicit lifecycle actions, and audit files.

## Main Features Added

### Capability Catalog

- Added `capability_search`.
- Added `capability_describe`.
- Tools, skills, built-in browser/testing capability cards, and MCP tools are
  represented as `CapabilityCard` objects.
- Catalog tools disclose capability metadata only. They do not execute, grant, or
  authorize tools by themselves.
- Capability cards include risk level, side effects, source, metadata, when-to-use
  hints, not-when-to-use hints, and search reasons.

### Capability Grant Scope

- Added `CapabilityGrantScope` for filtering visible/executable tool, skill, and
  MCP capability ids.
- Empty grant lists mean no extra capability filtering.
- MCP execution checks grant scope again inside `McpTool`.

### MCP Runtime and Discovery

- Added executable MCP stdio support through `StdioMcpExecutor`.
- MCP tools are registered as normal tools named `mcp.<server>.<tool>`.
- Added `mcp_auto_discover_tools`.
- Startup can call MCP `tools/list` and merge returned tools into descriptors.
- Explicit `mcp_tool_descriptors` win over auto-discovered descriptors.
- Discovery is best-effort: one failing MCP server does not block other servers
  or manual descriptors.
- `StdioMcpExecutor` caches discovered tools and exposes `refresh_tools(server)`
  for long-running agents.
- MCP `inputSchema` is surfaced in `ToolSpec.parameter_details`, including
  required fields, enum values, property descriptions, and one-level nested object
  summaries.

### Skill Registry and Lifecycle

- Added skill scanning and skill-to-capability-card mapping under
  `agent_py_agent/agent/capability/skills.py`.
- Added `SkillLifecycleStore` with:
  - `drafts/<name>/SKILL.md`
  - `active/<name>/SKILL.md`
  - `versions/<name>/vN/SKILL.md`
  - `disabled/<name>/vN/SKILL.md`
  - `events.jsonl`
- Added lifecycle actions:
  - `create_draft`
  - `promote`
  - `disable`
  - `rollback`
  - `events`
- Active lifecycle skills are scanned by `SkillRegistry` and show up in
  `capability_search`.

### Model-Callable Skill Learning Tools

- Added `skill_draft_from_task`.
  - Input is a structured Request bundle.
  - It renders deterministic `SKILL.md`.
  - It writes only to draft storage.
  - It never promotes or activates the skill.
- Added `skill_lifecycle`.
  - Input is a structured Command bundle or flat fields.
  - It performs explicit lifecycle transitions and emits structured JSON.
- Runtime-promoted skills are visible to capability catalog without rebuilding
  `ToolRegistry`.

### Routing Improvements

- Capability search remains deterministic and dependency-free.
- Added a small auditable semantic alias map for common search terms such as
  `网页`, `浏览器`, `截图`, `验收`, `接口`, `文件`, and `搜索`.
- Added usage feedback storage so successful/failed capability choices can
  adjust ranking.
- No vector runtime dependency is required.

### Registry Compatibility Fix

- Fixed a typed action protocol compatibility issue:
  - A normal flat tool payload like
    `{"tool": "capability_search", "kind": "skill"}` must remain a normal tool
    call.
  - The `kind` field should only be interpreted as a typed action envelope when
    the payload is not already a flat tool call.

## Important Files Changed

### New Capability Layer

- `agent_py_agent/agent/capability/grants.py`
- `agent_py_agent/agent/capability/mcp.py`
- `agent_py_agent/agent/capability/mcp_config.py`
- `agent_py_agent/agent/capability/mcp_runtime.py`
- `agent_py_agent/agent/capability/router.py`
- `agent_py_agent/agent/capability/scoring.py`
- `agent_py_agent/agent/capability/skill_lifecycle.py`
- `agent_py_agent/agent/capability/skills.py`
- `agent_py_agent/agent/capability/usage.py`

### Tooling Integration

- `agent_py_agent/agent/tooling/capability_catalog.py`
- `agent_py_agent/agent/tooling/registry.py`
- `agent_py_agent/agent/tooling/registry_capability_catalog.py`
- `agent_py_agent/agent/tooling/registry_envelopes.py`
- `agent_py_agent/agent/tooling/skill_learning.py`
- `agent_py_agent/agent/tooling/skill_lifecycle_tools.py`

### Agent Startup and Config

- `agent_py_agent/agent/core.py`
- `agent_py_agent/agent/settings/tool_config.py`
- `agent_py_agent/config/agent_config.yaml`

New config fields:

- `mcp_auto_discover_tools`
- `mcp_stdio_servers`
- `mcp_tool_descriptors`
- `capability_grant_tools`
- `capability_grant_skills`
- `capability_grant_mcp_tools`
- `skill_lifecycle_tools_enabled`
- `skill_lifecycle_root`

### Documentation

- `docs/development/CAPABILITY_SYSTEM.md`
- `docs/tasks/HANDOFF_capability-system.md`

## Tests Added or Expanded

- `agent_py_agent/tests/test_capability_catalog_tools.py`
- `agent_py_agent/tests/test_capability_catalog_registry_integration.py`
- `agent_py_agent/tests/test_capability_grants_usage.py`
- `agent_py_agent/tests/test_capability_mcp_real_e2e.py`
- `agent_py_agent/tests/test_capability_mcp_runtime.py`
- `agent_py_agent/tests/test_capability_router_catalog.py`
- `agent_py_agent/tests/test_mcp_discovery_config.py`
- `agent_py_agent/tests/test_skill_lifecycle.py`
- `agent_py_agent/tests/test_skill_lifecycle_tools.py`
- `agent_py_agent/tests/test_skill_registry_catalog.py`

## Verification Already Run

All commands below passed on this branch before this handoff document was added:

```bash
python3 -m pytest agent_py_agent/tests/test_skill_lifecycle.py agent_py_agent/tests/test_skill_lifecycle_tools.py agent_py_agent/tests/test_mcp_discovery_config.py agent_py_agent/tests/test_capability_mcp_runtime.py agent_py_agent/tests/test_capability_mcp_real_e2e.py agent_py_agent/tests/test_capability_catalog_tools.py agent_py_agent/tests/test_capability_catalog_registry_integration.py agent_py_agent/tests/test_skill_registry_catalog.py agent_py_agent/tests/test_capability_grants_usage.py -q
python3 -m pytest agent_py_agent/tests -q
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
```

Real integration tests performed:

- Real local `SimpleAgent` tool-chain smoke:
  - registered `skill_draft_from_task` and `skill_lifecycle`;
  - created a draft skill on disk;
  - promoted it to active;
  - found it via `capability_search`.
- Real external model smoke:
  - model backend `anthropic_compatible`;
  - model actually called `skill_draft_from_task`;
  - draft `real-model-skill-draft` was written to disk;
  - final model response contained `DRAFT_CREATED`.
- Real MCP stdio subprocess smoke:
  - local Python MCP server handled `initialize`, `tools/list`, and `tools/call`;
  - `SimpleAgent` discovered schema from `tools/list`;
  - model/tool loop called the registered MCP tool.

After this handoff document is added, rerun at minimum:

```bash
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
ruff check agent_py_agent scripts
```

## Merge Notes

- Likely conflict area: `agent_py_agent/agent/core.py`, because other branches may
  also modify agent startup and `ToolRegistryParams`.
- Likely conflict area: `agent_py_agent/agent/tooling/registry.py`, because this
  branch adds lifecycle skill registration and MCP registration state.
- Likely conflict area: `agent_py_agent/agent/settings/tool_config.py` and
  `agent_py_agent/config/agent_config.yaml`, because other branches may add new
  tool/config fields.
- Likely conflict area: `CODE_SIZE_REPORT.md`, because every branch that runs
  code-size can update it.
- If another branch changes typed action protocol execution, preserve the rule
  that flat payloads with a `tool` key are ordinary tool calls even when they also
  contain a `kind` parameter.
- If another branch changes skill scanning, preserve the lifecycle invariant:
  drafts are not active, active skills are scanned, disabled skills disappear from
  active scans, versions remain available for rollback.
- If another branch changes MCP support, preserve descriptor/executor separation:
  discovery creates descriptors, execution goes through registered tools and grant
  checks.

## Deferred Work

- Fine-grained human approval policy for MCP server changes, skill promotion, and
  dangerous capability grants is intentionally not implemented in this branch.
- Semantic routing is still deterministic keyword/alias based. A future semantic
  index can be added, but should remain optional and auditable.
- Lifecycle storage currently uses local filesystem directories and JSONL events.
  A future multi-user/shared-store implementation should keep the same lifecycle
  states and explicit promotion semantics.

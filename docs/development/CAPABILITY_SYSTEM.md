# Capability System / 能力系统

This document is for architecture review and feature audits. It describes how
tools, skills, MCP tools, routing, and learning drafts fit together.

## Boundaries

- `agent_py_agent/agent/capability/` owns capability cards, routing, MCP
  descriptors, grant filtering, usage feedback, and skill lifecycle state.
- `agent_py_agent/agent/tooling/` owns model-callable tools. Capability tools
  disclose or mutate only through typed command/request objects.
- MCP execution is registered as normal tools named `mcp.<server>.<tool>`.
  Discovery and execution stay separate: `tools/list` creates descriptors, while
  `tools/call` runs only through the registered executor.
- Learned skills are never written directly into the active catalog. They start
  as drafts under `skill_lifecycle_root`, then require an explicit promote action.

## Model-Visible Tools

| Tool | Purpose | Writes Active Skill? |
| --- | --- | --- |
| `capability_search` | Search visible tool, skill, and MCP cards. | No |
| `capability_describe` | Show one visible card's details and metadata. | No |
| `skill_draft_from_task` | Turn structured task feedback into a draft `SKILL.md`. | No |
| `skill_lifecycle` | Create drafts, promote, disable, rollback, and list lifecycle events. | Only on explicit `promote` or `rollback` |

`skill_draft_from_task` is the self-learning lane. It accepts a structured
Request bundle with name, task summary, when-to-use text, steps, required tools,
tags, risk level, and reason. It renders deterministic Markdown and stores it as
a draft.

`skill_lifecycle` is the lifecycle command lane. It accepts a structured Command
bundle or flat fields with `action`, `name`, `markdown`, `version`, and `reason`.
Supported actions are `create_draft`, `promote`, `disable`, `rollback`, and
`events`.

## MCP Discovery

When `mcp_auto_discover_tools` is enabled, startup calls `tools/list` on each
configured stdio MCP server and merges returned tool descriptors into the
capability catalog. Explicit descriptors win over discovered descriptors.

Discovery is best-effort. A failed server does not block other servers or manual
descriptors. The stdio executor caches `tools/list` results and exposes
`refresh_tools(server)` for long-running agents that need to refresh a server's
tool schema.

The first-level MCP `inputSchema` is exposed in the normal `ToolSpec`
`parameter_details`, including required fields, enum values, descriptions, and
one-level nested object summaries.

## Routing

Capability search is deterministic. It combines:

- exact name, kind, keyword, capability, description, and when-to-use matches;
- bounded usage feedback from `CapabilityUsageStore`;
- a small local alias map for common user terms such as `网页`, `截图`, `接口`,
  and `测试`.

There is no new runtime dependency and no hidden vector index requirement. The
alias map is intentionally small and auditable; it improves recall without
turning natural language into a hard business rule.

## Audit and Storage

Skill lifecycle state lives under `skill_lifecycle_root`:

- `drafts/<name>/SKILL.md`
- `active/<name>/SKILL.md`
- `versions/<name>/vN/SKILL.md`
- `disabled/<name>/vN/SKILL.md`
- `events.jsonl`

The active directory is scanned by `SkillRegistry`, so promoted lifecycle skills
appear in `capability_search` as normal skill cards. Disabled skills disappear
from active scans but remain versioned for rollback and audit.

## Configuration

Relevant runtime fields:

- `skill_lifecycle_tools_enabled`
- `skill_lifecycle_root`
- `mcp_auto_discover_tools`
- `mcp_stdio_servers`
- `mcp_tool_descriptors`
- `capability_grant_tools`
- `capability_grant_skills`
- `capability_grant_mcp_tools`

Grant scope filters visibility and MCP execution, but it is not a replacement
for write-boundary checks or future explicit approval policy.

# LLM: Agent core prompt templates; keep these as data-only constants imported by runner_prompts.py.
# 模块用途: 存放子代理 runner 输出模板和协作工具提示，避免把大段模板文本塞进执行逻辑文件。

from __future__ import annotations

SUBAGENT_RESULT_TEMPLATE = (
    "[SUBAGENT_RESULT]\n"
    "{\n"
    '  "status": "AWAITING_ACCEPTANCE",\n'
    '  "summary": "本轮完成或卡住的摘要",\n'
    '  "used_tools": [],\n'
    '  "used_skills": [],\n'
    '  "evidence": [\n'
    '    {"kind": "command", "summary": "验证摘要", "command": "", "path": "", "url": "", "ok": true}\n'
    "  ],\n"
    '  "evidence_packets": [\n'
    '    {"id": "evpkt-run-id-short", "claim": "可验收声明", "checked_scope": "检查范围", "evidence_refs": ["runner_result.json"], "artifact_refs": ["产物路径或output.json"], "confidence": 0.9}\n'
    "  ],\n"
    '  "coverage_records": [\n'
    '    {"covered_run_id": "失败或损坏的run_id", "covered_by_run_id": "已DONE/VERIFIED的覆盖run_id", "reason": "为什么覆盖同一范围", "artifact_refs": ["覆盖者产物路径"], "evidence_refs": ["覆盖者证据路径"]}\n'
    "  ],\n"
    '  "capability_requests": [\n'
    '    {"problem": "缺少什么", "needed_capability": "能力名", "capability_type": "shell|tool|skill|mcp|network|generic", "expected_output": "希望得到什么", "requested_tools": [], "requested_skills": [], "requested_mcp_tools": [], "requested_commands": ["python3"], "cwd_scope": [], "path_scope": ["任务内需要访问的目录"], "network_scope": [], "output_budget": {"stdout_bytes": 65536, "stderr_bytes": 32768}, "risk_level": "low|medium|high", "tried": [], "evidence": [], "constraints": {}, "fallback_attempted": [], "escalation_target": "parent", "reserved": {}}\n'
    "  ],\n"
    '  "artifacts": [\n'
    '    {"path": "产物路径", "kind": "file|report|log", "summary": "产物说明"}\n'
    "  ],\n"
    '  "tests": [\n'
    '    {"name": "测试名称", "validation_method": "command", "command": "python3 -m pytest -q", "working_dir": "运行目录", "ok": true, "summary": "测试结果摘要"}\n'
    "  ],\n"
    '  "patches": [\n'
    '    {"path": "改动文件", "status": "applied|planned|blocked", "summary": "改了什么或准备改什么"}\n'
    "  ],\n"
    '  "lessons": ["可沉淀经验，适合未来变成 skill 或规则"],\n'
    '  "next_actions": ["建议父代理下一步动作"],\n'
    '  "blocked_reason": "",\n'
    '  "failure_type": ""\n'
    "}\n"
    "[/SUBAGENT_RESULT]\n"
)

SUBAGENT_REPAIR_RESULT_TEMPLATE = (
    "[SUBAGENT_RESULT]\n"
    "{\n"
    '  "status": "AWAITING_ACCEPTANCE",\n'
    '  "summary": "本轮完成或卡住的摘要",\n'
    '  "used_tools": [],\n'
    '  "used_skills": [],\n'
    '  "evidence": [\n'
    '    {"kind": "artifact", "summary": "已检查的产物或报告", "path": "产物路径或报告路径", "ok": true}\n'
    "  ],\n"
    '  "evidence_packets": [\n'
    '    {"id": "evpkt-repair-run-id-short", "claim": "可验收声明", "checked_scope": "修复整理范围", "evidence_refs": ["报告或output.json路径"], "artifact_refs": ["产物路径"], "confidence": 0.8}\n'
    "  ],\n"
    '  "coverage_records": [],\n'
    '  "capability_requests": [],\n'
    '  "artifacts": [],\n'
    '  "tests": [],\n'
    '  "patches": [],\n'
    '  "lessons": [],\n'
    '  "next_actions": [],\n'
    '  "blocked_reason": "",\n'
    '  "failure_type": ""\n'
    "}\n"
    "[/SUBAGENT_RESULT]"
)

COLLABORATION_CONTROL_PLANE_TOOLS = (
    "case_status",
    "list_collaboration_requests",
    "raise_collaboration_event",
    "open_case",
    "request_collaboration",
    "submit_evidence",
    "update_collaboration_request",
    "reroute_collaboration_request",
)

COLLABORATION_TOOL_HINTS = (
    ("case_status", "- case_status：先读取已有 case/request 状态，避免重复开 case 或重复提交同一份证据。"),
    (
        "list_collaboration_requests",
        "- list_collaboration_requests：如果你知道自己被要求协作、但不知道 case_id/request_id，"
        "先用它按自己的 agent_id/agent_name/role 查询待响应请求；不要因为缺 case_id 就新开重复 case。",
    ),
    (
        "raise_collaboration_event",
        "- raise_collaboration_event：如果你发现需要其他代理、其他数据源或上级共同补证据，"
        "优先用这个单步工具打开 case 并发出 request；不要只在 output.json 里写 collaboration_required。"
        "它接受 observed_facts/query_hints/response_contract 等开放世界字段，不要求业务专项格式。",
    ),
    (
        "open_case",
        "- open_case：发现需要多代理共同研判、补证据、换数据源或跟踪阻塞时，"
        "打开通用协作 case，写清 title、summary、entities 和 required_capabilities。",
    ),
    (
        "request_collaboration",
        "- request_collaboration：需要其他代理补证据时发起请求，写清 question、target_agent_ids 或 required_capabilities；"
        "如果发现的是可被多方查证的线索，把 observed_facts、query_intent、query_hints、response_contract 和 context_refs 一起交出去。"
        "query_hints 是软提示，响应代理可自行拆分或改写。请求引用形如 collaboration://request/<id>。",
    ),
    (
        "submit_evidence",
        "- submit_evidence：回应协作请求时提交 refs-first 证据，优先给 evidence_refs/artifact_refs、matched、confidence 和简短 summary，"
        "不要把长正文塞进消息。",
    ),
    (
        "update_collaboration_request",
        "- update_collaboration_request：完成、阻塞或需要返工时更新 request 状态，把 actor_agent_id、summary 和必要的 request 引用写清楚。",
    ),
    (
        "reroute_collaboration_request",
        "- reroute_collaboration_request：原目标没有证据、不可用或更合适的来源已出现时，用结构化 target_agent_ids 改派；"
        "不要只在 summary 里说已经协作或已经转派。",
    ),
)

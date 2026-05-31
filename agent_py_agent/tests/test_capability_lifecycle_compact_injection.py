from __future__ import annotations

import json
from pathlib import Path


def test_capability_request_expiry_keeps_request_visible(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.capability_requests import (
        CreateCapabilityRequest,
        create_capability_request,
        expire_capability_requests,
        list_capability_requests,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    request = create_capability_request(
        home,
        CreateCapabilityRequest(
            requested_by="agent-1",
            capability="tool.extra_search",
            reason="需要额外搜索工具",
            expires_at="2026-05-31T00:00:00+00:00",
        ),
    )

    expired = expire_capability_requests(home, now="2026-06-01T00:00:00+00:00")
    open_requests = list_capability_requests(home, status="open")
    expired_requests = list_capability_requests(home, status="expired")

    assert expired[0].request_id == request.request_id
    assert open_requests == []
    assert expired_requests[0].capability == "tool.extra_search"


def test_compact_injection_renders_context_and_continue_packet(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.compact_injection import render_compact_injection
    from agent_py_agent.agent.user_space.compact_layout import ensure_compact_package

    paths = ensure_compact_package(tmp_path / "compact", compact_index=3, scope="task")
    paths.compact_context_md.write_text("当前已经读完 A 项目。\n", encoding="utf-8")
    paths.continue_packet_json.write_text(
        json.dumps(
            {
                "schema_version": "continue-packet.v1",
                "next_action": "继续分析 B 项目源码",
                "completed_items": ["A 项目"],
                "pending_work": ["B 项目", "C 项目"],
                "avoid_repeating": ["不要重复读 A 的 README"],
                "target_outputs": ["final.md"],
                "known_blockers": [],
                "user_updates": ["用户要求每个结论带文件证据"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rendered = render_compact_injection(paths.package_dir)

    assert "当前已经读完 A 项目" in rendered
    assert "继续分析 B 项目源码" in rendered
    assert "不要重复读 A 的 README" in rendered

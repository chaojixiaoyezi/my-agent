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
    from agent_py_agent.agent.user_space.compact_layout import (
        CompactPackageRequest,
        ensure_compact_package,
    )

    paths = ensure_compact_package(tmp_path / "compact", CompactPackageRequest(3, "task"))
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


# LLM: resolver caches a run's chosen capability version but must not keep using revoked entries.
# 函数用途: 验证同一 run 内版本解析可复用，安全撤销后缓存会失效并返回 revoked。
def test_capability_resolver_pins_version_and_invalidates_revoked(tmp_path: Path) -> None:
    import json

    from agent_py_agent.agent.user_space.capability_resolver import (
        CapabilityResolveOptions,
        resolve_owner_capability,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    home.shared_indexes_skills_jsonl.write_text(
        json.dumps({"id": "shared:weekly@1.0.0", "name": "weekly", "source": "shared", "version": "1.0.0"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    first = resolve_owner_capability(home, "weekly", CapabilityResolveOptions(kind="skill", run_id="run-1"))
    home.shared_indexes_skills_jsonl.write_text(
        json.dumps({"id": "shared:weekly@2.0.0", "name": "weekly", "source": "shared", "version": "2.0.0"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    pinned = resolve_owner_capability(home, "weekly", CapabilityResolveOptions(kind="skill", run_id="run-1"))
    fresh = resolve_owner_capability(home, "weekly", CapabilityResolveOptions(kind="skill", run_id="run-2"))
    home.shared_indexes_skills_jsonl.write_text(
        json.dumps({"id": "shared:weekly@2.0.0", "name": "weekly", "source": "shared", "version": "2.0.0", "status": "revoked"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    revoked = resolve_owner_capability(home, "weekly", CapabilityResolveOptions(kind="skill", run_id="run-1"))

    assert first.resolved_id == "shared:weekly@1.0.0"
    assert pinned.resolved_id == "shared:weekly@1.0.0"
    assert fresh.resolved_id == "shared:weekly@2.0.0"
    assert revoked.status == "revoked"

from __future__ import annotations

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

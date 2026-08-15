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

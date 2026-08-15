from __future__ import annotations

from pathlib import Path


def test_owner_capability_request_lifecycle(tmp_path: Path):
    from agent_py_agent.agent.user_space.capability_requests import (
        CreateCapabilityRequest,
        close_capability_request,
        create_capability_request,
        list_capability_requests,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)

    request = create_capability_request(
        home,
        CreateCapabilityRequest(
            requested_by="agent_sub_1",
            capability="browser_login",
            reason="需要登录页面确认结果",
            task_id="task_1",
        ),
    )
    closed = close_capability_request(home, request.request_id, status="expired", note="子代理已结束")

    rows = list_capability_requests(home)

    assert request.path.parent == home.owner_capability_requests_dir
    assert closed.status == "expired"
    assert rows[-1].request_id == request.request_id
    assert rows[-1].status == "expired"


def test_owner_capability_request_rejects_unknown_close_status(tmp_path: Path):
    from agent_py_agent.agent.user_space.capability_requests import (
        CreateCapabilityRequest,
        close_capability_request,
        create_capability_request,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    request = create_capability_request(
        home,
        CreateCapabilityRequest(
            requested_by="agent_sub_1",
            capability="browser_login",
            reason="需要登录页面确认结果",
            task_id="task_1",
        ),
    )

    try:
        close_capability_request(home, request.request_id, status="resolved")
    except ValueError as exc:
        assert str(exc) == "capability_request_status_invalid"
    else:
        raise AssertionError("unknown capability request status should not be silently closed")


def test_owner_capability_requests_report_corrupt_files(tmp_path: Path):
    from agent_py_agent.agent.user_space.capability_requests import list_capability_requests_report
    from agent_py_agent.agent.user_space.home_doctor import build_home_doctor_report
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    bad_path = home.owner_capability_requests_dir / "broken.json"
    bad_path.write_text("{bad capability request}\n", encoding="utf-8")

    report = list_capability_requests_report(home)
    doctor = build_home_doctor_report(home)

    assert report.requests == []
    assert report.load_errors
    assert report.load_errors[0]["context"] == "owner_capability_request.read"
    assert report.load_errors[0]["path"] == str(bad_path)
    assert doctor["capability_requests"]["load_errors"] == report.load_errors

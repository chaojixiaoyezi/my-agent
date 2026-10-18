"""候选服务显式引用与原preview内容，不用Agent替身或恢复HTTP权限。"""

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.tests.test_coordinator_memory_algorithm_components import resources


@pytest.fixture
def service_case(tmp_path):
    value = resources(tmp_path)
    candidate = value.authorities.candidates.observe(CandidateObservation(
        candidate_type="project", content="临时项目使用UTC", subject_key="ordinary.project",
        scope=MemoryScope("personal", "personal"), origin="reviewed", promotion_target="long_term",
    ))
    return value, candidate, gateway_paths_from_root(tmp_path / "gateway")


def test_explicit_services_preserve_real_references_and_preview_contract(service_case):
    from agent_py_agent.agent.gateway_parts import memory_candidate_review as review

    value, candidate, paths = service_case
    service = review.MemoryCandidateServices(
        value.authorities.candidates, value.promotion, value.home.owner_id
    )
    scope = GatewayControlScope("alice", "local", "candidate-session", {"message_id": "preview-one"})
    listed = review.run_memory_candidate_control_from_services(
        service, paths, parse_conversation_control("/memory candidates"), scope, "original-thread"
    )
    assert listed.ok and listed.details["count"] == 1
    assert listed.details["candidates"][0]["candidate_id"] == candidate.candidate_id
    shown = review.run_memory_candidate_control_from_services(
        service, paths, parse_conversation_control("/memory candidates preview " + candidate.candidate_id),
        scope, "original-thread",
    )
    assert shown.ok and shown.details["state"] == "preview"
    assert shown.details["owner"] == value.home.owner_id
    assert shown.details["thread_id"] == "original-thread"
    assert shown.details["value"]["content"] == candidate.content
    assert shown.details["expires_at"] - shown.details["issued_at"] == 600
    assert service.candidates is value.authorities.candidates
    assert service.promotion is value.promotion and value.long_term.all() == []
    assert set(vars(service)) == {'candidates', 'promotion', 'owner_id', 'owner_resources'}
    assert service.owner_resources is None

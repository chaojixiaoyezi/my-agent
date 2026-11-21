"""Exercise lesson routing through the owner index and foreground context."""

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import _prepare_runtime_context
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.tests.test_memory_promotion_v2 import _explicit
from agent_py_agent.tests.test_memory_tool import _agent_with_current_user


@pytest.mark.parametrize("mode", ["soft", "strict"])
@pytest.mark.parametrize("read_limit", [0, 3])
def test_unrelated_lessons_do_not_consume_route_read_slots(tmp_path, mode, read_limit):
    agent = _agent_with_current_user(tmp_path)
    agent.backend = EchoBackend()
    agent.config.memory_rule_routing_mode = mode
    agent.config.memory_rule_auto_read_limit = read_limit
    lessons = []
    for number in range(4):
        project = "outside" if number < 3 else "current"
        candidate = _explicit(
            agent.memory_promotion, agent.conversation_store,
            agent.conversation_store.threads.require(agent._current_run_params.task_attributes["conversation_thread_id"]),
            content=f"Deployment checklist procedure {number}.", candidate_type="lesson", promotion_target="lesson",
            subject_key=f"deploy.checklist.{number}", scope_type="project", scope_key=f"project:{project}",
        )
        approved = agent.memory_promotion.review(candidate.candidate_id, approved=True, reviewer="owner")
        lessons.append(agent.memory_lessons.promote(approved, min_occurrences=1))

    index = agent.home_paths.owner_memory_routing_index_md
    index.write_text("# Lesson routing\n\n" + "\n\n".join(
        f"## lesson.{lesson.lesson_id}\n"
        f"topic: deployment checklist\ntrigger_keywords: deploy\npriority: {10 - rank}\n"
        f"scope: project:{lesson.scope['scope_key']}\n"
        f"authority_path: {lesson.path}\ninject_mode: on_hit\n"
        for rank, lesson in enumerate(lessons)
    ), encoding="utf-8")
    request = RuntimeContextRequest(
        "deploy checklist", [], False, allowed_tools=[], task_attributes={"project_id": "current"}, save=True,
    )
    prepared = _prepare_runtime_context(agent, request)
    routed = prepared.routed_context
    expected = lessons[-1]
    read_paths = [expected.path] if read_limit else []
    required_paths = read_paths if mode == "strict" else []
    assert [receipt["path"] for receipt in routed.receipts] == read_paths
    assert [match["authority_path"] for match in routed.matches] == [expected.path]
    assert routed.candidate_paths == [expected.path]
    assert routed.required_read_paths == required_paths
    assert [row.content for row in prepared.memories if row.kind == "lesson"] == ([expected.content] if read_limit else [])
    bundle = json.loads(Path(prepared.main_context_bundle_path).read_text(encoding="utf-8"))
    assert bundle["memory_refs"]["routed_candidate_paths"] == [expected.path]
    assert bundle["memory_refs"]["routed_required_read_paths"] == required_paths
    result = agent.run("deploy checklist", save=False, allowed_tools=[], task_attributes={"project_id": "current"})
    assert result.memory_route_matches == 1 and set(result.memory_route_paths) == {expected.path}

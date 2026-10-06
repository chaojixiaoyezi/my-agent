"""learnpack 第 5 步："让她总结一下"（skill_summarize）。

锁定：只注册给本机管理员且默认收起；自学习关着如实返回 SKILL_SUMMARIZE_DISABLED；不在对话回合里返回 SKILL_SUMMARIZE_NO_RUN；
按结构化参数 target 分流：current 要求这一轮正在做活（否则 SKILL_SUMMARIZE_NO_CURRENT_TASK），按会话任务 id 打标记，那个任务
收尾（哪怕在后面的运行里）入队、不受最少工具轮数限制、只用一次；previous 当场入队这个会话最近完成的那次活（找不到如实返回
SKILL_SUMMARIZE_NOTHING_RECENT）。同一次活对用户只入队一次（两条路同一个用户请求键，已入队过如实说），不被自动总结去重；
队列满或写入抛错时撤回认领、过会儿能再要（收尾时入队抛错，这次活照样记成最近完成的那次）；标记取走后那次运行不合格会在
自学习账本里留丢弃事件；请求带 requested_by=user、重点和本轮召回的相关记忆（最多 8 条、各截 600 字；自动总结的请求不带）；
普通自动总结的提示词逐字节不变；过闸门发布后能回退。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning_prompt import (
    SkillLearningMaterial,
    skill_learning_prompt,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig


def _agent(tmp_path, monkeypatch, *, owner_kind="main", owner_id="main", **config) -> SimpleAgent:
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_owner_provider="local",
                                   my_agent_owner_kind=owner_kind, my_agent_owner_id=owner_id, **config),
                       tmp_path / "project")


def _ctx(run_id: str, tool_rounds: int, *, task_id: str = "", thread_id: str = "th-1",
         prompt: str = "把这一集短剧按人物重排台词") -> SimpleNamespace:
    attrs = {"conversation_thread_id": thread_id, "conversation_task_completed": True}
    if task_id:
        attrs["conversation_task_id"] = task_id
    recalled = [SimpleNamespace(entry_id=f"mem-{index}", kind="fact", content=f"短剧每集结尾留钩子 {index} " + "长" * 900)
                for index in range(10)]
    return SimpleNamespace(do_save=True, context_scope="default", source="user", tool_rounds=tool_rounds,
                           run_id=run_id, request_id=f"req-{run_id}", task_id=task_id or run_id, task_attributes=attrs,
                           archive_tool_calls=[], user_prompt=prompt, final_response=SimpleNamespace(text="已经改完。"),
                           memories=recalled)


def _pending(service) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in service.store.pending_requests()]


def test_registered_for_admin_only_and_folded(tmp_path, monkeypatch):
    admin = _agent(tmp_path, monkeypatch)
    hints = admin.tools.tools["skill_summarize"].model_spec.hints
    assert hints.default_deferred and hints.deferred_summary
    user = _agent(tmp_path / "u", monkeypatch, owner_kind="user", owner_id="alice")
    assert "skill_summarize" not in user.tools.tools


def test_self_learning_off_is_reported_honestly(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, enable_self_learning=False)
    outcome = agent.tools.tools["skill_summarize"].execute({"focus": "改稿顺序"})
    assert not outcome.ok and outcome.error_code == "SKILL_SUMMARIZE_DISABLED" and "enable_self_learning" in outcome.output


def test_work_in_progress_is_marked_by_task_and_queued_when_that_task_finishes(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, enable_self_learning=True)
    tool = agent.tools.tools["skill_summarize"]
    assert tool.execute({}).error_code == "SKILL_SUMMARIZE_NO_RUN"
    agent._current_run_params = SimpleNamespace(run_id="run-42", task_attributes={
        "conversation_task_turn_active": True, "conversation_task_id": "task-7", "conversation_thread_id": "th-1"})
    assert tool.execute({"target": "nope"}).error_code == "TOOL_INVALID_ARGUMENTS"
    for attrs in ({"conversation_task_id": "task-7"},  # 上一件活的任务还挂着，但这一轮没在做活
                  {"conversation_task_turn_active": True, "conversation_task_id": "task-7", "conversation_task_completed": True}):
        agent._current_run_params = SimpleNamespace(run_id="run-41", task_attributes=attrs)
        assert tool.execute({"target": "current"}).error_code == "SKILL_SUMMARIZE_NO_CURRENT_TASK", attrs
    agent._current_run_params = SimpleNamespace(run_id="run-42", task_attributes={
        "conversation_task_turn_active": True, "conversation_task_id": "task-7", "conversation_thread_id": "th-1"})
    agent._current_user_prompt = "把这一集短剧按人物重排台词，做完把做法总结一下"
    body = json.loads(tool.execute({"target": "current", "focus": "先列人物再改台词"}).output)
    assert body["mode"] == "this_task" and body["task_id"] == "task-7"
    assert body["summarizes"] in body["message"] and body["summarizes"].startswith("把这一集短剧"), "回执写出这一轮的提问"
    service = agent.skill_learning
    assert service.enqueue_from_finalize(_ctx("run-other", 1, task_id="task-other")) == "ineligible", "没打标记照旧按门槛"
    assert service.enqueue_from_finalize(_ctx("run-43", 1, task_id="task-7")) == "queued", "任务在后面的运行里收尾也认"
    [request] = _pending(service)
    assert request["requested_by"] == "user" and request["user_focus"] == "先列人物再改台词" and request["run_id"] == "run-43"
    memories = request["recalled_memories"]
    assert [item["entry_id"] for item in memories] == [f"mem-{index}" for index in range(8)], "本轮召回的记忆一起交，最多 8 条"
    assert memories[0]["kind"] == "fact" and memories[0]["content"].startswith("短剧每集结尾留钩子 0")
    assert all(len(item["content"]) <= 600 for item in memories), "每条正文有上限"
    assert service.enqueue_from_finalize(_ctx("run-44", 1, task_id="task-7")) == "ineligible", "标记只用一次"


def test_asking_after_the_work_is_done_queues_that_finished_work(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, enable_self_learning=True)
    service, tool = agent.skill_learning, agent.tools.tools["skill_summarize"]
    agent._current_run_params = SimpleNamespace(run_id="run-2", task_attributes={"conversation_thread_id": "th-2"})
    nothing = tool.execute({"target": "previous"})
    assert not nothing.ok and nothing.error_code == "SKILL_SUMMARIZE_NOTHING_RECENT", "没有做完的活就如实说找不到"
    assert tool.execute({"target": "current"}).error_code == "SKILL_SUMMARIZE_NO_CURRENT_TASK", "这一轮没在做活"
    assert service.enqueue_from_finalize(_ctx("run-1", 1, task_id="task-1", thread_id="th-2")) == "ineligible"
    body = json.loads(tool.execute({"target": "previous", "focus": "台词节奏"}).output)
    assert body["mode"] == "recent_task" and body["task_id"] == "task-1" and "按人物重排台词" in body["summarizes"]
    assert body["recalled_memories"] == 8 and "8 条相关记忆" in body["message"]
    [request] = _pending(service)
    assert request["requested_by"] == "user" and request["user_focus"] == "台词节奏" and request["run_id"] == "run-1"
    assert len(request["recalled_memories"]) == 8, "做完再要的，也带上那次召回的记忆"
    assert json.loads(tool.execute({"target": "previous"}).output)["queue"] == "already_requested", "同一次活不再重复"
    assert len(_pending(service)) == 1
    other = SimpleNamespace(run_id="run-9", task_attributes={"conversation_thread_id": "th-other"})
    agent._current_run_params = other
    assert tool.execute({"target": "previous"}).error_code == "SKILL_SUMMARIZE_NOTHING_RECENT", "别的会话拿不到这个会话的活"


def test_a_user_request_is_not_swallowed_by_the_automatic_one(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, enable_self_learning=True)
    service, tool = agent.skill_learning, agent.tools.tools["skill_summarize"]
    assert service.enqueue_from_finalize(_ctx("run-1", 8, task_id="task-1", thread_id="th-3")) == "queued", "自动总结照常入队"
    agent._current_run_params = SimpleNamespace(run_id="run-2", task_attributes={"conversation_thread_id": "th-3"})
    assert json.loads(tool.execute({"target": "previous"}).output)["queue"] == "queued"
    assert sorted(request.get("requested_by", "auto") for request in _pending(service)) == ["auto", "user"]
    automatic = next(request for request in _pending(service) if "requested_by" not in request)
    assert "recalled_memories" not in automatic, "自动总结的请求不带记忆，和原来一样"


def test_the_same_work_is_queued_for_the_user_only_once_across_both_paths(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, enable_self_learning=True)
    service, tool = agent.skill_learning, agent.tools.tools["skill_summarize"]
    agent._current_run_params = SimpleNamespace(run_id="run-1", task_attributes={
        "conversation_task_turn_active": True, "conversation_task_id": "task-1", "conversation_thread_id": "th-4"})
    assert tool.execute({"target": "current"}).ok
    assert service.enqueue_from_finalize(_ctx("run-1", 1, task_id="task-1", thread_id="th-4")) == "queued"
    agent._current_run_params = SimpleNamespace(run_id="run-2", task_attributes={"conversation_thread_id": "th-4"})
    assert json.loads(tool.execute({"target": "previous"}).output)["queue"] == "already_requested"
    assert len(_pending(service)) == 1
    agent._current_run_params = SimpleNamespace(run_id="run-3", task_attributes={
        "conversation_task_turn_active": True, "conversation_task_id": "task-3", "conversation_thread_id": "th-4"})
    assert tool.execute({"target": "current"}).ok
    real_enqueue = service.store.enqueue
    monkeypatch.setattr(service.store, "enqueue", lambda request: "queue_full")
    assert service.enqueue_from_finalize(_ctx("run-3", 1, task_id="task-3", thread_id="th-4")) == "queue_full"
    monkeypatch.setattr(service.store, "enqueue", real_enqueue)
    agent._current_run_params = SimpleNamespace(run_id="run-4", task_attributes={"conversation_thread_id": "th-4"})
    assert json.loads(tool.execute({"target": "previous"}).output)["queue"] == "queued", "收尾时队列满，事后还能再要"


def test_queue_full_releases_the_claim_and_an_ineligible_finish_is_logged(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, enable_self_learning=True)
    service, tool = agent.skill_learning, agent.tools.tools["skill_summarize"]
    assert service.enqueue_from_finalize(_ctx("run-1", 1, task_id="task-1", thread_id="th-5")) == "ineligible"
    agent._current_run_params = SimpleNamespace(run_id="run-2", task_attributes={"conversation_thread_id": "th-5"})
    real_enqueue = service.store.enqueue
    monkeypatch.setattr(service.store, "enqueue", lambda request: "queue_full")
    assert tool.execute({"target": "previous"}).error_code == "SKILL_SUMMARIZE_QUEUE_FULL"
    monkeypatch.setattr(service.store, "enqueue", real_enqueue)
    assert json.loads(tool.execute({"target": "previous"}).output)["queue"] == "queued", "队列满时撤回认领，过会儿还能再要"
    service.request_learning("task-6", "x")
    background = _ctx("run-6", 1, task_id="task-6")
    background.source = "background_main_agent"
    assert service.enqueue_from_finalize(background) == "ineligible"
    events = [(item["event"], item["code"]) for item in service.store.events(limit=0)]
    assert ("dropped", "SKILL_LEARNING_USER_REQUEST_INELIGIBLE") in events, "标记取走了却没入队，账上要看得到"


def test_a_failed_enqueue_releases_the_claim_and_keeps_the_work_available(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch, enable_self_learning=True)
    service, tool = agent.skill_learning, agent.tools.tools["skill_summarize"]
    real_enqueue = service.store.enqueue

    def broken(request):
        raise OSError("磁盘满了")

    assert service.enqueue_from_finalize(_ctx("run-1", 1, task_id="task-1", thread_id="th-6")) == "ineligible"
    agent._current_run_params = SimpleNamespace(run_id="run-2", task_attributes={"conversation_thread_id": "th-6"})
    monkeypatch.setattr(service.store, "enqueue", broken)
    with pytest.raises(OSError):
        tool.execute({"target": "previous"})
    monkeypatch.setattr(service.store, "enqueue", real_enqueue)
    assert json.loads(tool.execute({"target": "previous"}).output)["queue"] == "queued", "写入出错撤回认领，不会说已经交过"
    service.request_learning("task-3", "x")
    monkeypatch.setattr(service.store, "enqueue", broken)
    with pytest.raises(OSError):
        service.enqueue_from_finalize(_ctx("run-3", 1, task_id="task-3", thread_id="th-6"))
    monkeypatch.setattr(service.store, "enqueue", real_enqueue)
    agent._current_run_params = SimpleNamespace(run_id="run-4", task_attributes={"conversation_thread_id": "th-6"})
    body = json.loads(tool.execute({"target": "previous"}).output)
    assert body["queue"] == "queued" and body["task_id"] == "task-3", "收尾时入队出错，这次活还能事后再要"


def test_prompt_is_unchanged_for_automatic_requests_and_noted_for_user_requests():
    base = {"user_prompt": "a", "final_response": "b", "tool_rounds": 3, "tool_calls_total": 0, "tool_trace": []}
    automatic = skill_learning_prompt(SkillLearningMaterial(dict(base)))
    assert "user_requested" not in automatic and "用户明确要求" not in automatic
    requested = skill_learning_prompt(SkillLearningMaterial({**base, "requested_by": "user", "user_focus": "改稿顺序",
                                                             "recalled_memories": [{"entry_id": "m1", "kind": "fact",
                                                                                    "content": "短剧每集结尾留钩子"}]}))
    assert "用户明确要求" in requested and '"user_focus": "改稿顺序"' in requested
    assert "recalled_memories" in requested and "短剧每集结尾留钩子" in requested, "召回的记忆进总结提示词"
    assert "recalled_memories" not in automatic
    assert "不写本次任务的具体数据。\n\n待总结材料 JSON：\n{" in automatic, "自动总结的提示词逐字节不变"
    note_end = requested.index("待总结材料 JSON：")
    assert requested.index("用户明确要求") < note_end, "说明放在材料之外，不会被当成历史数据"
    rule = "个人信息，以及只对这一次有效的事实，仍按上面“不要保存”处理，不写进 Skill"
    assert rule in requested[:note_end], "记忆里的个人信息不写进技能（删掉或到期的记忆不能借技能复活）"


def test_user_requested_summary_publishes_through_the_gate_and_can_be_reverted(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_publish import revert_learned_skill
    from agent_py_agent.tests.test_skill_learning import (
        NAME,
        _ctx,
        _learned_file,
        _output,
        _runtime,
    )

    learning = _runtime(tmp_path, _output())
    learning.service.request_learning("task-9", "先列人物再改台词")
    attrs = {"conversation_thread_id": "thread-1", "conversation_task_completed": True, "conversation_task_id": "task-9"}
    assert learning.service.enqueue_from_finalize(_ctx("run-9", tool_rounds=1, task_attributes=attrs)) == "queued"
    assert learning.service.enqueue_from_finalize(_ctx("run-9", tool_rounds=1, task_attributes=attrs)) != "queued"
    assert learning.service.run_pending().status == "published"
    assert "用户明确要求" in learning.backend.prompts[0] and "先列人物再改台词" in learning.backend.prompts[0]
    assert _learned_file(learning).is_file(), "过闸门后写进学来的技能目录，/skills learned 能管"
    assert revert_learned_skill(learning.store, NAME).event == "removed" and not _learned_file(learning).exists()

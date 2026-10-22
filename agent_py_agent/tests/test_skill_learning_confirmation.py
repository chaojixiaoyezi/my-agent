import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning import SkillLearningService
from agent_py_agent.agent.capability.skill_learning_prompt import (
    parse_skill_learning_decision,
)
from agent_py_agent.agent.capability.skill_learning_proposals import (
    learning_proposal,
    list_learning_proposals,
    propose_learning,
    resolve_learning_proposal,
)
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
)
from agent_py_agent.tests.test_skill_learning import BODY, NAME, _ctx, _output, _runtime


def test_real_service_drafts_only_and_requires_exact_user_confirmation(tmp_path):
    ctx = _runtime(tmp_path,_output())
    ctx.service = SkillLearningService(ctx.store,ctx.service.settings,ctx.service.runtime)
    ctx.service.enqueue_from_finalize(_ctx())
    assert ctx.service.run_pending().status == 'proposed'
    assert not ctx.store.skill_path(NAME).exists() and not ctx.store.load_registry().skills
    proposal = list_learning_proposals(ctx.store)[0]
    with pytest.raises(SkillLearningGateError):
        resolve_learning_proposal(ctx.store,proposal['proposal_id'],2,decision='confirm')
    result = resolve_learning_proposal(ctx.store,proposal['proposal_id'],1,decision='confirm')
    assert result['status'] == 'committed' and ctx.store.skill_path(NAME).exists()
    assert resolve_learning_proposal(ctx.store,proposal['proposal_id'],1,decision='confirm') == result


def test_rejection_does_not_write_formal_skill(tmp_path):
    ctx = _runtime(tmp_path,_output())
    ctx.service = SkillLearningService(ctx.store,ctx.service.settings,ctx.service.runtime)
    ctx.service.enqueue_from_finalize(_ctx())
    ctx.service.run_pending()
    proposal = list_learning_proposals(ctx.store)[0]
    assert resolve_learning_proposal(ctx.store,proposal['proposal_id'],1,decision='reject')['status'] == 'rejected'
    assert not ctx.store.skill_path(NAME).exists()
    with pytest.raises(SkillLearningGateError):
        resolve_learning_proposal(ctx.store,proposal['proposal_id'],1,decision='confirm')


def test_user_disable_blocks_previously_proposed_update(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_maintenance import control_learning
    from agent_py_agent.tests.test_skill_learning import BODY, _publish_first
    ctx = _runtime(tmp_path,_output())
    _publish_first(ctx)
    ctx.backend.responses.append(_output('update',update_target=NAME,body=BODY+'\n新候选内容。'))
    raw = SkillLearningService(ctx.store,ctx.service.settings,ctx.service.runtime)
    raw.enqueue_from_finalize(_ctx('run-2',archive_tool_calls=[{'tool':'skill_search','ok':True,'parameters':{'skill_id':f'owner:{NAME}'}}]))
    assert raw.run_pending().status=='proposed'
    candidate = next(row for row in list_learning_proposals(ctx.store) if row['status']=='pending')
    before = ctx.store.skill_path(NAME).read_bytes()
    control_learning(ctx.store,NAME,operation='disable')
    with pytest.raises(SkillLearningGateError):
        resolve_learning_proposal(ctx.store,candidate['proposal_id'],1,decision='confirm')
    assert ctx.store.skill_path(NAME).read_bytes()==before


def test_im_control_reaches_candidate_confirmation_without_cli(tmp_path):
    from agent_py_agent.agent.conversation.control_commands import (
        parse_conversation_control,
    )
    from agent_py_agent.agent.gateway_parts.skill_control_service import (
        SkillControlHost,
        run_skill_control,
    )
    ctx = _runtime(tmp_path,_output())
    raw = SkillLearningService(ctx.store,ctx.service.settings,ctx.service.runtime)
    raw.enqueue_from_finalize(_ctx())
    raw.run_pending()
    proposal = list_learning_proposals(ctx.store)[0]
    # 与Gateway相同用户命令解析器；这里仅替代已认证owner host，不伪造模型批准。
    command = parse_conversation_control(f"/skills learned confirm {proposal['proposal_id']} 1")
    assert command.valid and command.operation=='learning_proposal'
    assert '已确认' in run_skill_control(SkillControlHost(ctx.home,None),command)


# LLM: 通过真实解析器、guard和候选写入生成测试输入，不用伪造候选哈希绕过身份校验。
# 函数用途: 创建指定名字与来源的一份真实学习候选。
def _draft(store, name=NAME, key="draft-1", *, body=BODY, current=None):
    kind = "update" if current else "create"
    draft = parse_skill_learning_decision(
        _output(kind, name=name, body=body, update_target=name if current else ""),
        frozenset({name}) if current else frozenset(),
    )
    request = {"request_key": key, "run_id": key, "task_id": "task", "thread_id": "thread"}
    event = propose_learning(store, PublishRequest(
        draft, request, expected_revision=current.revision if current else None,
        expected_sha256=current.sha256 if current else "",
    ))
    return learning_proposal(store, event.reason)


# LLM: 手动迁移只准备已有归档文件；后续读取和用户确认仍必须走真实产品入口。
# 函数用途: 将候选放到真实overflow目录，独立测试兼容读取而不依赖轮转实现。
def _archive_existing(store, row):
    target = store.directory / "proposals_overflow"
    target.mkdir(exist_ok=True)
    os.replace(store.directory / "proposals" / (row["proposal_id"] + ".json"),
               target / (row["proposal_id"] + ".json"))


# LLM: 满20后新候选仍应创建，归档事件必须在state锁外经原owner账本写入。
# 函数用途: 验证真实容量边界、精确轮转对象与结构化事件。
def test_full_proposals_rotate_oldest_and_keep_new_candidate(tmp_path, monkeypatch):
    store = _runtime(tmp_path).store
    rows = [_draft(store, f"rotate-{index}", str(index)) for index in range(20)]
    original_lock, original_append = store.state_lock, store.append_event
    state_depth = []

    # LLM: 保留原锁与配额，只观察事件写入时是否仍持state锁。
    # 函数用途: 跟踪本次测试真实state临界区。
    @contextmanager
    def observed_lock():
        with original_lock() as admission:
            state_depth.append(True)
            try:
                yield admission
            finally:
                state_depth.pop()

    # LLM: 在调用原append前断言，避免把锁反转实际变成挂死的测试。
    # 函数用途: 验证账本写入不嵌套在state临界区。
    def observed_append(event):
        assert not state_depth, "append_event不能在state_lock内执行"
        return original_append(event)

    monkeypatch.setattr(store, "state_lock", observed_lock)
    monkeypatch.setattr(store, "append_event", observed_append)
    newest = _draft(store, "rotate-new", "new")
    active = list_learning_proposals(store, include_overflow=False)
    all_rows = list_learning_proposals(store, include_overflow=True)
    assert len(active) == 20 and len(all_rows) == 21
    assert rows[0]["proposal_id"] not in {row["proposal_id"] for row in active}
    assert newest["proposal_id"] in {row["proposal_id"] for row in active}
    assert learning_proposal(store, rows[0]["proposal_id"])["revision"] == 1
    assert learning_proposal(store, rows[0]["proposal_id"])["status"] == "pending"
    [event] = store.events(limit=0)
    assert (event["event"], event["code"], event["skill_name"], event["reason"]) == (
        "overflowed", "SKILL_LEARNING_PROPOSAL_OVERFLOW_ROTATED", "rotate-0", rows[0]["proposal_id"],
    )


# LLM: 旧候选没有创建字段时使用物理mtime；刻意选择哈希排序末位来排除按ID伪装先进先出。
# 函数用途: 验证旧文件轮转次序不依赖随机哈希编号。
def test_legacy_proposal_rotation_uses_mtime_not_hash_order(tmp_path):
    store = _runtime(tmp_path).store
    rows = [_draft(store, f"legacy-{index}", str(index)) for index in range(20)]
    oldest = max(rows, key=lambda row: row["proposal_id"])
    for row in rows:
        path = store.directory / "proposals" / (row["proposal_id"] + ".json")
        payload = json.loads(path.read_text())
        payload.pop("created_at_ns", None)
        path.write_text(json.dumps(payload))
        seconds = 1 if row == oldest else 100
        os.utime(path, (seconds, seconds))
    _draft(store, "legacy-new", "new")
    active_ids = {row["proposal_id"] for row in list_learning_proposals(store, include_overflow=False)}
    assert oldest["proposal_id"] not in active_ids
    assert learning_proposal(store, oldest["proposal_id"])["status"] == "pending"


# LLM: 替代只作用同名pending，不发布内容；旧用户确认版本不能越过superseded状态。
# 函数用途: 验证同名新候选归档替代与重复请求幂等。
def test_same_name_pending_is_superseded_and_duplicate_does_not_revive_it(tmp_path):
    store = _runtime(tmp_path).store
    old = _draft(store)
    new = _draft(store, key="new", body=BODY + "\n补充校验顺序。")
    assert [row["proposal_id"] for row in list_learning_proposals(store, include_overflow=False)] == [new["proposal_id"]]
    previous = learning_proposal(store, old["proposal_id"])
    assert previous["status"] == "superseded" and previous["superseded_by"] == new["proposal_id"]
    with pytest.raises(SkillLearningGateError, match="PROPOSAL_CONFLICT"):
        resolve_learning_proposal(store, old["proposal_id"], old["revision"], decision="confirm")
    assert _draft(store)["status"] == "superseded"
    assert len(list_learning_proposals(store, include_overflow=True)) == 2
    assert not store.load_registry().skills


# LLM: 同名旧pending即便已在overflow仍是可确认候选，必须与新草稿一起受替代规则保护。
# 函数用途: 验证同名归档候选不会保留一个可发布的过时确认入口。
def test_same_name_archived_pending_is_also_superseded(tmp_path):
    store = _runtime(tmp_path).store
    old = _draft(store)
    _archive_existing(store, old)
    _draft(store, key="replacement")
    assert learning_proposal(store, old["proposal_id"])["status"] == "superseded"


# LLM: 归档不是批准；仍要求准确revision并保留原用户actor、同决定重放及冲突拒绝。
# 函数用途: 验证归档候选的真实读取、确认或拒绝及幂等。
@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_archived_candidate_resolves_through_exact_user_revision(tmp_path, decision):
    store = _runtime(tmp_path).store
    row = _draft(store)
    _archive_existing(store, row)
    assert list_learning_proposals(store, include_overflow=False) == []
    assert learning_proposal(store, row["proposal_id"]) == row
    assert list_learning_proposals(store, include_overflow=True) == [row]
    with pytest.raises(SkillLearningGateError, match="PROPOSAL_CONFLICT"):
        resolve_learning_proposal(store, row["proposal_id"], 9, decision=decision)
    result = resolve_learning_proposal(store, row["proposal_id"], 1, decision=decision, actor_id="user-7")
    assert result["status"] == ("committed" if decision == "confirm" else "rejected")
    assert result["revision"] == 2 and result["resolved_by"]["actor_id"] == "user-7"
    assert store.skill_path(NAME).exists() == (decision == "confirm")
    assert not (store.directory / "proposals" / (row["proposal_id"] + ".json")).exists()
    assert resolve_learning_proposal(store, row["proposal_id"], 1, decision=decision) == result
    other = "reject" if decision == "confirm" else "confirm"
    with pytest.raises(SkillLearningGateError, match="PROPOSAL_CONFLICT"):
        resolve_learning_proposal(store, row["proposal_id"], 1, decision=other)
    assert len(store.events(limit=0)) == (1 if decision == "confirm" else 0)


# LLM: 归档更新仍经publisher的原revision/hash CAS，不能把位置变化当成更新权限。
# 函数用途: 验证已失效归档更新及用户手改内容均不被覆盖。
@pytest.mark.parametrize("change", ["revision", "user_content"])
def test_archived_update_keeps_publisher_cas_and_user_content_gate(tmp_path, change):
    store = _runtime(tmp_path).store
    initial = _draft(store)
    resolve_learning_proposal(store, initial["proposal_id"], 1, decision="confirm")
    record = store.load_registry().skills[NAME]
    candidate = _draft(store, key="archived-update", body=BODY + "\n归档的更新建议。", current=record)
    _archive_existing(store, candidate)
    if change == "revision":
        from agent_py_agent.agent.capability.skill_learning_publish import publish_learned_skill
        decision = parse_skill_learning_decision(
            _output("update", update_target=NAME, body=BODY + "\n较新的实际更新。"), frozenset({NAME}),
        )
        publish_learned_skill(store, PublishRequest(
            decision, expected_revision=record.revision, expected_sha256=record.sha256,
        ))
    else:
        path = store.skill_path(NAME)
        path.write_text(path.read_text() + "\n用户自己的内容。")
    before = store.skill_path(NAME).read_bytes()
    code = "REVISION_CONFLICT" if change == "revision" else "TARGET_USER_OWNED"
    with pytest.raises(SkillLearningGateError, match=code):
        resolve_learning_proposal(store, candidate["proposal_id"], 1, decision="confirm")
    assert store.skill_path(NAME).read_bytes() == before
    assert learning_proposal(store, candidate["proposal_id"])["status"] == "pending"


# LLM: 归档正文继续绑定原身份哈希，篡改后不能从展示或确认路径读取为可信草稿。
# 函数用途: 验证归档身份校验不被新增查找路径绕过。
def test_archived_candidate_hash_tampering_fails_closed(tmp_path):
    store = _runtime(tmp_path).store
    row = _draft(store)
    _archive_existing(store, row)
    path = store.directory / "proposals_overflow" / (row["proposal_id"] + ".json")
    row["decision"]["body"] += "\n被替换的正文。"
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="已被修改"):
        learning_proposal(store, row["proposal_id"])
    with pytest.raises(ValueError, match="已被修改"):
        resolve_learning_proposal(store, row["proposal_id"], 1, decision="confirm")
    assert not store.load_registry().skills


# LLM: 使用默认真实容量而非替换上限；超过100条时只移除最早归档，不影响主区20条。
# 函数用途: 验证overflow有界保留及仍保留的pending可拒绝。
def test_proposal_overflow_retains_at_most_100_records(tmp_path):
    store = _runtime(tmp_path).store
    rows = [_draft(store, f"capacity-{index}", str(index)) for index in range(121)]
    active = list_learning_proposals(store, include_overflow=False)
    all_rows = list_learning_proposals(store, include_overflow=True)
    assert len(active) == 20 and len(all_rows) == 120
    with pytest.raises(ValueError):
        learning_proposal(store, rows[0]["proposal_id"])
    assert learning_proposal(store, rows[1]["proposal_id"])["status"] == "pending"
    assert resolve_learning_proposal(store, rows[1]["proposal_id"], 1, decision="reject")["status"] == "rejected"
    assert len(list((store.directory / "proposals_overflow").glob("learning-proposal-*.json"))) == 100


# LLM: nofollow必须同时覆盖主区/归档目录和归档叶，且拒绝时不触碰链接指向的用户数据。
# 函数用途: 验证符号链接边界不能被展示、确认或新建绕过。
@pytest.mark.parametrize("location", ["active_directory", "overflow_directory", "overflow_leaf"])
def test_proposal_symlink_boundaries_fail_closed(tmp_path, location):
    store = _runtime(tmp_path).store
    row = _draft(store)
    outside = tmp_path / "private-user-data"
    outside.mkdir()
    if location == "active_directory":
        original = store.directory / "proposals"
        original.rename(store.directory / "original-proposals")
        original.symlink_to(outside, target_is_directory=True)
    elif location == "overflow_directory":
        (store.directory / "proposals_overflow").symlink_to(outside, target_is_directory=True)
    else:
        _archive_existing(store, row)
        original = store.directory / "proposals_overflow" / (row["proposal_id"] + ".json")
        original.rename(outside / "user.json")
        original.symlink_to(outside / "user.json")
    before = {path.name: path.read_bytes() for path in outside.iterdir()}
    with pytest.raises((ValueError, OSError)):
        list_learning_proposals(store, include_overflow=True)
    with pytest.raises((ValueError, OSError)):
        resolve_learning_proposal(store, row["proposal_id"], 1, decision="confirm")
    with pytest.raises((ValueError, OSError)):
        _draft(store, key="new")
    assert {path.name: path.read_bytes() for path in outside.iterdir()} == before
    assert not store.load_registry().skills


# LLM: 暂停真正publisher入口，让轮转与确认实际并发；轮转不能覆盖已提交结果或恢复pending。
# 函数用途: 验证共享候选互斥区横跨发布和归档迁移。
def test_concurrent_confirmation_and_rotation_do_not_lose_resolution(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_proposals as proposals
    store = _runtime(tmp_path).store
    rows = [_draft(store, f"concurrent-{index}", str(index)) for index in range(20)]
    entered, release, rotating = Event(), Event(), Event()
    original_publish = proposals.publish_learned_skill

    # LLM: 只暂停时序，仍执行真实发布闸门与registry写入。
    # 函数用途: 在确认读到pending后给轮转制造竞争窗口。
    def paused_publish(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original_publish(*args, **kwargs)

    # LLM: 并发请求走真实propose，不直接改文件伪造竞态结果。
    # 函数用途: 在第二线程启动一个会触发满容量检查的新候选。
    def rotate():
        rotating.set()
        return _draft(store, "concurrent-new", "new")

    monkeypatch.setattr(proposals, "publish_learned_skill", paused_publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmation = pool.submit(resolve_learning_proposal, store, rows[0]["proposal_id"], 1, decision="confirm")
        try:
            assert entered.wait(10)
            rotation = pool.submit(rotate)
            assert rotating.wait(10)
        finally:
            release.set()
        result = confirmation.result(timeout=10)
        rotation.result(timeout=10)
    assert result["status"] == "committed"
    assert learning_proposal(store, rows[0]["proposal_id"]) == result
    assert sum(row["status"] == "pending" for row in list_learning_proposals(store, include_overflow=False)) == 20
    assert len(store.events("concurrent-0", limit=0)) == 1


# LLM: 两个真实用户决定同时提交同一revision时只有一个可生效，另一个必须冲突而非静默覆盖。
# 函数用途: 验证归档confirm/reject竞争仍保持单次CAS确认。
def test_archived_conflicting_resolutions_are_serialized(tmp_path):
    store = _runtime(tmp_path).store
    row = _draft(store)
    _archive_existing(store, row)

    # LLM: 保留明确冲突作为结果，不吞其它存储或发布错误。
    # 函数用途: 返回本次决定的真实状态或精确候选冲突码。
    def resolve(decision):
        try:
            return resolve_learning_proposal(store, row["proposal_id"], 1, decision=decision)["status"]
        except SkillLearningGateError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resolve, ["confirm", "reject"]))
    assert results.count("SKILL_LEARNING_PROPOSAL_CONFLICT") == 1
    final = learning_proposal(store, row["proposal_id"])
    assert final["status"] in {"committed", "rejected"} and final["revision"] == 2
    assert store.skill_path(NAME).exists() == (final["status"] == "committed")


# LLM: 轮转产物直接走用户确认入口，不使用手工归档fixture代替关键主链路。
# 函数用途: 验证第21份候选触发的真实归档可确认或拒绝，重复决定不重复发布。
@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_rotated_candidate_end_to_end_resolution(tmp_path, decision):
    store = _runtime(tmp_path).store
    rows = [_draft(store, f"e2e-{index}", str(index)) for index in range(21)]
    old = rows[0]
    result = resolve_learning_proposal(store, old["proposal_id"], old["revision"], decision=decision)
    assert result["status"] == ("committed" if decision == "confirm" else "rejected")
    assert resolve_learning_proposal(store, old["proposal_id"], old["revision"], decision=decision) == result
    assert store.skill_path("e2e-0").exists() == (decision == "confirm")
    path = store.directory / "proposals_overflow" / (old["proposal_id"] + ".json")
    assert json.loads(path.read_text()) == result
    assert len(list_learning_proposals(store, include_overflow=False)) == 20


# LLM: 使用真实owner配额与原state锁，空间不足不能借归档新增写入，也不能改掉旧候选状态。
# 函数用途: 验证原owner磁盘限制在同名替代入口仍生效。
def test_proposal_replacement_obeys_real_owner_quota(tmp_path):
    from agent_py_agent.agent.user_space.owner_quota import (
        OwnerQuotaExceeded,
        owner_logical_usage_bytes,
    )
    ctx = _runtime(tmp_path)
    store = ctx.store
    old = _draft(store)
    quota = ctx.home.owner_home_dir / "quota.json"
    config = json.loads(quota.read_text())
    config["max_disk_mb"] = 1
    quota.write_text(json.dumps(config))
    used = owner_logical_usage_bytes(ctx.home.owner_home_dir)
    assert used < 1024 * 1024
    (ctx.home.owner_home_dir / "quota-fill.bin").write_bytes(b"x" * (1024 * 1024 - used))
    with pytest.raises(OwnerQuotaExceeded):
        _draft(store, key="would-replace", body=BODY + "\n空间不够的新候选。")
    assert learning_proposal(store, old["proposal_id"]) == old
    assert list_learning_proposals(store, include_overflow=True) == [old]


# LLM: 配额按整个轮转的最终变化计算，搬移同一候选不能被误算成长期重复占用两份空间。
# 函数用途: 验证接近owner配额时可完成最终大小仍合规的同名替代。
def test_replacement_quota_projects_the_complete_rotation(tmp_path):
    from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes
    ctx = _runtime(tmp_path)
    store = ctx.store
    old = _draft(store)
    path = store.directory / "proposals" / (old["proposal_id"] + ".json")
    allowance = path.stat().st_size + 256
    quota = ctx.home.owner_home_dir / "quota.json"
    config = json.loads(quota.read_text())
    config["max_disk_mb"] = 1
    quota.write_text(json.dumps(config))
    used = owner_logical_usage_bytes(ctx.home.owner_home_dir)
    assert used + allowance < 1024 * 1024
    (ctx.home.owner_home_dir / "quota-fill.bin").write_bytes(b"x" * (1024 * 1024 - used - allowance))
    new = _draft(store, key="draft-2")
    assert new["status"] == "pending"
    assert learning_proposal(store, old["proposal_id"])["status"] == "superseded"
    assert owner_logical_usage_bytes(ctx.home.owner_home_dir) <= 1024 * 1024


# LLM: 注入一次真实writer失败，不让新候选和旧位置同时残留为两个可确认pending。
# 函数用途: 验证轮转多文件写入失败时恢复旧候选与索引。
def test_rotation_write_failure_restores_preexisting_candidate(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_proposals as proposals
    store = _runtime(tmp_path).store
    old = _draft(store)
    original_write = proposals.write_text_atomic_beneath
    failed = []

    # LLM: 只在归档目标故障一次，后续恢复仍执行真实no-follow writer。
    # 函数用途: 模拟候选轮转途中磁盘写失败。
    def failing_write(root, parts, text):
        if parts[0] == "proposals_overflow" and not failed:
            failed.append(True)
            raise OSError("injected archive write failure")
        return original_write(root, parts, text)

    monkeypatch.setattr(proposals, "write_text_atomic_beneath", failing_write)
    with pytest.raises(OSError, match="injected archive"):
        _draft(store, key="draft-2")
    assert list_learning_proposals(store, include_overflow=True) == [old]
    assert learning_proposal(store, old["proposal_id"]) == old


# LLM: 子进程在真实原子写或删除完成后硬退出，不允许异常回滚替代真实进程崩溃。
# 函数用途: 在指定候选落盘前缀终止独立解释器，并核对真实退出码。
def _crash_draft(tmp_path, *, name=NAME, key="replacement", point="archive", ordinal=1):
    script = '''
import json
import os
import sys
from pathlib import Path
from agent_py_agent.agent.capability import skill_learning_proposals as proposals
from agent_py_agent.tests.test_skill_learning import _runtime
from agent_py_agent.tests.test_skill_learning_confirmation import _draft

root, name, key, point, ordinal = json.loads(sys.argv[1])
store = _runtime(Path(root)).store
write, unlink = proposals.write_text_atomic_beneath, proposals.unlink_file_beneath
counts = {}
rolling_back = False

# LLM: 只观察真实写入完成点，保留原no-follow、fsync与配额调用。
# 函数用途: 在请求的原子写前缀完成后模拟断电式退出。
def crash_write(root, parts, text):
    global rolling_back
    if point.startswith('rollback_') and parts[0] == 'proposals_overflow' and not rolling_back:
        rolling_back = True
        raise OSError('injected archive failure before rollback crash')
    result = write(root, parts, text)
    phase = 'rollback_write' if rolling_back else 'new' if parts[0] == 'proposals' else 'archive' if parts[0] == 'proposals_overflow' else 'intent'
    counts[phase] = counts.get(phase, 0) + 1
    if phase == point and counts[phase] == ordinal:
        os._exit(91)
    return result

# LLM: 删除本身仍走真实no-follow原语，只在成功返回后终止进程。
# 函数用途: 覆盖旧位置清理、归档裁剪及意图清理的崩溃窗口。
def crash_unlink(root, parts):
    result = unlink(root, parts)
    phase = 'old_delete' if parts[0] == 'proposals' else 'trim_delete' if parts[0] == 'proposals_overflow' else 'cleanup'
    if rolling_back:
        phase = 'rollback_new_delete' if parts[0] == 'proposals' else 'rollback_archive_delete'
    counts[phase] = counts.get(phase, 0) + 1
    if phase == point and counts[phase] == ordinal:
        os._exit(91)
    return result

proposals.write_text_atomic_beneath = crash_write
proposals.unlink_file_beneath = crash_unlink
_draft(store, name, key)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps([str(tmp_path), name, key, point, ordinal])],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 91, result.stdout + result.stderr


# LLM: 重现审查中的active/pending和overflow/superseded双份，读取不得写业务数据或复活旧确认。
# 函数用途: 验证归档写完后硬崩溃仍可只读展示，并在新写入口落盘收敛。
def test_subprocess_archive_crash_projects_superseded_and_recovers(tmp_path):
    store = _runtime(tmp_path).store
    old = _draft(store)
    _crash_draft(tmp_path)
    store = _runtime(tmp_path).store
    before = {str(path.relative_to(store.directory)): path.read_bytes()
              for path in store.directory.rglob("*") if path.is_file()}
    active = list_learning_proposals(store, include_overflow=False)
    all_rows = list_learning_proposals(store, include_overflow=True)
    previous = learning_proposal(store, old["proposal_id"])
    assert previous["status"] == "superseded" and previous["revision"] == 2
    assert len(active) == 1 and len(all_rows) == 2
    assert previous["superseded_by"] == active[0]["proposal_id"]
    after = {str(path.relative_to(store.directory)): path.read_bytes()
             for path in store.directory.rglob("*") if path.is_file()}
    assert after == before
    with pytest.raises(SkillLearningGateError, match="PROPOSAL_CONFLICT"):
        resolve_learning_proposal(store, old["proposal_id"], 1, decision="confirm")
    assert not store.load_registry().skills
    _draft(store, "unrelated", "unrelated")
    assert len(list_learning_proposals(store, include_overflow=True)) == 3
    assert not (store.directory / "proposals" / (old["proposal_id"] + ".json")).exists()
    assert _draft(store)["status"] == "superseded"


# LLM: 从意图刷盘到清理完成逐个硬退出；未写新候选保持原态，其余前缀统一投影为替代后状态。
# 函数用途: 验证同名替代每个写删前缀的只读状态与最终幂等收敛。
@pytest.mark.parametrize("point", ["intent", "new", "archive", "old_delete", "cleanup"])
def test_subprocess_replacement_every_durable_prefix(tmp_path, point):
    store = _runtime(tmp_path).store
    old = _draft(store)
    _crash_draft(tmp_path, point=point)
    store = _runtime(tmp_path).store
    before = {str(path): path.read_bytes() for path in store.directory.rglob("*") if path.is_file()}
    rows = list_learning_proposals(store, include_overflow=True)
    previous = learning_proposal(store, old["proposal_id"])
    committed = point != "intent"
    assert previous["status"] == ("superseded" if committed else "pending")
    assert len(rows) == (2 if committed else 1)
    assert len(list_learning_proposals(store, include_overflow=False)) == 1
    assert {str(path): path.read_bytes() for path in store.directory.rglob("*") if path.is_file()} == before
    new = _draft(store, key="replacement")
    assert new["status"] == "pending"
    assert learning_proposal(store, old["proposal_id"])["status"] == "superseded"
    assert not (store.directory / ".proposals-intent.json").exists()
    assert len(list_learning_proposals(store, include_overflow=True)) == 2
    assert not (store.directory / "proposals" / (old["proposal_id"] + ".json")).exists()


# LLM: 满额轮转与100条裁剪均用真实上限；新候选已落盘但旧归档尚未写时也不能对外暴露第21条active。
# 函数用途: 验证满容量迁移和裁剪的每个前缀都保持20/100投影，并能恢复落盘。
@pytest.mark.parametrize("point", ["new", "archive", "old_delete", "trim_delete", "cleanup"])
def test_subprocess_capacity_and_archive_trim_prefixes(tmp_path, point):
    store = _runtime(tmp_path).store
    rows = [_draft(store, f"crash-capacity-{index}", str(index)) for index in range(120)]
    _crash_draft(tmp_path, name="capacity-new", point=point)
    store = _runtime(tmp_path).store
    before = {str(path): path.read_bytes() for path in store.directory.rglob("*") if path.is_file()}
    assert len(list_learning_proposals(store, include_overflow=False)) == 20
    assert len(list_learning_proposals(store, include_overflow=True)) == 120
    with pytest.raises(ValueError):
        learning_proposal(store, rows[0]["proposal_id"])
    assert learning_proposal(store, rows[100]["proposal_id"])["status"] == "pending"
    assert {str(path): path.read_bytes() for path in store.directory.rglob("*") if path.is_file()} == before
    _draft(store, "capacity-new", "replacement")
    assert not (store.directory / ".proposals-intent.json").exists()
    assert len(list((store.directory / "proposals").glob("learning-proposal-*.json"))) == 20
    assert len(list((store.directory / "proposals_overflow").glob("learning-proposal-*.json"))) == 100
    with pytest.raises(ValueError):
        learning_proposal(store, rows[0]["proposal_id"])


# LLM: 恢复过程再次在写归档、删主区或裁剪点硬退出，不能递增修订、丢正文或遗留永久双份。
# 函数用途: 从同一未恢复前缀分别验证恢复再次崩溃，独立于删除集合的遍历顺序。
@pytest.mark.parametrize("recovery_point", ["archive", "old_delete", "trim_delete"])
def test_subprocess_recovery_itself_can_crash_repeatedly(tmp_path, recovery_point):
    store = _runtime(tmp_path).store
    rows = [_draft(store, f"recover-{index}", str(index)) for index in range(120)]
    _crash_draft(tmp_path, name="recover-new", point="new")
    expected = sorted(list_learning_proposals(store, include_overflow=True), key=lambda row: row["proposal_id"])
    _crash_draft(tmp_path, name="recover-new", key="replacement", point=recovery_point)
    store = _runtime(tmp_path).store
    assert sorted(list_learning_proposals(store, include_overflow=True), key=lambda row: row["proposal_id"]) == expected
    assert learning_proposal(store, rows[100]["proposal_id"])["revision"] == 1
    _draft(store, "recover-new", "replacement")
    assert not (store.directory / ".proposals-intent.json").exists()
    assert len(list((store.directory / "proposals").glob("learning-proposal-*.json"))) == 20
    assert len(list((store.directory / "proposals_overflow").glob("learning-proposal-*.json"))) == 100


# LLM: 崩溃投影中的归档pending仍需原用户revision，确认/拒绝入口必须先恢复且同决定重放不重复发布。
# 函数用途: 覆盖真实满20轮转中断后归档确认、拒绝、幂等及actor审计主链路。
@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_subprocess_rotated_candidate_can_resolve_after_restart(tmp_path, decision):
    store = _runtime(tmp_path).store
    old = [_draft(store, f"resolve-crash-{index}", str(index)) for index in range(20)][0]
    _crash_draft(tmp_path, name="resolve-new", point="new")
    store = _runtime(tmp_path).store
    with pytest.raises(SkillLearningGateError, match="PROPOSAL_CONFLICT"):
        resolve_learning_proposal(store, old["proposal_id"], 7, decision=decision)
    result = resolve_learning_proposal(store, old["proposal_id"], 1, decision=decision, actor_id="restart-user")
    assert result["status"] == ("committed" if decision == "confirm" else "rejected")
    assert result["revision"] == 2 and result["resolved_by"]["actor_id"] == "restart-user"
    assert resolve_learning_proposal(store, old["proposal_id"], 1, decision=decision) == result
    assert store.skill_path("resolve-crash-0").exists() == (decision == "confirm")
    assert learning_proposal(store, old["proposal_id"]) == result
    assert not (store.directory / ".proposals-intent.json").exists()


# LLM: 无意图表示旧版本产物，只有完整可证明的归档/替代关系可以收敛，不能重开旧pending确认。
# 函数用途: 验证旧版本遗留的双位置崩溃产物可只读查询并由现有写入口修复。
@pytest.mark.parametrize("superseded", [True, False])
def test_legacy_crash_duplicate_converges_without_an_intent(tmp_path, superseded):
    store = _runtime(tmp_path).store
    old = _draft(store)
    if superseded:
        _crash_draft(tmp_path)
    else:
        for index in range(19):
            _draft(store, f"legacy-crash-{index}", str(index))
        _crash_draft(tmp_path, name="legacy-crash-new")
    (store.directory / ".proposals-intent.json").unlink()
    before = {str(path): path.read_bytes() for path in store.directory.rglob("*") if path.is_file()}
    previous = learning_proposal(store, old["proposal_id"])
    assert previous["status"] == ("superseded" if superseded else "pending")
    assert len(list_learning_proposals(store, include_overflow=False)) == (1 if superseded else 20)
    assert {str(path): path.read_bytes() for path in store.directory.rglob("*") if path.is_file()} == before
    if superseded:
        with pytest.raises(SkillLearningGateError, match="PROPOSAL_CONFLICT"):
            resolve_learning_proposal(store, old["proposal_id"], 1, decision="confirm")
    _draft(store, "legacy-unrelated", "legacy-unrelated")
    assert not (store.directory / "proposals" / (old["proposal_id"] + ".json")).exists()
    assert learning_proposal(store, old["proposal_id"]) == previous


# LLM: 恢复描述不是旁路，目录、叶链接、字节上限、schema和全记录哈希都必须在读写入口fail closed。
# 函数用途: 验证中断意图与候选被篡改后不会被恢复逻辑信任或触及链接目标。
@pytest.mark.parametrize("damage", ["symlink", "hardlink", "oversize", "json", "path", "folder_type", "bool_schema", "hash", "candidate"])
def test_crash_intent_and_candidate_damage_fail_closed(tmp_path, damage):
    store = _runtime(tmp_path).store
    old = _draft(store)
    _crash_draft(tmp_path, point="new")
    path = store.directory / ".proposals-intent.json"
    outside = tmp_path / "protected-intent.json"
    outside.write_bytes(path.read_bytes())
    protected = outside.read_bytes()
    if damage in {"symlink", "hardlink"}:
        path.unlink()
        if damage == "symlink":
            path.symlink_to(outside)
        else:
            try:
                os.link(outside, path)
            except PermissionError as error:
                pytest.skip(f"当前执行环境拒绝创建真实硬链接，未验证该观察点：{error}")
    elif damage == "oversize":
        path.write_bytes(b"x" * 65537)
    elif damage == "json":
        path.write_text("{")
    elif damage == "candidate":
        candidate = store.directory / "proposals" / (old["proposal_id"] + ".json")
        row = json.loads(candidate.read_text())
        row["status"] = "rejected"
        candidate.write_text(json.dumps(row))
    else:
        intent = json.loads(path.read_text())
        if damage == "path":
            intent["changes"][0][0] = "../outside"
        elif damage == "folder_type":
            intent["changes"][0][0] = []
        elif damage == "bool_schema":
            intent["v"] = True
        else:
            intent["changes"][0][3] = "f" * 64
        path.write_text(json.dumps(intent))
    before = {str(item): item.read_bytes() for item in (store.directory / "proposals").iterdir() if item.is_file()}
    with pytest.raises((ValueError, OSError)):
        list_learning_proposals(store, include_overflow=True)
    with pytest.raises((ValueError, OSError)):
        learning_proposal(store, old["proposal_id"])
    with pytest.raises((ValueError, OSError)):
        _draft(store, "unrelated", "unrelated")
    with pytest.raises((ValueError, OSError)):
        resolve_learning_proposal(store, old["proposal_id"], 1, decision="confirm")
    assert {str(item): item.read_bytes() for item in (store.directory / "proposals").iterdir() if item.is_file()} == before
    assert outside.read_bytes() == protected
    assert not store.load_registry().skills


# LLM: 普通IO失败必须仍回滚；若回滚自身硬退出，新候选最后删除的提交点决定唯一可验证状态。
# 函数用途: 验证回滚的每个恢复写/删前缀均不会留下永久阻塞或重开旧确认。
@pytest.mark.parametrize("point", ["rollback_write", "rollback_archive_delete", "rollback_new_delete"])
def test_subprocess_ordinary_io_rollback_can_itself_crash(tmp_path, point):
    store = _runtime(tmp_path).store
    old = _draft(store)
    _crash_draft(tmp_path, point=point)
    store = _runtime(tmp_path).store
    committed = point != "rollback_new_delete"
    assert learning_proposal(store, old["proposal_id"])["status"] == ("superseded" if committed else "pending")
    assert len(list_learning_proposals(store, include_overflow=True)) == (2 if committed else 1)
    _draft(store, key="replacement")
    assert learning_proposal(store, old["proposal_id"])["status"] == "superseded"
    assert not (store.directory / ".proposals-intent.json").exists()
    assert not (store.directory / "proposals" / (old["proposal_id"] + ".json")).exists()


# LLM: 普通原子writer在替换后仍可能报fsync错误，回滚必须恢复归档旧正文及revision而不静默提交。
# 函数用途: 验证同名归档pending原位替代遇到提交后IO异常时完整恢复原态。
def test_archived_replacement_post_write_io_failure_rolls_back(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_proposals as proposals
    store = _runtime(tmp_path).store
    old = _draft(store)
    _archive_existing(store, old)
    original_write = proposals.write_text_atomic_beneath
    failed = []

    # LLM: 首次归档替换真实发生后抛错，后续回滚继续使用真实原子writer而非伪造成功。
    # 函数用途: 模拟原位归档替代完成后目录刷盘失败。
    def fail_after_archive_write(root, parts, text):
        result = original_write(root, parts, text)
        if parts[0] == "proposals_overflow" and not failed:
            failed.append(True)
            raise OSError("injected post-write archive failure")
        return result

    monkeypatch.setattr(proposals, "write_text_atomic_beneath", fail_after_archive_write)
    with pytest.raises(OSError, match="post-write"):
        _draft(store, key="replacement")
    assert learning_proposal(store, old["proposal_id"]) == old
    assert list_learning_proposals(store, include_overflow=True) == [old]
    assert not (store.directory / ".proposals-intent.json").exists()


# LLM: 兼容旧版本主区与归档同时含同名pending，必须替代每一份而非只处理第一个匹配。
# 函数用途: 验证多个同名来源在部分归档覆盖后崩溃仍全部收敛为superseded。
@pytest.mark.parametrize("ordinal", [1, 2])
def test_subprocess_replaces_all_same_name_active_and_archived_pending(tmp_path, ordinal):
    store = _runtime(tmp_path).store
    active_old = _draft(store)
    legacy_store = _runtime(tmp_path / "legacy-source").store
    archived_old = _draft(legacy_store, key="legacy-archived")
    target = store.directory / "proposals_overflow"
    target.mkdir()
    (target / (archived_old["proposal_id"] + ".json")).write_bytes(
        (legacy_store.directory / "proposals" / (archived_old["proposal_id"] + ".json")).read_bytes(),
    )
    _crash_draft(tmp_path, point="archive", ordinal=ordinal)
    store = _runtime(tmp_path).store
    for old in (active_old, archived_old):
        previous = learning_proposal(store, old["proposal_id"])
        assert previous["status"] == "superseded" and previous["revision"] == 2
    assert len(list_learning_proposals(store, include_overflow=False)) == 1
    assert len(list_learning_proposals(store, include_overflow=True)) == 3
    _draft(store, "unrelated", "unrelated")
    assert _draft(store, key="legacy-archived")["status"] == "superseded"
    assert _draft(store)["status"] == "superseded"
    assert not (store.directory / ".proposals-intent.json").exists()


# LLM: 恢复仅修复存储，不批准内容；用户停用在恢复后仍必须由原publisher闸门拒绝。
# 函数用途: 验证真实崩溃归档候选不能借重启恢复绕过用户停用保护。
def test_subprocess_recovered_candidate_still_obeys_user_disable(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_maintenance import control_learning
    store = _runtime(tmp_path).store
    initial = _draft(store)
    resolve_learning_proposal(store, initial["proposal_id"], 1, decision="confirm")
    current = store.load_registry().skills[NAME]
    pending = _draft(store, key="update-before-crash", body=BODY + "\n待确认更新。", current=current)
    for index in range(19):
        _draft(store, f"guard-crash-{index}", str(index))
    _crash_draft(tmp_path, name="guard-new", point="new")
    control_learning(store, NAME, operation="disable")
    before = store.skill_path(NAME).read_bytes()
    with pytest.raises(SkillLearningGateError):
        resolve_learning_proposal(store, pending["proposal_id"], 1, decision="confirm")
    assert store.skill_path(NAME).read_bytes() == before
    assert learning_proposal(store, pending["proposal_id"])["status"] == "pending"
    assert not (store.directory / ".proposals-intent.json").exists()


# LLM: 真正填满并轮转更新候选；查询前后逐字核对业务文件，避免把显式单条查找当成列表可见。
# 函数用途: 验证默认读口、CLI、IM及维护保护均能发现overflow中的pending更新。
@pytest.mark.parametrize("surface", ["default", "cli", "im", "protection"])
def test_overflow_pending_is_visible_through_real_surfaces(tmp_path, monkeypatch, capsys, surface):
    ctx = _runtime(tmp_path)
    first = _draft(ctx.store)
    resolve_learning_proposal(ctx.store, first["proposal_id"], 1, decision="confirm")
    pending = _draft(ctx.store, key="update-to-protect", body=BODY + "\n待审的新版本。",
                     current=ctx.store.load_registry().skills[NAME])
    for index in range(20):
        _draft(ctx.store, f"visible-{index}", str(index))
    assert (ctx.store.directory / "proposals_overflow" / (pending["proposal_id"] + ".json")).is_file()
    before = {str(path): path.read_bytes() for path in ctx.home.owner_home_dir.rglob("*") if path.is_file()}
    if surface == "default":
        assert pending["proposal_id"] in {row["proposal_id"] for row in list_learning_proposals(ctx.store)}
        assert pending["proposal_id"] not in {row["proposal_id"] for row in list_learning_proposals(ctx.store, include_overflow=False)}
    elif surface == "cli":
        from agent_py_agent.cli import skill_learning_commands as cli
        monkeypatch.setattr(cli, "_owner_context", lambda _args: cli._LearnedCommandContext(ctx.store, "main", None))
        assert cli.cmd_learning_proposals(SimpleNamespace(skills_learned_action="proposals", json=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert pending["proposal_id"] in {row["proposal_id"] for row in payload["proposals"]}
    elif surface == "im":
        from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
        from agent_py_agent.agent.gateway_parts.skill_control_service import (
            SkillControlHost,
            run_skill_control,
        )
        text = run_skill_control(SkillControlHost(ctx.home, None), parse_conversation_control("/skills learned proposals"))
        assert pending["proposal_id"] in text and NAME in text
    else:
        from agent_py_agent.agent.capability.skill_learning_protection import (
            learning_protection_for_agent,
        )
        agent = SimpleNamespace(
            home_paths=ctx.home,
            conversation_store=SimpleNamespace(tasks=SimpleNamespace(storage=SimpleNamespace(tasks_dir=tmp_path / "no-tasks"))),
            scheduler_repository=SimpleNamespace(list_jobs=lambda **_kwargs: ([], [])),
            subagents=SimpleNamespace(list_runs_report=lambda: SimpleNamespace(load_errors=[], runs=[])),
        )
        result = learning_protection_for_agent(agent)
        assert result["protection_complete"] and f"owner:{NAME}" in result["protected_ids"]
    assert {str(path): path.read_bytes() for path in ctx.home.owner_home_dir.rglob("*") if path.is_file()} == before


# LLM: 观察原quota、state和proposals真实锁的进入顺序，错误时在阻塞前断言，不以空context替换互斥。
# 函数用途: 验证所有提案入口都遵守owner配额→state→proposals且publisher不会重复取不可重入锁。
@pytest.mark.parametrize("operation", ["propose", "read", "list", "confirm", "reject"])
def test_proposal_entrances_use_global_lock_order(tmp_path, monkeypatch, operation):
    from agent_py_agent.agent.capability import skill_learning_proposals as proposals
    from agent_py_agent.agent.capability import skill_learning_store as stores
    from agent_py_agent.agent.common import json_io
    from agent_py_agent.agent.user_space import owner_quota
    ctx = _runtime(tmp_path)
    quota = ctx.home.owner_home_dir / "quota.json"
    quota.write_text(json.dumps({**json.loads(quota.read_text()), "max_disk_mb": 16}))
    store = ctx.store
    row = _draft(store)
    original, held, acquired = json_io.locked_json_path, [], []
    ranks = {".owner-quota": 0, ".state": 1, ".proposals": 2}

    # LLM: 保留底层线程/flock和原admission；缺少外层锁与重入都明确失败，避免变成超时。
    # 函数用途: 记录并校验这一次真实锁嵌套。
    @contextmanager
    def observe(path, **kwargs):
        name = Path(path).name
        if name in ranks:
            assert held == list(ranks)[:ranks[name]], f"锁顺序错误: {held} -> {name}"
        with original(path, **kwargs):
            if name in ranks:
                held.append(name)
                acquired.append(name)
            try:
                yield
            finally:
                if name in ranks:
                    assert held.pop() == name

    for module in (proposals, stores, owner_quota):
        monkeypatch.setattr(module, "locked_json_path", observe)
    if operation == "propose":
        _draft(store, "lock-ordered-new", "new")
    elif operation == "read":
        assert learning_proposal(store, row["proposal_id"]) == row
    elif operation == "list":
        assert list_learning_proposals(store) == [row]
    else:
        result = resolve_learning_proposal(store, row["proposal_id"], 1, decision=operation)
        assert result["status"] == ("committed" if operation == "confirm" else "rejected")
    assert acquired[:3] == list(ranks) and not held
    assert store._quota_admission.get() is None


# LLM: 首次查询没有业务目录时不创建配额/state/proposals锁；未知编号仍按原合同拒绝。
# 函数用途: 保证新增统一锁序不会把无目录只读查询变成初始化写入。
def test_empty_proposal_reads_do_not_create_any_lock(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
    store = SkillLearningStore(tmp_path / "owner" / "data" / "skill_learning", tmp_path / "owner" / "skills")
    assert list_learning_proposals(store) == []
    with pytest.raises(ValueError):
        learning_proposal(store, "learning-proposal-" + "0" * 32)
    assert not (tmp_path / "owner").exists()


# LLM: 两个独立解释器用真实owner锁竞争；一方已持quota/state再取proposals，另一方执行产品写入口。
# 函数用途: 确定性重现反向锁序的跨进程死锁，并有界清理所有测试进程。
@pytest.mark.parametrize("operation", ["propose", "confirm", "reject"])
def test_proposal_writers_do_not_deadlock_other_owner_transactions(tmp_path, operation):
    ctx = _runtime(tmp_path)
    quota = ctx.home.owner_home_dir / "quota.json"
    quota.write_text(json.dumps({**json.loads(quota.read_text()), "max_disk_mb": 16}))
    store = ctx.store
    row = _draft(store)
    script = '''
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from agent_py_agent.agent.capability import skill_learning_proposals as proposals
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.user_space import owner_quota
from agent_py_agent.tests.test_skill_learning import _runtime
from agent_py_agent.tests.test_skill_learning_confirmation import _draft
root, role, operation, identifier = json.loads(sys.argv[1])
root = Path(root)
store = _runtime(root).store
ready, waiting = root / 'holder-ready', root / 'writer-waiting'
if role == 'holder':
    with store.state_lock():
        ready.touch()
        deadline = time.monotonic() + 10
        while not waiting.exists():
            assert time.monotonic() < deadline, 'writer never reached lock entrance'
            time.sleep(0.01)
        with locked_json_path(store.directory / '.proposals'):
            pass
else:
    # LLM: quota等待前发信号；旧实现则先实际持有proposals后发信号，保证反转不是概率窗口。
    # 函数用途: 同步真实锁竞争，不替换或跳过任何底层锁。
    @contextmanager
    def observed(path, **kwargs):
        if Path(path).name == '.owner-quota':
            waiting.touch()
        with locked_json_path(path, **kwargs):
            if Path(path).name == '.proposals':
                waiting.touch()
            yield
    proposals.locked_json_path = owner_quota.locked_json_path = observed
    if operation == 'propose':
        _draft(store, 'cross-process-new', 'new')
    else:
        proposals.resolve_learning_proposal(store, identifier, 1, decision=operation)
'''
    args = [str(tmp_path), "holder", operation, row["proposal_id"]]
    holder = subprocess.Popen([sys.executable, "-c", script, json.dumps(args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    writer = None
    try:
        import time
        deadline = time.monotonic() + 10
        while not (tmp_path / "holder-ready").exists():
            assert holder.poll() is None, holder.communicate()
            assert time.monotonic() < deadline, "holder did not acquire real quota/state"
            time.sleep(0.01)
        args[1] = "writer"
        writer = subprocess.Popen([sys.executable, "-c", script, json.dumps(args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = holder.communicate(timeout=8)
        assert holder.returncode == 0, out + err
        out, err = writer.communicate(timeout=8)
        assert writer.returncode == 0, out + err
    finally:
        for process in (holder, writer):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    if operation == "propose":
        assert len(list_learning_proposals(store)) == 2
    else:
        assert learning_proposal(store, row["proposal_id"])["status"] == ("committed" if operation == "confirm" else "rejected")


# LLM: 发布已生效但确认事件尚未落盘的原恢复点不能丢失；失败后必须释放原ContextVar及所有真实锁。
# 函数用途: 验证publisher或账本中断后可按operation_id重试且不重复发布，仍保留原owner配额准入。
@pytest.mark.parametrize("failure_point", ["publisher", "ledger"])
def test_confirmation_retry_reuses_publication_after_interrupted_receipt(tmp_path, monkeypatch, failure_point):
    from agent_py_agent.agent.capability import skill_learning_proposals as proposals
    ctx = _runtime(tmp_path)
    quota = ctx.home.owner_home_dir / "quota.json"
    quota.write_text(json.dumps({**json.loads(quota.read_text()), "max_disk_mb": 16}))
    row = _draft(ctx.store)
    original_publish, original_append = proposals.publish_learned_skill, proposals.append_private_jsonl_capped
    publications = []

    # LLM: 仍执行原publisher所有guard/CAS和真实写入，只在其已返回后制造一次可恢复中断。
    # 函数用途: 记录发布次数并覆盖发布成功到确认落盘之间的失败点。
    def interrupted_publish(*args, **kwargs):
        event = original_publish(*args, **kwargs)
        publications.append(event)
        if failure_point == "publisher":
            raise OSError("interrupted before confirmation receipt")
        return event

    # LLM: 不伪造成功账本，第一次追加前明确报错；后续重试必须由产品再次完整写入。
    # 函数用途: 模拟账本暂时失败而不改变已发布登记事实。
    def interrupted_append(*args, **kwargs):
        raise OSError("interrupted before confirmation receipt")

    monkeypatch.setattr(proposals, "publish_learned_skill", interrupted_publish)
    if failure_point == "ledger":
        monkeypatch.setattr(proposals, "append_private_jsonl_capped", interrupted_append)
    with pytest.raises(OSError, match="interrupted"):
        resolve_learning_proposal(ctx.store, row["proposal_id"], 1, decision="confirm")
    assert ctx.store._quota_admission.get() is None
    assert learning_proposal(ctx.store, row["proposal_id"])["status"] == "pending"
    published = ctx.store.skill_path(NAME).read_bytes()
    assert ctx.store.load_registry().skills[NAME].version == 1 and not ctx.store.events(limit=0)
    monkeypatch.setattr(proposals, "append_private_jsonl_capped", original_append)
    result = resolve_learning_proposal(ctx.store, row["proposal_id"], 1, decision="confirm", actor_id="retry-user")
    assert result["status"] == "committed" and result["revision"] == 2
    assert result["resolved_by"]["actor_id"] == "retry-user"
    assert len(publications) == 1 and len(ctx.store.events(NAME, limit=0)) == 1
    assert ctx.store.skill_path(NAME).read_bytes() == published
    assert resolve_learning_proposal(ctx.store, row["proposal_id"], 1, decision="confirm") == result
    assert len(ctx.store.events(NAME, limit=0)) == 1 and ctx.store._quota_admission.get() is None

# LLM: 仅用临时owner、真实导航器和确定性故障边界测试；不启动进程、改权限或消费通知。
# 模块用途: 守住目录四次尝试、同scope瞬态宽限及权限/归属硬错误的失败关闭。
from __future__ import annotations

import errno
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_process_view as bg
from agent_py_agent.cli.chat_parts import tui_threading
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.tests.test_wave9_tui_bg_watch_and_sticky_user_bar import watch_fixture

_TRANSIENT_FAILURES = [
    FileNotFoundError("race"), ProcessLookupError("race"),
    *(OSError(number, "race") for number in (
        errno.EINTR, errno.EAGAIN, errno.ENOENT, getattr(errno, "ESTALE", errno.ENOENT), errno.ESRCH,
    )),
]


# LLM: 异常子类仍属于OSError，但不依赖构造器自动转换成PermissionError来证明errno分支。
# 类用途: 精确注入只有errno能识别的目录/观察权限错误。
class BoundaryError(OSError):
    pass


# LLM: 只重置产品的易失投影缓存，测试间不遗留目录代次，不触碰磁盘权限。
# 函数用途: 隔离每个参数例的后台观察缓存。
@pytest.fixture(autouse=True)
def clear_cache():
    bg._clear_background_process_view_cache()
    yield
    bg._clear_background_process_view_cache()


# LLM: 注入完整单次扫描边界，保留外层真实重试、退避和错误封装；不让异常逃逸报错代替合同断言。
# 函数用途: 提供指定次数异常、之后稳定成功的目录扫描夹具。
def scan_fixture(tmp_path, monkeypatch, failure, failures):
    calls, delays = [], []
    stable = (bg._SessionSnapshot("bg-stable"),)

    # LLM: 每次调用代表一次完整尝试，不复用失败后的半份快照。
    # 函数用途: 按确定性次序产生瞬态或硬失败。
    def attempt(_root, _home, *, allow_initial_missing=True):
        assert allow_initial_missing is (not calls)
        calls.append(1)
        if len(calls) <= failures:
            raise failure
        return stable

    monkeypatch.setattr(bg, "_directory_snapshot_attempt", attempt)
    monkeypatch.setattr(bg.time, "sleep", delays.append)
    try:
        result = bg._directory_snapshots(tmp_path, tmp_path)
    except OSError:
        result = None
    return result, stable, calls, delays


# LLM: 无errno的两个瞬态子类及全部五个errno均需重试，最多在第四次得到稳定快照，不吞未知错误。
# 函数用途: 验证前三次任意次数的扫描边界竞争都能恢复。
@pytest.mark.parametrize("failure", _TRANSIENT_FAILURES)
@pytest.mark.parametrize("failures", [1, 2, 3])
def test_scan_transient_boundary_recovers(tmp_path, monkeypatch, failure, failures):
    result, stable, calls, delays = scan_fixture(tmp_path, monkeypatch, failure, failures)
    assert result == stable
    assert len(calls) == failures + 1
    assert delays == [0.005] * failures


# LLM: 预算耗尽必须统一返回换代错误而非抛原异常或伪造projection_ok成功。
# 函数用途: 验证持续瞬态竞争只尝试四次并清理旧目录投影。
@pytest.mark.parametrize("failure", _TRANSIENT_FAILURES)
def test_scan_transient_exhaustion_returns_directory_changed(tmp_path, monkeypatch, failure):
    key = (str(tmp_path.resolve()), str(tmp_path.resolve()))
    bg._DIRECTORY_CACHE[key] = bg._ViewCacheEntry((), 0.0)
    result, _stable, calls, delays = scan_fixture(tmp_path, monkeypatch, failure, 100)
    assert result is not None and len(result) == 1
    assert result[0].error_code == "DIRECTORY_CHANGED_DURING_READ"
    assert len(calls) == 4 and delays == [0.005] * 3
    assert key not in bg._DIRECTORY_CACHE


# LLM: 显式PermissionError及两个权限errno优先于瞬态分类，诊断保留类型/errno但不泄漏异常正文。
# 函数用途: 验证目录权限拒绝立即返回PERMISSION_DENIED且不等待、不留旧投影。
@pytest.mark.parametrize("failure", [
    PermissionError("RAW_ERROR_BODY_W9B2"), BoundaryError(errno.EACCES, "RAW_ERROR_BODY_W9B2"),
    BoundaryError(errno.EPERM, "RAW_ERROR_BODY_W9B2"),
])
def test_scan_permission_is_immediate_structured_failure(tmp_path, monkeypatch, failure):
    key = (str(tmp_path.resolve()), str(tmp_path.resolve()))
    bg._DIRECTORY_CACHE[key] = bg._ViewCacheEntry((), 0.0)
    result, _stable, calls, delays = scan_fixture(tmp_path, monkeypatch, failure, 100)
    assert result is not None and result[0].error_code == "PERMISSION_DENIED"
    assert result[0].error == type(failure).__name__
    assert result[0].error_errno == failure.errno
    assert "RAW_ERROR_BODY_W9B2" not in repr(result)
    assert calls == [1] and delays == [] and key not in bg._DIRECTORY_CACHE


# LLM: 真实单次扫描仍穿过stat与scandir边界；初始不存在的目录为空态，扫描中消失才是重试竞争。
# 函数用途: 证明实际目录枚举边界上的ENOENT会恢复，而从未创建的目录不会误告警。
def test_real_directory_enumeration_race_retries_and_absent_directory_stays_empty(tmp_path, monkeypatch):
    root = tmp_path / "records"
    root.mkdir()
    original = bg._directory_record_names
    calls = []

    # LLM: 只让首次枚举失败，后续仍用产品真实scandir，不改目录或伪造稳定快照。
    # 函数用途: 注入已开始扫描后的目录消失竞争。
    def names(path):
        calls.append(path)
        if len(calls) == 1:
            raise FileNotFoundError(errno.ENOENT, "race")
        return original(path)

    monkeypatch.setattr(bg, "_directory_record_names", names)
    monkeypatch.setattr(bg.time, "sleep", lambda _seconds: None)
    try:
        result = bg._directory_snapshots(root, tmp_path)
    except OSError:
        result = None
    assert result == () and calls == [root, root]
    assert bg._directory_snapshots(tmp_path / "never-created", tmp_path) == ()


# LLM: 首次stat见过目录后，后续尝试的起始缺失也属于竞争；不能借初次空态合同提前报成功。
# 函数用途: 覆盖读后消失、重试仍缺失，以及后来重新出现的真实stat边界时序。
@pytest.mark.parametrize("recover", [False, True])
def test_started_scan_missing_on_retry_is_not_initial_empty(tmp_path, monkeypatch, recover):
    calls, delays = [], []

    # LLM: 第一次存在，第二次读后消失；恢复例在第四次stat才重现，绝不以未核验的空快照替代。
    # 函数用途: 注入跨完整尝试的目录消失与恢复序列。
    def key(_path):
        calls.append(1)
        if len(calls) == 1 or (recover and len(calls) >= 4):
            return (1, 2, 100)
        raise FileNotFoundError(errno.ENOENT, "vanished after scan started")

    monkeypatch.setattr(bg, "_directory_stat_key", key)
    monkeypatch.setattr(bg, "_directory_record_names", lambda _path: [])
    monkeypatch.setattr(bg.time, "sleep", delays.append)
    result = bg._directory_snapshots(tmp_path, tmp_path)
    if recover:
        assert result == () and delays == [0.005] * 2
    else:
        assert len(result) == 1 and result[0].error_code == "DIRECTORY_CHANGED_DURING_READ"
        assert result[0].error == "FileNotFoundError" and delays == [0.005] * 3
    assert len(calls) == 5


# LLM: 明确code的锁竞争或不完整快照只允许同scope已验证帧静默两次；第三次告警，成功恢复后重新计数。
# 函数用途: 核对新增两种瞬态错误的完整连续失败与恢复周期。
@pytest.mark.parametrize("code", ["LOCK_TIMEOUT", "SNAPSHOT_PARSE_INCOMPLETE"])
@pytest.mark.parametrize("error", [None, "OSError"])
def test_verified_frame_tolerates_two_lock_or_parse_failures(tmp_path, monkeypatch, code, error):
    agent, nav, runtime, response = watch_fixture(tmp_path, monkeypatch)
    good = response["value"]
    tui_threading._sync_background_watch(agent, nav)
    old = nav._background_rows
    response["value"] = {"projection_ok": False, "errors": [{"error_code": code, "error": error}]}
    for count in (1, 2):
        tui_threading._sync_background_watch(agent, nav)
        assert runtime.notice() == "" and nav._background_rows is old
        assert nav._bg_watch_consecutive_transient_failures == count
    tui_threading._sync_background_watch(agent, nav)
    assert "仅保留同会话上次结果" in runtime.notice()
    assert nav._bg_watch_consecutive_transient_failures == 3
    response["value"] = good
    tui_threading._sync_background_watch(agent, nav)
    assert runtime.notice() == "" and nav._bg_watch_consecutive_transient_failures == 0


# LLM: 瞬态code不能借用另一scope或尚未验证的旧帧；实际scope解析失败仍失败关闭，不按code粉饰。
# 函数用途: 防止新增瞬态集合扩大旧帧的归属范围。
@pytest.mark.parametrize("code", ["LOCK_TIMEOUT", "SNAPSHOT_PARSE_INCOMPLETE"])
@pytest.mark.parametrize("switch", ["first", "thread", "owner", "scope_error"])
def test_lock_or_parse_failure_never_borrows_another_scope(tmp_path, monkeypatch, code, switch):
    agent, nav, runtime, response = watch_fixture(tmp_path, monkeypatch)
    if switch != "first":
        tui_threading._sync_background_watch(agent, nav)
    if switch == "thread":
        agent.thread_id = "thread-two"
    elif switch == "owner":
        agent.home_paths.owner_home_dir = tmp_path / "other"
    response["value"] = {"projection_ok": False, "errors": [{"error_code": code}]}
    tui_threading._sync_background_watch(agent, nav, scope_error=switch == "scope_error")
    assert "观察刷新失败" in runtime.notice()
    assert nav._background_rows == () and nav.snapshot().background_process_count == 0
    assert nav._background_watch_valid_scope is None
    assert nav._bg_watch_consecutive_transient_failures == 0


# LLM: 硬权限/归属错误在同scope也必须清帧并撤销资格，混合瞬态标签不能掩盖PermissionError。
# 函数用途: 验证硬错误立即清空行、计数与宽限资格，随后瞬态不能复活旧帧。
@pytest.mark.parametrize("failure", [
    {"projection_ok": False, "errors": [{"error_code": "PERMISSION_DENIED"}]},
    {"projection_ok": False, "errors": [{"error_code": "OWNER_MISMATCH"}]},
    {"projection_ok": False, "errors": [{"error": "PermissionError"}]},
    {"projection_ok": False, "error_code": "OWNER_MISMATCH", "errors": []},
    {"projection_ok": False, "errors": [{"error_code": "LOCK_TIMEOUT", "error": "PermissionError"}]},
    pytest.param({"projection_ok": False, "errors": [{"error_code": []}, {"error_code": "OWNER_MISMATCH"}]}, id="malformed_sibling"),
    pytest.param({"projection_ok": False, "error_code": {}, "errors": [{"error": "PermissionError"}]}, id="malformed_top"),
    PermissionError(errno.EACCES, "private"), BoundaryError(errno.EPERM, "private"),
])
def test_hard_security_error_discards_verified_frame(tmp_path, monkeypatch, failure):
    agent, nav, runtime, response = watch_fixture(tmp_path, monkeypatch)
    good = response["value"]
    tui_threading._sync_background_watch(agent, nav)
    response["value"] = FileNotFoundError(errno.ENOENT, "race")
    tui_threading._sync_background_watch(agent, nav)
    assert nav._bg_watch_consecutive_transient_failures == 1
    response["value"] = failure
    tui_threading._sync_background_watch(agent, nav)
    assert nav._background_rows == () and nav.snapshot().background_process_count == 0
    assert nav._background_watch_valid_scope is None
    assert nav._bg_watch_consecutive_transient_failures == 0
    assert "观察刷新失败" in runtime.notice() and "仅保留" not in runtime.notice()
    response["value"] = {"projection_ok": False, "errors": [{"error_code": "LOCK_TIMEOUT"}]}
    tui_threading._sync_background_watch(agent, nav)
    assert nav._background_rows == () and nav._background_watch_valid_scope is None
    assert nav._bg_watch_consecutive_transient_failures == 0 and runtime.notice()
    response["value"] = good
    tui_threading._sync_background_watch(agent, nav)
    assert nav.snapshot().background_process_count == 1 and runtime.notice() == ""


# LLM: 写真实兼容格式进程JSON，经原list/reader建立有效帧；终态待通知只用于展示，不启动进程或消费通知。
# 函数用途: 为owner错配回归准备真实磁盘记录、导航和一条已验证活动观察行。
def real_owner_watch(tmp_path):
    root = tmp_path / "workspace/runtime/processes/sessions"
    root.mkdir(parents=True)
    path = root / "bg-real-owner.json"
    payload = {"session_id": "bg-real-owner", "status": "exited", "command": "real reader fixture",
               "execution_scope": {"owner_home": str(tmp_path), "owner_id": "owner-one", "thread_id": "thread-one"},
               "completion_target": {"thread_id": "thread-one", "task_id": "task-one"}}
    path.write_text(json.dumps(payload))
    runtime = TuiRuntime("real-owner-reader")
    nav = TuiAgentNavigationState(runtime)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), thread_id="thread-one")
    assert tui_threading._sync_background_watch(agent, nav)
    assert len(nav._background_rows) == 1 and nav.snapshot().background_process_count == 1
    assert nav._background_watch_valid_scope == (str(tmp_path), "thread-one")
    return agent, nav, path, payload


# LLM: 仅破坏临时记录中的owner声明，始终保留当前thread；不手工构造OWNER_MISMATCH返回信封。
# 函数用途: 覆盖owner跨家目录、归属冲突与非法owner值。
def damage_real_owner(payload, home, damage):
    if damage.startswith("foreign_home"):
        payload["execution_scope"]["owner_home"] = str(home / "other-owner")
        if damage == "foreign_home_bad_schema":
            payload["schema"] = "invalid"
        elif damage == "foreign_home_bad_scope":
            payload["metadata"] = []
        elif damage == "foreign_home_bad_task":
            payload["task_id"] = []
    elif damage == "conflicting_home":
        payload["metadata"] = {"owner_home": str(home / "other-owner")}
    elif damage == "conflicting_id":
        payload["metadata"] = {"owner_id": "owner-other"}
    elif damage == "conflicting_id_bad_scope":
        payload["activation_scope"] = {"owner_id": "owner-other"}
        payload["metadata"] = []
    elif damage == "invalid_home":
        payload["execution_scope"]["owner_home"] = ["invalid"]
    elif damage == "invalid_id":
        payload["execution_scope"]["owner_id"] = 123
    elif damage == "empty_home":
        payload["execution_scope"]["owner_home"] = ""
    else:
        assert damage == "empty_activation_id"
        payload["activation_scope"] = {"owner_id": ""}


# LLM: 从真实同步结果核对硬拒绝的全部易失状态；不能仅检查告警字样或替换数据源输出。
# 函数用途: 确认行、计数、scope和有效帧资格均已撤销，并显示硬错误提示。
def assert_real_owner_denied(nav):
    assert nav._background_rows == () and nav.snapshot().background_process_count == 0
    assert nav._background_watch_scope is None and nav._background_watch_valid_scope is None
    assert nav._bg_watch_consecutive_transient_failures == 0
    notice = nav.active_runtime().notice()
    assert "观察刷新失败" in notice and "仅保留" not in notice


# LLM: 合法帧后只改磁盘owner，保留相同thread走真实list和TUI；当前线程错配不得隔离成unrelated。
# 函数用途: 验证真实列表reader的owner错误双字段及立即清帧。
@pytest.mark.parametrize("damage", ["foreign_home", "foreign_home_bad_schema", "foreign_home_bad_scope", "foreign_home_bad_task", "conflicting_home", "conflicting_id", "conflicting_id_bad_scope", "invalid_home", "invalid_id", "empty_home", "empty_activation_id"])
def test_real_owner_mismatch_list_revokes_prior_frame(tmp_path, damage):
    agent, nav, path, payload = real_owner_watch(tmp_path)
    damage_real_owner(payload, tmp_path, damage)
    path.write_text(json.dumps(payload))
    result = bg.list_background_processes(tmp_path, thread_id="thread-one")
    assert result["projection_ok"] is False and result["unrelated_errors"] == []
    tui_threading._sync_background_watch(agent, nav)
    assert_real_owner_denied(nav)
    assert result["errors"][0]["error_code"] == "OWNER_MISMATCH"
    assert result["errors"][0]["reason_code"] == "OWNER_MISMATCH"
    snapshot = bg._file_snapshot(path.parent, payload["session_id"], tmp_path)
    assert snapshot.error_code == "OWNER_MISMATCH"


# LLM: 真实list成功返回后才篡改磁盘；真实inspect/_read_session继续校验，包装仅观察异常并模拟reason-only兼容接口。
# 函数用途: 验证列表到详情之间的owner竞态及两种异常属性入口均立即撤销旧帧。
@pytest.mark.parametrize("reason_only", [False, True])
@pytest.mark.parametrize("damage", ["foreign_home", "foreign_home_bad_scope"])
def test_real_owner_mismatch_between_list_and_detail_revokes_frame(tmp_path, monkeypatch, reason_only, damage):
    agent, nav, path, payload = real_owner_watch(tmp_path)
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav.snapshot().background_watch_active
    listing, inspect = bg.list_background_processes, bg.inspect_background_process
    observed = []

    # LLM: 返回原list的真实成功结果，不改响应字典；仅在返回到TUI之前安排磁盘owner变化。
    # 函数用途: 固定列表成功、详情校验前记录变更的交错。
    def list_then_change(*args, **kwargs):
        result = listing(*args, **kwargs)
        assert result["projection_ok"] is True and len(result["items"]) == 1
        damage_real_owner(payload, tmp_path, damage)
        path.write_text(json.dumps(payload))
        return result

    # LLM: 始终调用真实详情reader并重抛原异常；reason-only只移除该真实异常的error_code属性值，不制造错误码。
    # 函数用途: 捕获真实owner拒绝，验证异常属性贯穿消费端。
    def inspect_and_observe(*args, **kwargs):
        try:
            return inspect(*args, **kwargs)
        except ValueError as exc:
            if reason_only:
                exc.error_code = None
            observed.append(exc)
            raise

    monkeypatch.setattr(bg, "list_background_processes", list_then_change)
    monkeypatch.setattr(bg, "inspect_background_process", inspect_and_observe)
    tui_threading._sync_background_watch(agent, nav)
    assert observed and isinstance(observed[0], ValueError)
    assert_real_owner_denied(nav)
    assert getattr(observed[0], "reason_code", None) == "OWNER_MISMATCH"
    assert getattr(observed[0], "error_code", None) == (None if reason_only else "OWNER_MISMATCH")


# LLM: 普通数据错误不具备owner拒绝语义；真实列表/详情ValueError仍告警保留同scope旧帧，不一概升级权限拒绝。
# 函数用途: 守住owner异常子类修复的精确分类边界。
@pytest.mark.parametrize("detail", [False, True])
def test_real_ordinary_value_error_does_not_become_owner_refusal(tmp_path, monkeypatch, detail):
    agent, nav, path, payload = real_owner_watch(tmp_path)
    old_rows, old_scope = nav._background_rows, nav._background_watch_valid_scope
    if detail:
        assert nav.move_selection(1) and nav.enter_selected()
        listing = bg.list_background_processes

        # LLM: 列表仍读合法JSON，返回前仅将真实记录pid改成非法值，让详情原_summary抛普通ValueError。
        # 函数用途: 固定普通详情数据错误的真实交错。
        def list_then_bad_pid(*args, **kwargs):
            result = listing(*args, **kwargs)
            payload["pid"] = "invalid"
            path.write_text(json.dumps(payload))
            return result

        monkeypatch.setattr(bg, "list_background_processes", list_then_bad_pid)
    else:
        payload["pid"] = "invalid"
        path.write_text(json.dumps(payload))
        result = bg.list_background_processes(tmp_path, thread_id="thread-one")
        assert result["errors"][0]["error"] == "ValueError"
        assert not result["errors"][0].get("error_code") and not result["errors"][0].get("reason_code")
    tui_threading._sync_background_watch(agent, nav)
    assert nav._background_rows is old_rows and nav._background_watch_valid_scope == old_scope
    assert nav.snapshot().background_process_count == 1
    assert "仅保留同会话上次结果" in nav.active_runtime().notice()


# LLM: 仍由真实list读取磁盘拒绝；仅去掉其真实诊断error_code以验证reason_code兼容，不手造错误信封。
# 函数用途: 确认列表消费端只收到reason_code时同样立即清帧。
def test_real_owner_list_reason_only_refusal_clears_frame(tmp_path, monkeypatch):
    agent, nav, path, payload = real_owner_watch(tmp_path)
    damage_real_owner(payload, tmp_path, "foreign_home")
    path.write_text(json.dumps(payload))
    listing = bg.list_background_processes

    # LLM: 保留真实错误及其reason_code，不替换items/projection_ok或改造owner拒绝的事实。
    # 函数用途: 模拟只输出兼容reason字段的真实reader适配器。
    def reason_only_list(*args, **kwargs):
        result = listing(*args, **kwargs)
        assert not result["projection_ok"] and result["errors"]
        for error in result["errors"]:
            assert error["reason_code"] == "OWNER_MISMATCH"
            error.pop("error_code")
        return result

    monkeypatch.setattr(bg, "list_background_processes", reason_only_list)
    tui_threading._sync_background_watch(agent, nav)
    assert_real_owner_denied(nav)


# LLM: 对动态概要出口做边界单测；异常仍来自真实JSON/_read_session校验，而非手工赋码或伪造错误字典。
# 函数用途: 守住list动态概要阶段捕获owner拒绝时的双字段和当前线程归属。
def test_summary_exception_keeps_real_owner_rejection_codes(tmp_path, monkeypatch):
    _agent, _nav, path, payload = real_owner_watch(tmp_path)
    summary = bg._summary

    # LLM: 在概要边界安排owner变更并调用原reader重新校验，专门覆盖list的第二个异常捕获出口。
    # 函数用途: 把真实owner校验异常送到动态概要投影分支。
    def summary_revalidates_owner(root, valid_payload):
        damage_real_owner(payload, tmp_path, "foreign_home")
        path.write_text(json.dumps(payload))
        return summary(root, bg._read_session(root, valid_payload["session_id"], tmp_path))

    monkeypatch.setattr(bg, "_summary", summary_revalidates_owner)
    result = bg.list_background_processes(tmp_path, thread_id="thread-one")
    assert not result["projection_ok"] and result["unrelated_errors"] == []
    assert result["errors"][0]["error_code"] == "OWNER_MISMATCH"
    assert result["errors"][0]["reason_code"] == "OWNER_MISMATCH"

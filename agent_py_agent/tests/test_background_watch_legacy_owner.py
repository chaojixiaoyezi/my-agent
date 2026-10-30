# LLM: 使用临时规范owner布局检验v1只读兼容，不修改生产记录或授予执行权限。
# 模块用途: 覆盖历史空home、精确线程过滤和新启动准入的正反例。
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_process_view as bg
from agent_py_agent.agent.tooling.background_process_launch import BackgroundLaunchRequest
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope


# LLM: 仅创建临时普通单链接v1文件，回传原字节与stat供零写入断言。
# 函数用途: 准备历史进程读取样本，不启动或探测真实PID。
def _legacy(home, *, value="", owner="local/main", thread="history", omit=False):
    root = home / ".background_jobs/process_sessions"
    root.mkdir(parents=True, exist_ok=True)
    access = {"owner_id": owner, "conversation_id": thread}
    if not omit:
        access["owner_home"] = value
    payload = {"schema": "managed_process_session.v1", "session_id": "bg-legacy",
               "pid": 123, "pid_birth_token": "historical-birth", "started_at": 100,
               "finished_at": 200, "status": "exited", "command": "historical command",
               "access_scope": access}
    path = root / "bg-legacy.json"
    path.write_text(json.dumps(payload))
    os.utime(path, (100, 100))
    return path, payload


# LLM: 冷热两次必须保持原字节及元数据；跨线程过滤先于任何PID/日志访问。
# 函数用途: 验证三种历史缺省home合法且不连坐健康线程。
@pytest.mark.parametrize("value,omit", [("", False), (None, False), (None, True)])
def test_r1_r2_legacy_empty_home_readonly_and_thread_filter(tmp_path, monkeypatch, value, omit):
    home = tmp_path / "owners/local/main"
    path, _ = _legacy(home, value=value, omit=omit)
    before = (path.read_bytes(), path.stat())
    monkeypatch.setattr(bg, "_managed_instance_still_live", lambda *a: pytest.fail("terminal or foreign PID probe"))
    monkeypatch.setattr(bg, "_log_tail", lambda *a: pytest.fail("foreign log read"))
    bg._clear_background_process_view_cache()
    for _ in range(2):
        other = bg.list_background_processes(home, thread_id="healthy")
        assert other["projection_ok"] is True and other["errors"] == []
        assert other["items"] == []
        own = bg.list_background_processes(home, thread_id="history")
        assert own["projection_ok"] is True
        assert [row["session_id"] for row in own["items"]] == ["bg-legacy"]
    assert (path.read_bytes(), path.stat()) == before
    with pytest.raises(KeyError):
        bg.inspect_background_process(home, "bg-legacy", thread_id="healthy")


# LLM: 非字面缺省或外owner不能获v1豁免；保留原硬拒绝分类。
# 函数用途: 验证空白、坏类型和错home拒绝。
@pytest.mark.parametrize("value", [" ", 0, False, [], {}, "/different-owner"])
def test_r4_legacy_invalid_home_remains_hard_rejection(tmp_path, value):
    home = tmp_path / "owners/local/main"
    _legacy(home, value=value)
    result = bg.list_background_processes(home, thread_id="history")
    assert not result["projection_ok"] and result["items"] == []
    assert result["errors"][0]["error_code"] == "OWNER_MISMATCH"


# LLM: ID不能来自v1转换或默认管理员猜测；原始声明必须与回验布局相同。
# 函数用途: 验证坏owner或真实跨owner记录无法兼容。
@pytest.mark.parametrize("owner", ["local/other", "", " ", None, 123, [], {}])
def test_r4_legacy_owner_must_match_trusted_layout(tmp_path, owner):
    home = tmp_path / "owners/local/main"
    _legacy(home, owner=owner)
    result = bg.list_background_processes(home, thread_id="history")
    assert not result["projection_ok"] and not result["items"]


# LLM: basename近似不是身份；无规范布局的旧入口保持保守拒绝。
# 函数用途: 确认非规范home不能借历史缺省认领管理员。
def test_r3_noncanonical_home_has_no_empty_home_compatibility(tmp_path):
    home = tmp_path / "arbitrary-main"
    _legacy(home)
    assert not bg.list_background_processes(home, thread_id="history")["projection_ok"]


# LLM: 纯构造准入先于任何Store/文件/进程动作，不以cwd补全缺失身份。
# 函数用途: 拒绝新启动的缺失或互斥home。
@pytest.mark.parametrize("access_home,execution_home", [("", ""), ("", "/owner"), ("/owner", ""), ("/owner", "/other")])
def test_r10_new_launch_rejects_missing_or_conflicting_home_before_side_effects(tmp_path, access_home, execution_home):
    with pytest.raises(ValueError, match="owner"):
        BackgroundLaunchRequest(argv=["never-spawn"], command="never-spawn", cwd=tmp_path,
            log_path=tmp_path / "never.log", env={}, max_log_bytes=100,
            store_root=tmp_path / "sessions",
            access_scope=ProcessAccessScope("local/main", "thread", access_home),
            execution_scope=ProcessExecutionScope(owner_home=execution_home))
    assert list(tmp_path.iterdir()) == []


# LLM: 显式上下文每次在冷热缓存前核验；拒绝不能污染下一次正确身份读取。
# 函数用途: 验证原入口与可信入口一致、冲突拒绝且详情不借旧缓存。
def test_r3_r8_context_revalidation_and_detail(tmp_path):
    home = tmp_path / "owners/local/main"
    path, payload = _legacy(home)
    log = home / "history.log"
    log.write_text("one\ntwo\n")
    payload["output_file"] = str(log)
    path.write_text(json.dumps(payload))
    for _ in range(2):
        old = bg.list_background_processes(home, thread_id="history")
        explicit = bg.list_background_processes(home, thread_id="history", expected_owner_id="local/main")
        assert old == explicit and explicit["projection_ok"]
        wrong = bg.list_background_processes(home, thread_id="history", expected_owner_id="local/other")
        assert not wrong["projection_ok"] and not wrong["items"]
        with pytest.raises(ValueError, match="owner"):
            bg.inspect_background_process(home, "bg-legacy", thread_id="history", expected_owner_id="local/other")
    assert bg.inspect_background_process(home, "bg-legacy", thread_id="history", tail_lines=1)["log_tail"] == ["two"]
    payload["access_scope"]["owner_id"] = "local/other"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="owner"):
        bg.inspect_background_process(home, "bg-legacy", thread_id="history")


# LLM: 使用原resolver的规范local/provider user/group布局；后代目录无资格，缺失目录不得创建。
# 函数用途: 覆盖严格路径回验而非只特判local/main。
@pytest.mark.parametrize("parts,owner", [("local/user-a", "local/user-a"),
    ("local/owners", "local/owners"),
    ("providers/feishu/users/owners", "providers/feishu/users/owners"),
    ("providers/feishu/groups/owners", "providers/feishu/groups/owners"),
    ("providers/owners/users/owners", "providers/owners/users/owners"),
    ("providers/feishu/users/user-a", "providers/feishu/users/user-a"),
    ("providers/feishu/groups/group-a", "providers/feishu/groups/group-a")])
def test_r3_owner_layout_roundtrip(tmp_path, parts, owner):
    home = tmp_path / "owners" / parts
    _legacy(home, owner=owner)
    assert bg.list_background_processes(home, thread_id="history", expected_owner_id=owner)["projection_ok"]
    descendant = home / "nested"
    _legacy(descendant, owner=owner)
    assert not bg.list_background_processes(descendant, thread_id="history")["projection_ok"]


# LLM: 原生v1全条校验和非access身份字段不得被缺省home旁路；错误不能靠其他线程洗白。
# 函数用途: 守住坏schema、坏PID、metadata冲突及额外空执行home反例。
@pytest.mark.parametrize("changes", [{"pid": 0}, {"schema": "invalid"},
    {"metadata": {"owner_id": "local/other"}}, {"execution_scope": {"owner_home": ""}}])
def test_r4_r7_full_native_validation_still_required(tmp_path, changes):
    home = tmp_path / "owners/local/main"
    path, payload = _legacy(home)
    payload.update(changes)
    path.write_text(json.dumps(payload))
    for _ in range(2):
        assert not bg.list_background_processes(home, thread_id="healthy")["projection_ok"]


# LLM: 直接调用真实Shell早期准入；后续argv、日志和spawn一旦触达立即失败，不使用假的成功进程。
# 函数用途: 验证拒绝输出not_started且零文件副作用，共享MCP的空业务字段仍可构造。
def test_r10_shell_rejects_before_log_or_spawn(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import shell

    monkeypatch.setattr(shell, "_background_command_argv", lambda *a, **k: pytest.fail("argv before scope admission"))
    tool = SimpleNamespace(model_spec=SimpleNamespace(name="run_command"))
    outcome = shell.ShellTool._start_background_command(tool, "never", tmp_path, (None, None, None),
        ProcessAccessScope("local/main", "thread", ""), execution_scope=ProcessExecutionScope())
    assert not outcome.ok and outcome.effect_outcome == "not_started" and outcome.error_code == "COMMAND_FAILED"
    assert list(tmp_path.iterdir()) == []
    request = BackgroundLaunchRequest(argv=["never"], command="never", cwd=tmp_path, log_path=tmp_path / "out",
        env={}, max_log_bytes=100, store_root=tmp_path / "sessions",
        access_scope=ProcessAccessScope("local/main", "", str(tmp_path)),
        execution_scope=ProcessExecutionScope(owner_home=str(tmp_path)))
    assert request.execution_scope.thread_id == ""

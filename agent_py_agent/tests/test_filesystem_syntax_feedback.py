"""真实临时文件工具验证：语法观察不能改变发布事实、原安全门和分块写入。"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.tooling import _filesystem_write as write_module
from agent_py_agent.agent.tooling import file_syntax_diagnostics as diagnostics
from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions


# LLM: 测试只在 tmp_path 创建原生文件工具，不绕过生产发布入口来伪造观察。
# 函数用途: 装配显式开关及相同访问配置的三个工具。
def file_tools(root, *, enabled=True):
    access = FileSystemAccessOptions(enable_file_syntax_diagnostics=enabled)
    return (
        WriteFileTool(root, options=WriteFileToolOptions(access_options=access)),
        EditFileTool(root, access_options=access),
        ApplyPatchTool(root, access_options=access),
    )


# LLM: 结构回执才是观察来源，不能从自然输出反解析控制状态。
# 函数用途: 返回本次已提交候选的观察列表。
def observations(result):
    return result.result_envelope["syntax_diagnostics"]["observations"]


# LLM: 仍调用真实文件工具入口；仅测试夹具负责准备旧文件，诊断失败不能改变该入口的提交事实。
# 函数用途: 用统一 JSON 修改覆盖 write、edit 以及 patch 的新增和更新路径。
def commit_json_change(root, operation):
    write, edit, patch = file_tools(root)
    if operation != "patch_add":
        (root / "value.json").write_bytes(b"{}\n")
    if operation == "write":
        return write.execute({"path": "value.json", "content": "{\n"})
    if operation == "edit":
        return edit.execute({"path": "value.json", "old_string": "{}", "new_string": "{"})
    command = "*** Add File: value.json\n+{\n" if operation == "patch_add" else "*** Update File: value.json\n-{}\n+{\n"
    return patch.execute({"patch": f"*** Begin Patch\n{command}*** End Patch"})


@pytest.mark.parametrize("operation", ["write", "edit", "patch_add", "patch_update"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_record_failure_preserves_committed_tool_success(tmp_path, monkeypatch, operation, error_type):
    def fail(*_args):
        raise error_type("private diagnostic failure")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "record", fail)
    result = commit_json_change(tmp_path, operation)
    assert (tmp_path / "value.json").read_bytes() == b"{\n"
    assert result.ok and not result.error_code and not result.effect_outcome
    feedback = result.result_envelope["syntax_diagnostics"]
    assert feedback["status"] == "not_checked" and not feedback["observations"]
    assert "private diagnostic failure" not in result.output


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_discard_failure_preserves_deletion_and_patch_continues(tmp_path, monkeypatch, operation):
    def fail(*_args):
        raise ValueError("private diagnostic failure")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "discard", fail)
    _, _, patch = file_tools(tmp_path)
    removal = ("*** Delete File: old.json\n" if operation == "delete" else
               "*** Update File: old.json\n*** Move to: moved.json\n-{}\n+true\n")
    result = patch.execute({"patch": "*** Begin Patch\n*** Add File: old.json\n+{}\n" + removal
                                    + "*** Add File: tail.json\n+null\n*** End Patch"})
    assert result.ok and not result.error_code and not result.effect_outcome
    assert not (tmp_path / "old.json").exists()
    assert (tmp_path / "tail.json").read_bytes() == b"null\n"
    if operation == "move":
        assert (tmp_path / "moved.json").read_bytes() == b"true\n"
    assert result.result_envelope["syntax_diagnostics"]["status"] == "not_checked"
    assert not observations(result)


def test_record_failure_after_mutation_does_not_leave_stale_success(tmp_path, monkeypatch):
    original = diagnostics.FileSyntaxDiagnostics.record

    def fail_after_update(budget, observation):
        original(budget, observation)
        if observation.status == "invalid":
            raise ValueError("failed after mutation")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "record", fail_after_update)
    _, _, patch = file_tools(tmp_path)
    result = patch.execute({"patch": "*** Begin Patch\n*** Add File: value.json\n+{}\n"
                                    "*** Update File: value.json\n-{}\n+{\n*** End Patch"})
    assert result.ok and (tmp_path / "value.json").read_bytes() == b"{\n"
    assert result.result_envelope["syntax_diagnostics"]["status"] == "not_checked"
    assert not observations(result)


def test_diagnostic_collection_failure_is_local_to_one_call(tmp_path, monkeypatch):
    original = diagnostics.FileSyntaxDiagnostics.record
    calls = []

    def fail_once(budget, observation):
        calls.append(observation)
        if len(calls) == 1:
            raise ValueError("first receipt unavailable")
        original(budget, observation)

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "record", fail_once)
    write, _, _ = file_tools(tmp_path)
    first = write.execute({"path": "value.json", "content": "{"})
    second = write.execute({"path": "value.json", "content": "{}"})
    assert first.ok and first.result_envelope["syntax_diagnostics"]["status"] == "not_checked"
    assert second.ok and observations(second)[0]["status"] == "valid"


def test_record_failure_does_not_hide_later_real_partial_commit(tmp_path, monkeypatch):
    original = write_module.os.link

    def fail_record(*_args):
        raise ValueError("private diagnostic failure")

    def fail_second_publish(source, target):
        if Path(target).name == "second.json":
            raise OSError("real publish failure")
        return original(source, target)

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "record", fail_record)
    monkeypatch.setattr(write_module.os, "link", fail_second_publish)
    _, _, patch = file_tools(tmp_path)
    result = patch.execute({"patch": "*** Begin Patch\n*** Add File: first.json\n+{\n"
                                    "*** Add File: second.json\n+{}\n*** End Patch"})
    assert not result.ok and result.result_envelope["partial_commit"]
    assert result.result_envelope["failed_path"] == "second.json"
    assert (tmp_path / "first.json").read_bytes() == b"{\n" and not (tmp_path / "second.json").exists()
    assert "real publish failure" in result.output and "private diagnostic failure" not in result.output
    assert result.result_envelope["syntax_diagnostics"]["status"] == "not_checked"


@pytest.mark.parametrize("operation", ["write", "edit", "patch_add", "patch_update"])
def test_record_control_cancellation_propagates_after_real_commit(tmp_path, monkeypatch, operation):
    def cancel(*_args):
        raise ToolCancelled("cancel diagnostic receipt")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "record", cancel)
    with pytest.raises(ToolCancelled):
        commit_json_change(tmp_path, operation)
    assert (tmp_path / "value.json").read_bytes() == b"{\n"


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_discard_control_cancellation_propagates_after_real_delete(tmp_path, monkeypatch, operation):
    def cancel(*_args):
        raise ToolCancelled("cancel diagnostic receipt")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "discard", cancel)
    _, _, patch = file_tools(tmp_path)
    removal = ("*** Delete File: old.json\n" if operation == "delete" else
               "*** Update File: old.json\n*** Move to: moved.json\n-{}\n+true\n")
    with pytest.raises(ToolCancelled):
        patch.execute({"patch": "*** Begin Patch\n*** Add File: old.json\n+{}\n" + removal + "*** End Patch"})
    assert not (tmp_path / "old.json").exists()


def test_write_bad_json_succeeds_and_model_sees_position(tmp_path):
    write, _, _ = file_tools(tmp_path)
    result = write.execute({"path": "broken.json", "content": '{"value": ]}'})
    assert result.ok and not result.error_code and not result.effect_outcome
    assert (tmp_path / "broken.json").read_text() == '{"value": ]}'
    row = observations(result)[0]
    assert row["status"] == "invalid" and row["column"] == 11
    assert "JSON_INVALID" in result.render_for_prompt()


def test_append_checks_complete_candidate_and_keeps_incremental_writes(tmp_path):
    write, _, _ = file_tools(tmp_path)
    first = write.execute({"path": "value.json", "content": "{"})
    second = write.execute({"path": "value.json", "content": "}", "mode": "append"})
    assert first.ok and second.ok
    assert observations(first)[0]["status"] == "invalid"
    assert observations(second)[0]["status"] == "valid"
    assert (tmp_path / "value.json").read_bytes() == b"{}"


def test_edit_reports_actual_final_bytes_without_blocking(tmp_path):
    (tmp_path / "value.json").write_bytes(b'{"value": 1}\r\n')
    _, edit, _ = file_tools(tmp_path)
    result = edit.execute({"path": "value.json", "old_string": "1", "new_string": "]"})
    assert result.ok and observations(result)[0]["status"] == "invalid"
    assert (tmp_path / "value.json").read_bytes() == b'{"value": ]}\r\n'


def test_patch_repeated_path_move_and_delete_report_only_current_commits(tmp_path):
    _, _, patch = file_tools(tmp_path)
    result = patch.execute({"patch": "*** Begin Patch\n"
                            "*** Add File: old.json\n+{\n"
                            "*** Update File: old.json\n-{\n+{}\n"
                            "*** Update File: old.json\n*** Move to: moved.json\n-{}\n+true\n"
                            "*** Add File: deleted.json\n+{\n"
                            "*** Delete File: deleted.json\n*** End Patch"})
    assert result.ok, result.output
    rows = observations(result)
    assert [(row["path"], row["status"]) for row in rows] == [(str(tmp_path / "moved.json"), "valid")]
    assert not (tmp_path / "old.json").exists() and not (tmp_path / "deleted.json").exists()


def test_partial_patch_keeps_only_successful_candidate_observation(tmp_path, monkeypatch):
    _, _, patch = file_tools(tmp_path)
    original = write_module.os.link

    def fail_second(source, target):
        if Path(target).name == "second.json":
            raise OSError("publish refused")
        return original(source, target)

    monkeypatch.setattr(write_module.os, "link", fail_second)
    result = patch.execute({"patch": "*** Begin Patch\n*** Add File: first.json\n+{\n"
                                     "*** Add File: second.json\n+{}\n*** End Patch"})
    assert not result.ok and result.result_envelope["partial_commit"]
    assert [(row["path"], row["status"]) for row in observations(result)] == [(str(tmp_path / "first.json"), "invalid")]
    assert not (tmp_path / "second.json").exists()


def test_partial_patch_failed_rewrite_keeps_last_committed_observation(tmp_path, monkeypatch):
    _, _, patch = file_tools(tmp_path)

    def fail_publish(*_args):
        raise OSError("publish refused")

    monkeypatch.setattr(write_module.os, "replace", fail_publish)
    result = patch.execute({"patch": "*** Begin Patch\n*** Add File: value.json\n+{\n"
                                     "*** Update File: value.json\n-{\n+{}\n*** End Patch"})
    assert not result.ok and result.result_envelope["partial_commit"]
    assert observations(result)[0]["status"] == "invalid"
    assert (tmp_path / "value.json").read_bytes() == b"{\n"
    assert not list(tmp_path.glob(".value.json.*"))


def test_move_source_delete_failure_retains_committed_destination(tmp_path, monkeypatch):
    source = tmp_path / "old.json"
    source.write_text("{}\n")
    original = Path.unlink

    def fail_source(path, *args, **kwargs):
        if path == source:
            raise OSError("source delete denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source)
    _, _, patch = file_tools(tmp_path)
    result = patch.execute({"patch": "*** Begin Patch\n*** Update File: old.json\n"
                                    "*** Move to: moved.json\n-{}\n+{\n*** End Patch"})
    assert not result.ok and result.result_envelope["partial_commit"]
    assert observations(result)[0]["path"] == str(tmp_path / "moved.json")
    assert source.exists() and (tmp_path / "moved.json").exists()


def test_disabled_three_tools_never_observe_and_keep_old_receipts(tmp_path, monkeypatch):
    def forbidden(*_args):
        raise AssertionError("关闭时不能解析")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "observe_candidate", forbidden)
    write, edit, patch = file_tools(tmp_path, enabled=False)
    results = [write.execute({"path": "value.json", "content": "{"}),
               edit.execute({"path": "value.json", "old_string": "{", "new_string": "["}),
               patch.execute({"patch": "*** Begin Patch\n*** Update File: value.json\n-[\n+{\n*** End Patch"})]
    assert all(result.ok and "syntax_diagnostics" not in result.result_envelope for result in results)
    assert results[0].output == "已写入文件: value.json"
    assert results[2].output == "已应用补丁: value.json"
    assert all("语法观察" not in result.output for result in results)


def test_candidate_observation_does_not_reopen_published_target(tmp_path, monkeypatch):
    write, _, _ = file_tools(tmp_path)
    replace = write_module.os.replace

    def replace_then_external_change(src, dst):
        replace(src, dst)
        Path(dst).write_bytes(b"{}")

    monkeypatch.setattr(write_module.os, "replace", replace_then_external_change)
    result = write.execute({"path": "value.json", "content": "{"})
    assert result.ok and (tmp_path / "value.json").read_bytes() == b"{}"
    assert observations(result)[0]["status"] == "invalid"


def test_candidate_observation_uses_the_same_open_temporary_descriptor(tmp_path, monkeypatch):
    descriptors = []
    mkstemp = write_module.tempfile.mkstemp
    observe = diagnostics.FileSyntaxDiagnostics.observe_candidate

    def track_temp(*args, **kwargs):
        descriptor, name = mkstemp(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor, name

    def check_descriptor(budget, target, source):
        assert source.fileno() == descriptors[-1]
        assert not (tmp_path / "value.json").exists()
        return observe(budget, target, source)

    monkeypatch.setattr(write_module.tempfile, "mkstemp", track_temp)
    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "observe_candidate", check_descriptor)
    write, _, _ = file_tools(tmp_path)
    result = write.execute({"path": "value.json", "content": "null"})
    assert len(descriptors) == 1 and result.ok
    assert observations(result)[0]["status"] == "valid"


def test_failed_publish_never_reports_candidate_as_committed(tmp_path, monkeypatch):
    (tmp_path / "value.json").write_bytes(b"{}")
    write, _, _ = file_tools(tmp_path)

    def fail(*_args):
        raise OSError("publish failed")

    monkeypatch.setattr(write_module.os, "replace", fail)
    result = write.execute({"path": "value.json", "content": "{"})
    assert not result.ok and "syntax_diagnostics" not in result.result_envelope
    assert (tmp_path / "value.json").read_bytes() == b"{}"


def test_diagnostic_error_cannot_turn_successful_write_into_unknown(tmp_path, monkeypatch):
    write, _, _ = file_tools(tmp_path)

    def fail(*_args):
        raise OSError("diagnostic failed")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "observe_candidate", fail)
    result = write.execute({"path": "value.json", "content": "{"})
    assert result.ok and not result.effect_outcome
    assert observations(result)[0]["status"] == "not_checked"


def test_enabled_diagnostics_preserve_binary_format_gate(tmp_path):
    (tmp_path / "bundle.zip").write_bytes(b"previous")
    write, _, _ = file_tools(tmp_path)
    result = write.execute({"path": "bundle.zip", "data_base64": base64.b64encode(b"bad zip").decode()})
    assert not result.ok and result.error_code == "ARTIFACT_VALIDATION_FAILED"
    assert (tmp_path / "bundle.zip").read_bytes() == b"previous"


@pytest.mark.parametrize("name", ["file.jsonc", "file.jsonl", "file.future"])
def test_unknown_formats_keep_original_write_result(tmp_path, name):
    write, _, _ = file_tools(tmp_path)
    result = write.execute({"path": name, "content": "{unfinished"})
    assert result.ok and "syntax_diagnostics" not in result.result_envelope

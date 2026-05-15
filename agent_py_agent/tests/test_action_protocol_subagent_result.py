from agent_py_agent.agent.action_protocol import RunScope, SubagentResultEnvelope
from agent_py_agent.agent.subagents.parsing import (
    parse_subagent_result_envelope,
    parse_subagent_runner_output,
)
from agent_py_agent.agent.subagents.parsing_envelope import SubagentResultEnvelopeParseRequest


def test_subagent_result_text_converts_to_typed_envelope():
    text = (
        "[SUBAGENT_RESULT]\n"
        "{"
        '"status":"AWAITING_ACCEPTANCE",'
        '"summary":"display only",'
        '"used_tools":["fake_from_model"],'
        '"artifacts":[{"path":"out.txt","kind":"file","summary":"output"}],'
        '"evidence_packets":[{"id":"evpkt-1","claim":"output exists",'
        '"artifact_refs":["out.txt"],"evidence_refs":["report.json"],"confidence":0.8}],'
        '"tests":[{"name":"static","ok":true}],'
        '"next_actions":["parent verify"]'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )

    envelope = parse_subagent_result_envelope(
        SubagentResultEnvelopeParseRequest(
            text=text,
            result_id="result-1",
            run_id="run-1",
            scope=RunScope(task_id="task-1", run_id="run-1"),
            actual_tools=["read_file"],
        )
    )

    assert isinstance(envelope, SubagentResultEnvelope)
    assert envelope.kind == "subagent_result"
    assert envelope.result_id == "result-1"
    assert envelope.run_id == "run-1"
    assert envelope.status == "AWAITING_ACCEPTANCE"
    assert envelope.summary == "display only"
    assert envelope.actual_tools == ["read_file"]
    assert envelope.artifact_refs[0].path == "out.txt"
    assert envelope.evidence_refs[0].claim == "output exists"
    assert envelope.tests == [{"name": "static", "ok": True}]
    assert envelope.next_actions == ["parent verify"]


def test_subagent_result_envelope_does_not_infer_tools_from_summary():
    text = (
        "[SUBAGENT_RESULT]\n"
        '{"status":"AWAITING_ACCEPTANCE","summary":"I used read_file and write_file"}\n'
        "[/SUBAGENT_RESULT]"
    )

    envelope = parse_subagent_result_envelope(
        SubagentResultEnvelopeParseRequest(
            text=text,
            result_id="result-2",
            run_id="run-2",
        )
    )

    assert envelope is not None
    assert envelope.actual_tools == []


# LLM: Real runners sometimes say deliverables; parser must normalize that before parent acceptance.
# 函数用途: 复现真实 E2E 中模型把 artifacts 写成 deliverables 后父级漏测产物的问题。
def test_subagent_result_accepts_deliverables_alias_for_artifacts():
    text = (
        "[SUBAGENT_RESULT]\n"
        "{"
        '"status":"AWAITING_ACCEPTANCE",'
        '"summary":"homepage written",'
        '"deliverables":[{"path":"deliverables/furniture-home/index.html","kind":"html"}],'
        '"files":[{"summary":"not a path"}],'
        '"tests":[]'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )

    parsed = parse_subagent_runner_output(text)

    assert parsed.ok
    assert parsed.artifacts == [{"path": "deliverables/furniture-home/index.html", "kind": "html"}]


# LLM: Envelope conversion consumes parser artifacts, so aliases must survive into typed refs.
# 函数用途: 确认 deliverables 同义字段会变成 typed artifact_refs，供后续 refs-only 验收和恢复读取。
def test_subagent_result_envelope_accepts_deliverables_alias_for_artifacts():
    text = (
        "[SUBAGENT_RESULT]\n"
        "{"
        '"status":"AWAITING_ACCEPTANCE",'
        '"summary":"homepage written",'
        '"deliverables":[{"path":"deliverables/furniture-home/index.html","kind":"html"}]'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )

    envelope = parse_subagent_result_envelope(
        SubagentResultEnvelopeParseRequest(
            text=text,
            result_id="result-3",
            run_id="run-3",
            scope=RunScope(task_id="task-3", run_id="run-3"),
        )
    )

    assert envelope is not None
    assert envelope.artifact_refs[0].path == "deliverables/furniture-home/index.html"


# LLM: Structured repair may move the file path into evidence while leaving artifacts empty.
# 函数用途: 复现真实 E2E 中修复后的 SUBAGENT_RESULT 有 evidence.path 但 artifacts=[]，父级因此漏测的问题。
def test_subagent_result_recovers_artifact_from_evidence_path():
    text = (
        "[SUBAGENT_RESULT]\n"
        "{"
        '"status":"AWAITING_ACCEPTANCE",'
        '"summary":"homepage written",'
        '"evidence":[{"kind":"artifact","path":"/tmp/site/index.html","summary":"HTML"}],'
        '"artifacts":[]'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )

    parsed = parse_subagent_runner_output(text)

    assert parsed.ok
    assert parsed.artifacts == [{"path": "/tmp/site/index.html", "kind": "file", "summary": "HTML"}]


# LLM: Evidence packets are already refs-first claims and should feed parent artifact checks.
# 函数用途: 确认 evidence_packets.artifact_refs 会补成 artifacts，避免验收链路只看文字声明。
def test_subagent_result_recovers_artifact_from_evidence_packet_refs():
    text = (
        "[SUBAGENT_RESULT]\n"
        "{"
        '"status":"AWAITING_ACCEPTANCE",'
        '"summary":"homepage written",'
        '"evidence_packets":[{"claim":"HTML exists","artifact_refs":["/tmp/site/index.html"]}],'
        '"artifacts":[]'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )

    parsed = parse_subagent_runner_output(text)

    assert parsed.ok
    assert parsed.artifacts == [{"path": "/tmp/site/index.html", "kind": "file", "summary": "HTML exists"}]


# LLM: Repair workers often report changed files instead of repeating the canonical artifact list.
# 函数用途: 确认 files_modified 字符串列表会补成 artifacts，避免父级验收漏跑修复后的产物。
def test_subagent_result_recovers_artifact_from_files_modified():
    text = (
        "[SUBAGENT_RESULT]\n"
        "{"
        '"status":"COMPLETED",'
        '"summary":"homepage fixed",'
        '"files_modified":["deliverables/furniture-home/index.html"]'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )

    parsed = parse_subagent_runner_output(text)

    assert parsed.ok
    assert parsed.artifacts == [
        {
            "path": "deliverables/furniture-home/index.html",
            "kind": "file",
            "summary": "reported modified artifact",
        }
    ]

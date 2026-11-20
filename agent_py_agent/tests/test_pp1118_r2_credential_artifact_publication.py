"""Round-2 Rework Slice 6: 携卡产物有界清单准入、词元边界调试名识别、路径组件回显拦截与非冻结后台投递失败关闭测试。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.credential_artifact_paths import (
    is_blocked_credential_debug_path,
    path_contains_credential_echo,
    relative_artifact_path_parts,
)
from agent_py_agent.agent.tooling.host_output_redaction import (
    TurnCredentialRedaction,
    credential_echo_variants,
)
from agent_py_agent.agent.workflows import run_artifacts as run_artifacts_mod
from agent_py_agent.agent.workflows import script_runner as script_runner_mod
from agent_py_agent.agent.workflows.run_artifacts import (
    RunArtifactBatch,
    RunOutError,
    RunOutTarget,
    bind_run_out_target,
)
from agent_py_agent.agent.workflows.script_runner import (
    ScriptCredentialLease,
    ScriptRunner,
    _sanitize_or_block_credential_run_out,
)


def _assert_zero_secret_hits(payload_obj: object, secrets_tuple: tuple[str, ...]) -> None:
    rendered = payload_obj if isinstance(payload_obj, str) else json.dumps(payload_obj, ensure_ascii=False, default=str)
    for variant in credential_echo_variants(secrets_tuple):
        assert not (variant and variant in rendered), "detected unredacted credential echo variant in public surface"


def _make_cred_lease(secret_value: str) -> tuple[ScriptCredentialLease, TurnCredentialRedaction]:
    turn_red = TurnCredentialRedaction(owner_key="owner-r2-6", thread_id="thr-r2-6", turn_key="turn-" + uuid.uuid4().hex[:8])
    turn_red.register_values((secret_value,))
    lease = ScriptCredentialLease(
        credential_ref="cred_" + "a" * 32,
        credential_use=SimpleNamespace(turn_redaction=turn_red),
    )
    return lease, turn_red


@pytest.mark.parametrize(
    ("name", "expected_blocked"),
    [
        ("sales_chart.csv", False),
        ("sales_total.csv", False),
        ("monthly_share.csv", False),
        ("chargeback_report.txt", False),
        ("reports/sales_chart.csv", False),
        ("request.har", True),
        ("har_capture.json", True),
        ("cookie_jar.txt", True),
        ("cookie_dump.txt", True),
        ("auth_dump.json", True),
        ("storage_state.json", True),
        ("storagestate.json", True),
        ("trace_dump.log", True),
        ("capture.pcapng", True),
        ("client.pem", True),
        (".hidden.csv", True),
        (".secret_dir/sales_chart.csv", True),
    ],
)
def test_token_boundary_debug_classifier_allows_business_names_and_blocks_debug_dumps(
    name: str, expected_blocked: bool
) -> None:
    assert is_blocked_credential_debug_path(name) is expected_blocked


@pytest.mark.parametrize("exceed_mode", ["sparse_over_max_bytes", "sparse_4gib", "over_max_files"])
def test_oversized_and_overcount_run_out_rejected_before_any_content_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exceed_mode: str
) -> None:
    scratch = tmp_path / "scratch"
    run_out = scratch / "run_out"
    run_out.mkdir(parents=True)
    if exceed_mode == "sparse_over_max_bytes":
        with (run_out / "oversized.csv").open("wb") as fh:
            fh.truncate(ScriptRunner.RUN_OUT_MAX_BYTES + 1)
    elif exceed_mode == "sparse_4gib":
        with (run_out / "huge_4gib.csv").open("wb") as fh:
            fh.truncate(4 * 1024 * 1024 * 1024)
    else:
        for idx in range(ScriptRunner.RUN_OUT_MAX_FILES + 1):
            (run_out / f"part_{idx:03d}.csv").write_text("col\n1\n", encoding="utf-8")

    read_calls = 0

    def _forbid_read(*_args: object, **_kwargs: object) -> bytes:
        nonlocal read_calls
        read_calls += 1
        raise AssertionError("read_bytes_beneath must not be called before manifest limits pass")

    monkeypatch.setattr(script_runner_mod, "read_bytes_beneath", _forbid_read)
    monkeypatch.setattr(run_artifacts_mod, "read_bytes_beneath", _forbid_read)

    lease, turn_red = _make_cred_lease("SYNTHETIC_LIMIT_SECRET_7701")
    try:
        with pytest.raises(RunOutError) as caught:
            _sanitize_or_block_credential_run_out(
                scratch,
                lease,
                max_files=ScriptRunner.RUN_OUT_MAX_FILES,
                max_bytes=ScriptRunner.RUN_OUT_MAX_BYTES,
            )
        assert caught.value.error_code == "SCRIPT_RUN_OUT_LIMIT_EXCEEDED"
        assert read_calls == 0
    finally:
        turn_red.close()


def test_post_manifest_file_growth_rejected_with_limit_exceeded_and_bounded_max_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer

    scratch = tmp_path / "scratch"
    run_out = scratch / "run_out"
    run_out.mkdir(parents=True)
    report = run_out / "sales_chart.csv"
    report.write_text("item,total\nwidget,42\n", encoding="utf-8")

    observed_max_bytes: list[int | None] = []
    orig_read = script_runner_mod.read_bytes_beneath

    def _recording_read(root: Path, parts: tuple[str, ...], *, max_bytes: int | None = None, **kw: object):
        observed_max_bytes.append(max_bytes)
        return orig_read(root, parts, max_bytes=max_bytes, **kw)

    lease, turn_red = _make_cred_lease("SYNTHETIC_GROWTH_SECRET_8802")
    try:
        with monkeypatch.context() as mp:
            mp.setattr(script_runner_mod, "read_bytes_beneath", _recording_read)
            _sanitize_or_block_credential_run_out(
                scratch,
                lease,
                max_files=ScriptRunner.RUN_OUT_MAX_FILES,
                max_bytes=ScriptRunner.RUN_OUT_MAX_BYTES,
            )
        assert observed_max_bytes == [report.stat().st_size]

        # Simulate file growing after _manifest before read_bytes_beneath in _sanitize_or_block_credential_run_out
        orig_manifest = script_runner_mod.read_run_out_manifest

        def _grow_after_manifest(folder: Path, *, max_files: int, max_bytes: int):
            entries = orig_manifest(folder, max_files=max_files, max_bytes=max_bytes)
            with report.open("ab") as fh:
                fh.write(b"extra_row,99\n")
            return entries

        with monkeypatch.context() as mp:
            mp.setattr(script_runner_mod, "read_run_out_manifest", _grow_after_manifest)
            with pytest.raises(RunOutError) as caught_san:
                _sanitize_or_block_credential_run_out(
                    scratch,
                    lease,
                    max_files=ScriptRunner.RUN_OUT_MAX_FILES,
                    max_bytes=ScriptRunner.RUN_OUT_MAX_BYTES,
                )
            assert caught_san.value.error_code == "SCRIPT_RUN_OUT_LIMIT_EXCEEDED"

        # Also verify RunArtifactBatch.stage maps post-manifest file growth to SCRIPT_RUN_OUT_LIMIT_EXCEEDED
        owner_home = tmp_path / "owner_home"
        owner_home.mkdir(parents=True)
        batch = RunArtifactBatch(
            RunOutTarget(owner_home, "wfrun_grow", "step_grow", "c" * 32),
            OwnerQuotaEnforcer(owner_home, max_bytes=10 * 1024 * 1024),
        )
        orig_stage_manifest = run_artifacts_mod.read_run_out_manifest

        def _grow_during_stage(folder: Path, *, max_files: int, max_bytes: int):
            entries = orig_stage_manifest(folder, max_files=max_files, max_bytes=max_bytes)
            with report.open("ab") as fh:
                fh.write(b"more_growth,100\n")
            return entries

        with monkeypatch.context() as mp:
            mp.setattr(run_artifacts_mod, "read_run_out_manifest", _grow_during_stage)
            with pytest.raises(RunOutError) as caught_stage:
                batch.stage(
                    scratch,
                    max_files=ScriptRunner.RUN_OUT_MAX_FILES,
                    max_bytes=ScriptRunner.RUN_OUT_MAX_BYTES,
                )
            assert caught_stage.value.error_code == "SCRIPT_RUN_OUT_LIMIT_EXCEEDED"
        assert not list(owner_home.rglob("sales_chart.csv"))
    finally:
        turn_red.close()


def test_chart_and_total_csv_succeed_while_debug_and_secret_paths_blocked_across_all_three_boundaries(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
    from agent_py_agent.agent.capability.channel_message_tool import (
        SendMessageTool,
        deliver_host_script_intent,
    )
    from agent_py_agent.agent.conversation.background_delivery import (
        BackgroundDeliveryDependencies,
        record_background_response,
    )
    from agent_py_agent.agent.delivery import (
        ChannelAdapterRegistry,
        ChannelAttachment,
        ChannelCapabilities,
        DeliveryContext,
        DeliveryService,
        ReplyEnvelope,
    )
    from agent_py_agent.agent.tooling.host_output_redaction import turn_credential_redaction
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
    from agent_py_agent.tests.test_fc2_credential_shell_boundary import (
        _active,
        _agent,
        _submit_custom_card,
    )
    from agent_py_agent.tests.test_im_progress_draft import Adapter

    agent = _agent(tmp_path, "user", full=False)
    thread, actor, _claim = _active(agent)
    secret_val = "SynthPathCardSecret9421"
    ref = _submit_custom_card(
        agent,
        "https://reports.example.test/api",
        fields_spec=[{"name": "secret", "label": "Token"}],
        submitted_values={"secret": secret_val},
        authentication="bearer",
        collection_mode="password",
    )

    owner_home = Path(agent.home_paths.owner_home_dir)
    adapter = Adapter()
    adapter.supports_frozen_attachment_content = True
    adapter.send_attachment_content = lambda target, item, **_k: (
        adapter.finals.append((target, item.attachment.name)),
        True,
    )[1]
    ch_registry = ChannelAdapterRegistry()
    ch_registry.register_adapter(
        "feishu",
        adapter,
        capabilities=ChannelCapabilities(
            proactive=True,
            reply=True,
            edit_progress=True,
            files=True,
            frozen_attachment_content=True,
            control_cards=True,
        ),
    )
    agent.delivery_service = DeliveryService(ch_registry)
    agent.channel_registry = ch_registry

    # --- Boundary 1: ScriptRunner + ScriptsTool + RunArtifactBatch ---
    scripts_tool = ScriptsTool(
        owner_home,
        workspace_root=agent.tools.workspace_root,
        runner=ScriptRunner(
            owner_home,
            workspace_root=agent.tools.workspace_root,
            read_scope_mode="exact",
            owner_scope_root=owner_home,
            agent_home_root=agent.home_paths.root,
            run_out_quota=agent.owner_quota,
        ),
    )
    reg = scripts_tool.execute(
        {
            "action": "register",
            "script_id": "artifact_pub_probe",
            "code": (
                "import json, os\n"
                "from pathlib import Path\n"
                "inp = json.loads(os.environ.get('MY_AGENT_SCRIPT_INPUT_JSON', '{}'))\n"
                "cred_path = os.environ.get('MY_AGENT_CREDENTIAL_FILE', '')\n"
                "cred = json.load(open(cred_path, encoding='utf-8')) if cred_path else {}\n"
                "sec = cred.get('values', {}).get('secret', '')\n"
                "out = Path(os.environ['MY_AGENT_RUN_OUT'])\n"
                "mode = inp.get('mode', 'clean')\n"
                "if mode == 'clean':\n"
                "    (out / 'sales_chart.csv').write_text('region,amount\\nAPAC,120\\n', encoding='utf-8')\n"
                "    (out / 'sales_total.csv').write_text('total\\n120\\n', encoding='utf-8')\n"
                "    (out / 'monthly_share.csv').write_text('share\\n0.5\\n', encoding='utf-8')\n"
                "elif mode == 'har':\n"
                "    (out / 'request.har').write_text('{\"log\":{}}', encoding='utf-8')\n"
                "elif mode == 'cookie_dump':\n"
                "    (out / 'cookie_dump.txt').write_text('sid=1', encoding='utf-8')\n"
                "elif mode == 'secret_parent':\n"
                "    (out / 'sales_total.csv').write_text('total\\n120\\n', encoding='utf-8')\n"
                "    sub = out / f'reports_{sec}'\n"
                "    sub.mkdir(parents=True, exist_ok=True)\n"
                "    (sub / 'out.csv').write_text('count\\n1\\n', encoding='utf-8')\n"
                "elif mode == 'secret_leaf':\n"
                "    (out / 'sales_chart.csv').write_text('region,amount\\nAPAC,120\\n', encoding='utf-8')\n"
                "    (out / f'sales_{sec}.csv').write_text('count\\n1\\n', encoding='utf-8')\n"
                "elif mode == 'oversized':\n"
                "    with (out / 'big_export.csv').open('wb') as fh:\n"
                "        fh.truncate(64 * 1024 * 1024 + 1)\n"
                "print(json.dumps({'ok_flag': True}))\n"
            ),
            "descriptor": {
                "name": "产物边界探针脚本",
                "access_targets": [],
                "data_sources": [],
                "credential_refs": [],
                "io_contract": {"inputs": [], "outputs": ["ok_flag"]},
            },
        }
    )
    assert reg.ok, reg.output

    boundary = write_boundary_with_runtime_ledger(agent, agent._current_run_params)
    cred_use = boundary["_host_output_redaction"]._credential_use.begin_invocation(boundary["_host_output_redaction"])
    ctx = SimpleNamespace(credential_use=cred_use, execution_authority_check=None)
    try:
        # 1a. sales_chart.csv, sales_total.csv, monthly_share.csv all succeed in script_runner
        with bind_run_out_target(owner_home, "wfrun_clean", "step_clean", "1" * 32):
            res_clean = scripts_tool.execute_scoped(
                {"action": "run", "script_id": "artifact_pub_probe", "inputs": {"mode": "clean"}, "credential_ref": ref},
                ctx,
            )
        assert res_clean.ok, res_clean.output
        published_names = {item["path"] for item in res_clean.result_envelope["output_artifacts"]}
        assert published_names == {"sales_chart.csv", "sales_total.csv", "monthly_share.csv"}

        # 1b. request.har, cookie_dump.txt, reports_<secret>/out.csv, sales_<secret>.csv all blocked with 0 partial batch
        for idx, blocked_mode in enumerate(("har", "cookie_dump", "secret_parent", "secret_leaf"), start=2):
            run_id = f"wfrun_blk_{idx}"
            attempt_id = str(idx) * 32
            with bind_run_out_target(owner_home, run_id, "step_blk", attempt_id):
                res_blk = scripts_tool.execute_scoped(
                    {
                        "action": "run",
                        "script_id": "artifact_pub_probe",
                        "inputs": {"mode": blocked_mode},
                        "credential_ref": ref,
                    },
                    ctx,
                )
            assert not res_blk.ok
            assert res_blk.reported_error_code == "SCRIPT_CREDENTIAL_ARTIFACT_BLOCKED"
            assert res_blk.result_envelope["error_code"] == "SCRIPT_CREDENTIAL_ARTIFACT_BLOCKED"
            assert not res_blk.result_envelope.get("output_artifacts")
            if blocked_mode in {"secret_parent", "secret_leaf"}:
                diag = res_blk.result_envelope["escalation_context"]["execution_error"]
                assert diag["path_role"] == "run_out_name"
            run_art_root = owner_home / "data" / "workflow_runs" / run_id
            assert not run_art_root.exists() or list(run_art_root.rglob("*")) == []
            _assert_zero_secret_hits(res_blk.output, (secret_val,))
            _assert_zero_secret_hits(res_blk.result_envelope, (secret_val,))

        # 1c. Oversized sparse file in real credentialed script returns SCRIPT_RUN_OUT_LIMIT_EXCEEDED
        with bind_run_out_target(owner_home, "wfrun_over", "step_over", "9" * 32):
            res_over = scripts_tool.execute_scoped(
                {"action": "run", "script_id": "artifact_pub_probe", "inputs": {"mode": "oversized"}, "credential_ref": ref},
                ctx,
            )
        assert not res_over.ok
        assert res_over.result_envelope["error_code"] == "SCRIPT_RUN_OUT_LIMIT_EXCEEDED"

        # --- Boundary 2: Explicit SendMessageTool & deliver_host_script_intent ---
        task_root = owner_home / "tasks" / "r2_6_send"
        out_dir = task_root / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        chart_path = out_dir / "sales_chart.csv"
        chart_path.write_text("region,amount\nAPAC,120\n", encoding="utf-8")
        chart_rec = register_artifact(ArtifactRegistration(workspace_root=task_root, path=chart_path, kind="text"))

        total_path = out_dir / "sales_total.csv"
        total_path.write_text("total\n120\n", encoding="utf-8")
        total_rec = register_artifact(ArtifactRegistration(workspace_root=task_root, path=total_path, kind="text"))

        har_path = out_dir / "request.har"
        har_path.write_text('{"log":{}}', encoding="utf-8")
        har_rec = register_artifact(ArtifactRegistration(workspace_root=task_root, path=har_path, kind="text"))

        cookie_path = out_dir / "cookie_dump.txt"
        cookie_path.write_text("sid=1", encoding="utf-8")
        cookie_rec = register_artifact(ArtifactRegistration(workspace_root=task_root, path=cookie_path, kind="text"))

        secret_parent_dir = out_dir / f"reports_{secret_val}"
        secret_parent_dir.mkdir(parents=True, exist_ok=True)
        secret_parent_file = secret_parent_dir / "out.csv"
        secret_parent_file.write_text("count\n1\n", encoding="utf-8")
        secret_parent_rec = register_artifact(
            ArtifactRegistration(workspace_root=task_root, path=secret_parent_file, kind="text")
        )

        secret_leaf_file = out_dir / f"sales_{secret_val}.csv"
        secret_leaf_file.write_text("count\n1\n", encoding="utf-8")
        secret_leaf_rec = register_artifact(
            ArtifactRegistration(workspace_root=task_root, path=secret_leaf_file, kind="text")
        )

        send_tool = SendMessageTool(agent)
        run_scope = {
            "request_id": agent._current_run_params.request_id,
            "run_id": agent._current_run_params.run_id,
            "task_id": agent._current_run_params.task_id,
            "session_id": thread.thread_id,
        }

        ok_chart = send_tool.execute(
            {
                "notify_channel": "feishu",
                "message": "销售图表与汇总已生成",
                "attachments": [chart_rec.artifact_id, total_rec.artifact_id],
                "__run_scope": run_scope,
                "__artifact_task_root": str(task_root),
                "__artifact_registry_root": str(task_root),
            }
        )
        assert ok_chart.ok, ok_chart.output
        sent_payload = json.loads(ok_chart.output)
        assert [a["name"] for a in sent_payload["attachments"]] == ["sales_chart.csv", "sales_total.csv"]

        finals_before_blocked = len(adapter.finals)
        for bad_att in (
            har_rec.artifact_id,
            cookie_rec.artifact_id,
            secret_parent_rec.artifact_id,
            secret_leaf_rec.artifact_id,
            f"unregistered_{secret_val}.csv",
        ):
            bad_send = send_tool.execute(
                {
                    "notify_channel": "feishu",
                    "message": "尝试发送受限附件",
                    "attachments": [bad_att],
                    "__run_scope": run_scope,
                    "__artifact_task_root": str(task_root),
                    "__artifact_registry_root": str(task_root),
                }
            )
            assert not bad_send.ok
            assert bad_send.reported_error_code == "CREDENTIAL_ATTACHMENT_WITHHELD"
            assert bad_send.effect_outcome == "not_started"
            _assert_zero_secret_hits(bad_send.output, (secret_val,))
            _assert_zero_secret_hits(bad_send.result_envelope, (secret_val,))
        assert len(adapter.finals) == finals_before_blocked

        # Also verify deliver_host_script_intent blocks secret parent path and allows sales_chart.csv
        intent_ctx_ok = DeliveryContext(
            channel="feishu",
            target=actor.actor_id,
            thread_id=thread.thread_id,
            task_id=agent._current_run_params.task_id,
            request_id=agent._current_run_params.request_id,
            mode="proactive",
            idempotency_key="script-intent-chart-ok",
        )
        intent_ok = deliver_host_script_intent(
            agent,
            intent_ctx_ok,
            ReplyEnvelope(
                content="脚本图表附件",
                attachments=(
                    ChannelAttachment(
                        artifact_id=chart_rec.artifact_id,
                        path=str(chart_path),
                        name="sales_chart.csv",
                        kind="text",
                        sha256=chart_rec.sha256,
                        size_bytes=chart_rec.size_bytes,
                        owner_root=str(owner_home),
                    ),
                ),
            ),
        )
        assert intent_ok["status"] == "sent"

        intent_ctx_blk = DeliveryContext(
            channel="feishu",
            target=actor.actor_id,
            thread_id=thread.thread_id,
            task_id=agent._current_run_params.task_id,
            request_id=agent._current_run_params.request_id,
            mode="proactive",
            idempotency_key="script-intent-secret-parent-blk",
        )
        intent_blk = deliver_host_script_intent(
            agent,
            intent_ctx_blk,
            ReplyEnvelope(
                content="脚本敏感目录附件",
                attachments=(
                    ChannelAttachment(
                        artifact_id=secret_parent_rec.artifact_id,
                        path=str(secret_parent_file),
                        name="out.csv",
                        kind="text",
                        sha256=secret_parent_rec.sha256,
                        size_bytes=secret_parent_rec.size_bytes,
                        owner_root=str(owner_home),
                    ),
                ),
            ),
        )
        assert intent_blk["status"] == "failed"
        assert intent_blk["error_code"] == "CREDENTIAL_ATTACHMENT_WITHHELD"
        _assert_zero_secret_hits(intent_blk, (secret_val,))

        # --- Boundary 3: Background Delivery ---
        bg_adapter = Adapter()
        bg_adapter.supports_frozen_attachment_content = True
        bg_adapter.send_attachment_content = lambda target, item, **_k: (
            bg_adapter.finals.append((target, item.attachment.name)),
            True,
        )[1]
        bg_reg = ChannelAdapterRegistry()
        bg_reg.register_adapter(
            "feishu",
            bg_adapter,
            capabilities=ChannelCapabilities(
                proactive=True,
                reply=True,
                files=True,
                frozen_attachment_content=True,
            ),
        )
        bg_svc = DeliveryService(bg_reg)
        deps = BackgroundDeliveryDependencies(
            agent=agent,
            store=agent.conversation_store,
            channels=bg_svc,
            task_status=lambda _r: "active",
        )
        wake = agent.conversation_store.wakes.raise_signal(
            {
                "thread_id": thread.thread_id,
                "root_task_id": agent._current_run_params.task_id,
                "reason": "task_input_resolved",
                "metadata": {"input_provider": "credential", "credential_ref": ref},
            }
        )
        bg_req = SimpleNamespace(
            thread_id=thread.thread_id,
            task_id=agent._current_run_params.task_id,
            reason="task_input_resolved",
            conversation_turn_id=agent._current_run_params.request_id,
            wake_signal=wake.to_dict(),
        )
        bg_commit = record_background_response(
            deps,
            bg_req,
            DeliveryContext(
                channel="feishu",
                target=actor.actor_id,
                thread_id=thread.thread_id,
                task_id=agent._current_run_params.task_id,
                mode="reply",
                idempotency_key="bg-r2-6-artifacts",
            ),
            "后台报表汇总已完成。",
            delivery_artifacts=(
                {
                    "artifact_id": chart_rec.artifact_id,
                    "path": str(chart_path),
                    "name": "sales_chart.csv",
                    "kind": "text",
                    "sha256": chart_rec.sha256,
                    "size_bytes": chart_rec.size_bytes,
                    "ok": True,
                },
                {
                    "artifact_id": total_rec.artifact_id,
                    "path": str(total_path),
                    "name": "sales_total.csv",
                    "kind": "text",
                    "sha256": total_rec.sha256,
                    "size_bytes": total_rec.size_bytes,
                    "ok": True,
                },
                {
                    "artifact_id": har_rec.artifact_id,
                    "path": str(har_path),
                    "name": "request.har",
                    "kind": "text",
                    "sha256": har_rec.sha256,
                    "size_bytes": har_rec.size_bytes,
                    "ok": True,
                },
                {
                    "artifact_id": cookie_rec.artifact_id,
                    "path": str(cookie_path),
                    "name": "cookie_dump.txt",
                    "kind": "text",
                    "sha256": cookie_rec.sha256,
                    "size_bytes": cookie_rec.size_bytes,
                    "ok": True,
                },
                {
                    "artifact_id": secret_parent_rec.artifact_id,
                    "path": str(secret_parent_file),
                    "name": "out.csv",
                    "kind": "text",
                    "sha256": secret_parent_rec.sha256,
                    "size_bytes": secret_parent_rec.size_bytes,
                    "ok": True,
                },
                {
                    "artifact_id": secret_leaf_rec.artifact_id,
                    "path": str(secret_leaf_file),
                    "name": secret_leaf_file.name,
                    "kind": "text",
                    "sha256": secret_leaf_rec.sha256,
                    "size_bytes": secret_leaf_rec.size_bytes,
                    "ok": True,
                },
            ),
            deliver=True,
            delivery_reason="task_input_resolved",
        )
        assert bg_commit.delivery_status == "sent"
        stored_rows = agent.conversation_store.messages.history_page_report(thread.thread_id, limit=20).rows
        last_meta = stored_rows[-1].metadata
        delivered_names = [a["name"] for a in last_meta["delivery_artifacts"]]
        assert delivered_names == ["sales_chart.csv", "sales_total.csv"]
        withheld_reasons = {w["artifact_id"]: w["reason"] for w in last_meta["withheld_credential_attachments"]}
        assert withheld_reasons == {
            har_rec.artifact_id: "CREDENTIAL_DEBUG_OR_BINARY_ATTACHMENT_BLOCKED",
            cookie_rec.artifact_id: "CREDENTIAL_DEBUG_OR_BINARY_ATTACHMENT_BLOCKED",
            secret_parent_rec.artifact_id: "CREDENTIAL_ECHO_IN_ATTACHMENT",
            secret_leaf_rec.artifact_id: "CREDENTIAL_ECHO_IN_ATTACHMENT",
        }
        _assert_zero_secret_hits(
            agent.conversation_store.storage.message_path(thread.thread_id).read_text(encoding="utf-8"),
            (secret_val,),
        )
    finally:
        cred_use.disarm()
        turn_credential_redaction(agent, agent._current_run_params).close()

    # Pure helper coverage for relative_artifact_path_parts & path_contains_credential_echo
    probe_red = TurnCredentialRedaction(owner_key="o1", thread_id="t1", turn_key="k1")
    probe_red.register_values((secret_val,))
    try:
        rel = relative_artifact_path_parts(secret_parent_file, base_root=owner_home)
        assert f"reports_{secret_val}" in rel
        assert path_contains_credential_echo(rel, (probe_red,)) is True
    finally:
        probe_red.close()


def test_non_frozen_first_background_delivery_with_provenance_and_no_active_redaction_fails_closed(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.conversation.background_delivery import (
        BackgroundDeliveryDependencies,
        _prepare_credential_safe_background_inputs,
        record_background_response,
    )
    from agent_py_agent.agent.delivery import (
        ChannelAdapterRegistry,
        ChannelCapabilities,
        DeliveryContext,
        DeliveryService,
    )
    from agent_py_agent.tests.test_fc2_credential_shell_boundary import _active, _agent
    from agent_py_agent.tests.test_im_progress_draft import Adapter

    agent = _agent(tmp_path, "user", full=False)
    thread, actor, _claim = _active(agent)
    adapter = Adapter()
    reg = ChannelAdapterRegistry()
    reg.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(proactive=True, reply=True))
    svc = DeliveryService(reg)
    deps = BackgroundDeliveryDependencies(
        agent=agent,
        store=agent.conversation_store,
        channels=svc,
        task_status=lambda _r: "active",
    )

    raw_sensitive = "UNVERIFIED_FIRST_DELIVERY_RAW_MARKER_5501"
    cred_wake = agent.conversation_store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": agent._current_run_params.task_id,
            "reason": "task_input_resolved",
            "metadata": {"input_provider": "credential", "credential_ref": "cred_" + "e" * 32},
        }
    )
    cred_req = SimpleNamespace(
        thread_id=thread.thread_id,
        task_id=agent._current_run_params.task_id,
        reason="task_input_resolved",
        conversation_turn_id="gwreq-no-active-red-" + uuid.uuid4().hex[:8],
        wake_signal=cred_wake.to_dict(),
    )

    # 1. Direct _prepare_credential_safe_background_inputs with frozen_retry=None & no active redactor -> withheld
    prepared = _prepare_credential_safe_background_inputs(
        deps,
        cred_req,
        raw_sensitive,
        public_receipt={"status": "ok"},
        delivery_artifacts=({"artifact_id": "art_1", "name": "sales_chart.csv"},),
        assistant_commentaries=("commentary",),
        display_snapshot={"request_id": "bg-snap-1"},
        frozen_retry=None,
    )
    content_out, receipt_out, arts_out, comm_out, snap_out, verified_out, diag_out = prepared
    assert "已安全扣留" in content_out
    assert raw_sensitive not in content_out
    assert receipt_out is None
    assert arts_out == ()
    assert comm_out == ()
    # 扣留时不保留未核验的显示快照（R3-P1-05）。
    assert snap_out is None
    assert verified_out is True
    assert diag_out == {
        "credential_delivery_withheld": {
            "reason_code": "UNVERIFIED_CREDENTIAL_FROZEN_DELIVERY",
            "action": "withheld",
        }
    }

    # 2. Full record_background_response (non-frozen first delivery, tool event in snapshot, no active redactor) -> withheld
    tool_snap = {
        "schema_version": "background_display_turn.v1",
        "thread_id": thread.thread_id,
        "task_id": agent._current_run_params.task_id,
        "request_id": f"bg-main:{thread.thread_id}:1",
        "complete": True,
        "events": [
            {
                "request_id": f"bg-main:{thread.thread_id}:1",
                "block_id": f"bg-main:{thread.thread_id}:1:tool:1",
                "kind": "tool_completed",
                "phase": "completed",
                "payload": {"tool": "web_fetch"},
            }
        ],
    }
    withheld_commit = record_background_response(
        deps,
        cred_req,
        DeliveryContext(
            channel="feishu",
            target=actor.actor_id,
            thread_id=thread.thread_id,
            task_id=agent._current_run_params.task_id,
            mode="reply",
            idempotency_key="bg-first-unverified-withheld",
        ),
        raw_sensitive,
        display_snapshot=tool_snap,
        operation_verification={"schema": "operation_verification.public.v1", "status": "none", "operation_count": 0},
        deliver=True,
        delivery_reason="task_input_resolved",
        frozen_retry=None,
    )
    assert "已安全扣留" in withheld_commit.content
    assert raw_sensitive not in withheld_commit.content
    assert all(raw_sensitive not in str(item) for item in adapter.finals)
    rows = agent.conversation_store.messages.history_page_report(thread.thread_id, limit=10).rows
    assert rows[-1].metadata["credential_delivery_withheld"] == {
        "reason_code": "UNVERIFIED_CREDENTIAL_FROZEN_DELIVERY",
        "action": "withheld",
    }

    # 3. Ordinary non-credential background delivery with no active redactor delivers normally
    normal_wake = agent.conversation_store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": agent._current_run_params.task_id,
            "reason": "thread_goal_continue",
            "metadata": {},
        }
    )
    normal_req = SimpleNamespace(
        thread_id=thread.thread_id,
        task_id=agent._current_run_params.task_id,
        reason="thread_goal_continue",
        conversation_turn_id="gwreq-normal-" + uuid.uuid4().hex[:8],
        wake_signal=normal_wake.to_dict(),
    )
    normal_text = "普通无凭据后台任务正常送达：NORMAL_DELIVERY_OK_01"
    normal_commit = record_background_response(
        deps,
        normal_req,
        DeliveryContext(
            channel="feishu",
            target=actor.actor_id,
            thread_id=thread.thread_id,
            task_id=agent._current_run_params.task_id,
            mode="reply",
            idempotency_key="bg-first-normal-ok",
        ),
        normal_text,
        deliver=True,
        delivery_reason="thread_goal_continue",
        frozen_retry=None,
    )
    assert normal_commit.delivery_status == "sent"
    assert "NORMAL_DELIVERY_OK_01" in normal_commit.content

    # 4. A live unclosed 0-value TurnCredentialRedaction on the request does not prove a zero-tool turn (R3-P1-04):
    #    without the live slice's authoritative tool count it fails closed, and a closed binding fails closed too.
    #    The authoritative zero-count exemption is covered by
    #    test_unknown_tool_count_withheld_while_authoritative_zero_tool_turn_delivers.
    zero_tool_tr = TurnCredentialRedaction(owner_key="owner-zero", thread_id=thread.thread_id, turn_key="turn-zero")
    live_zero_req = SimpleNamespace(
        thread_id=thread.thread_id,
        task_id=agent._current_run_params.task_id,
        reason="task_input_resolved",
        conversation_turn_id="gwreq-live-zero-" + uuid.uuid4().hex[:8],
        wake_signal=cred_wake.to_dict(),
        _turn_credential_redaction=zero_tool_tr,
    )
    empty_binding_prepared = _prepare_credential_safe_background_inputs(
        deps,
        live_zero_req,
        raw_sensitive,
        public_receipt={"status": "ok"},
        delivery_artifacts=(),
        assistant_commentaries=(),
        display_snapshot=None,
        frozen_retry=None,
    )
    assert "已安全扣留" in empty_binding_prepared[0]
    assert raw_sensitive not in empty_binding_prepared[0]
    assert empty_binding_prepared[6]["credential_delivery_withheld"]["reason_code"] == "UNVERIFIED_CREDENTIAL_FROZEN_DELIVERY"

    zero_tool_tr.close()
    closed_tr_prepared = _prepare_credential_safe_background_inputs(
        deps,
        live_zero_req,
        raw_sensitive,
        public_receipt={"status": "ok"},
        delivery_artifacts=(),
        assistant_commentaries=(),
        display_snapshot=None,
        frozen_retry=None,
    )
    assert "已安全扣留" in closed_tr_prepared[0]
    assert raw_sensitive not in closed_tr_prepared[0]
    assert closed_tr_prepared[6]["credential_delivery_withheld"]["reason_code"] == "UNVERIFIED_CREDENTIAL_FROZEN_DELIVERY"


_WITHHELD_DIAGNOSTICS = {
    "credential_delivery_withheld": {
        "reason_code": "UNVERIFIED_CREDENTIAL_FROZEN_DELIVERY",
        "action": "withheld",
    }
}
_ZERO_OPERATIONS = {"schema": "operation_verification.public.v1", "status": "none", "operation_count": 0}


def _credential_delivery_case(tmp_path: Path) -> SimpleNamespace:
    from agent_py_agent.agent.conversation.background_delivery import BackgroundDeliveryDependencies
    from agent_py_agent.agent.delivery import (
        ChannelAdapterRegistry,
        ChannelCapabilities,
        DeliveryService,
    )
    from agent_py_agent.tests.test_fc2_credential_shell_boundary import _active, _agent
    from agent_py_agent.tests.test_im_progress_draft import Adapter

    agent = _agent(tmp_path, "user", full=False)
    thread, actor, _claim = _active(agent)
    adapter = Adapter()
    reg = ChannelAdapterRegistry()
    reg.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(proactive=True, reply=True))
    deps = BackgroundDeliveryDependencies(
        agent=agent,
        store=agent.conversation_store,
        channels=DeliveryService(reg),
        task_status=lambda _r: "active",
    )
    return SimpleNamespace(
        agent=agent,
        thread=thread,
        actor=actor,
        adapter=adapter,
        deps=deps,
        task_id=agent._current_run_params.task_id,
        owner_key=str(agent.home_paths.owner_home_dir),
        conversation_root=str(agent.conversation_store.storage.root),
    )


def _credential_request(case: SimpleNamespace, turn_id: str, *, bound: object = None) -> SimpleNamespace:
    wake = case.agent.conversation_store.wakes.raise_signal(
        {
            "thread_id": case.thread.thread_id,
            "root_task_id": case.task_id,
            "reason": "task_input_resolved",
            "metadata": {"input_provider": "credential", "credential_ref": "cred_" + uuid.uuid4().hex},
        }
    )
    request = SimpleNamespace(
        thread_id=case.thread.thread_id,
        task_id=case.task_id,
        reason="task_input_resolved",
        conversation_turn_id=turn_id,
        wake_signal=wake.to_dict(),
    )
    if bound is not None:
        request._turn_credential_redaction = bound
    return request


def _display_snapshot(case: SimpleNamespace, *events: dict[str, object]) -> dict[str, object]:
    request_id = f"bg-main:{case.thread.thread_id}:1"
    return {
        "schema_version": "background_display_turn.v1",
        "thread_id": case.thread.thread_id,
        "task_id": case.task_id,
        "request_id": request_id,
        "complete": True,
        "events": [
            {"request_id": request_id, "block_id": f"{request_id}:block:{index}", **event}
            for index, event in enumerate(events, start=1)
        ],
    }


def _prepare_first_delivery(
    case: SimpleNamespace,
    request: SimpleNamespace,
    content: str,
    *,
    snapshot: dict[str, object] | None,
    tool_call_count: int | None = None,
) -> tuple:
    from agent_py_agent.agent.conversation.background_delivery import (
        _prepare_credential_safe_background_inputs,
    )

    # 未知计数不传参，与 direct/recovery 调用方一致（默认 None）。
    count_option = {} if tool_call_count is None else {"tool_call_count": tool_call_count}
    return _prepare_credential_safe_background_inputs(
        case.deps,
        request,
        content,
        public_receipt={"status": "ok"},
        delivery_artifacts=(),
        assistant_commentaries=("过程说明",),
        display_snapshot=snapshot,
        frozen_retry=None,
        operation_verification=_ZERO_OPERATIONS,
        **count_option,
    )


def _assert_withheld(prepared: tuple, raw: str) -> None:
    content, receipt, artifacts, commentaries, snapshot, verified, diagnostics = prepared
    assert "已安全扣留" in content and raw not in content
    assert (receipt, artifacts, commentaries, snapshot, verified) == (None, (), (), None, True)
    assert diagnostics == _WITHHELD_DIAGNOSTICS


_TOOL_EVENT = {"kind": "tool_completed", "phase": "completed", "payload": {"tool": "web_fetch"}}


@pytest.mark.parametrize(
    ("tool_call_count", "events"),
    [(1, ()), (None, (_TOOL_EVENT,)), (0, (_TOOL_EVENT,))],
    ids=["positive_count", "unknown_count_tool_event", "zero_count_tool_event"],
)
def test_empty_request_bound_redactor_does_not_prove_zero_tool_turn(
    tmp_path: Path, tool_call_count: int | None, events: tuple
) -> None:
    case = _credential_delivery_case(tmp_path)
    empty_tr = TurnCredentialRedaction(
        owner_key=case.owner_key, thread_id=case.thread.thread_id, turn_key="turn-empty-" + uuid.uuid4().hex[:8]
    )
    try:
        assert not empty_tr.active and not empty_tr.withheld
        request = _credential_request(case, "gwreq-empty-" + uuid.uuid4().hex[:8], bound=empty_tr)
        raw = "EMPTY_BINDING_RAW_MARKER_6601"
        prepared = _prepare_first_delivery(
            case, request, raw, snapshot=_display_snapshot(case, *events), tool_call_count=tool_call_count
        )
        _assert_withheld(prepared, raw)
    finally:
        empty_tr.close()


@pytest.mark.parametrize("neighbor_task", ["shared_task_id", "other_task_id"])
@pytest.mark.parametrize("current_binding", ["missing", "closed", "exact_scope_active", "request_bound_active"])
def test_neighbor_turn_redactor_in_same_thread_never_verifies_current_turn(
    tmp_path: Path, neighbor_task: str, current_binding: str
) -> None:
    from agent_py_agent.agent.tooling.host_output_redaction import turn_credential_redaction

    case = _credential_delivery_case(tmp_path)
    neighbor_params = SimpleNamespace(
        request_id="gwreq-neighbor-" + uuid.uuid4().hex[:8],
        run_id="",
        task_id=case.task_id if neighbor_task == "shared_task_id" else "task-neighbor-" + uuid.uuid4().hex[:8],
        conversation_turn_id="",
        thread_id=case.thread.thread_id,
        task_attributes={},
    )
    neighbor = turn_credential_redaction(case.agent, neighbor_params)
    neighbor.register_values(("NeighborTurnSecret_7702",))
    current_turn = "gwreq-current-" + uuid.uuid4().hex[:8]
    current_secret = "CurrentTurnSecret_7703"
    current = TurnCredentialRedaction(
        owner_key=case.owner_key,
        conversation_root=case.conversation_root,
        thread_id=case.thread.thread_id,
        # 请求上直接绑定的对象沿宿主请求号，可与 conversation_turn_id 不同；索引查找只认精确回合号。
        turn_key=current_turn if current_binding == "exact_scope_active" else "gwreq-bound-" + uuid.uuid4().hex[:8],
    )
    try:
        assert neighbor.active and neighbor.thread_id == case.thread.thread_id
        assert (case.task_id in neighbor._turn_aliases) is (neighbor_task == "shared_task_id")
        if current_binding != "missing":
            current.register_values((current_secret,))
        if current_binding == "closed":
            current.close()
        bound = current if current_binding in {"closed", "request_bound_active"} else None
        request = _credential_request(case, current_turn, bound=bound)
        snapshot = _display_snapshot(
            case, {"kind": "tool_completed", "phase": "completed", "payload": {"tool": "web_fetch", "text": f"抓取 {current_secret}"}}
        )
        raw = f"后台结果 {current_secret}"
        prepared = _prepare_first_delivery(case, request, raw, snapshot=snapshot)
        if current_binding in {"missing", "closed"}:
            _assert_withheld(prepared, raw)
        else:
            content, _receipt, _artifacts, _commentaries, safe_snapshot, _verified, diagnostics = prepared
            assert diagnostics == {"credential_delivery_scoped": True}
            assert "后台结果" in content and current_secret not in content
            # 已验证投递保留完整显示快照，只做脱敏。
            assert isinstance(safe_snapshot, dict) and len(safe_snapshot["events"]) == 1
            assert current_secret not in json.dumps(safe_snapshot, ensure_ascii=False)
    finally:
        current.close()
        neighbor.close()


@pytest.mark.parametrize("tool_call_count", [1, None], ids=["read_only_call", "unknown_count"])
def test_read_only_tool_turn_with_zero_side_effect_operations_and_no_binding_is_withheld(
    tmp_path: Path, tool_call_count: int | None
) -> None:
    case = _credential_delivery_case(tmp_path)
    request = _credential_request(case, "gwreq-read-only-" + uuid.uuid4().hex[:8])
    # 只读 web_fetch 不计入副作用 operation_count；未保存终态的工具块在显示快照里只剩 system_message。
    snapshot = _display_snapshot(
        case, {"kind": "system_message", "phase": "completed", "payload": {"text": "web_fetch：本工作片没有保存完整终态。"}}
    )
    raw = "READ_ONLY_FETCH_RAW_MARKER_6603"
    _assert_withheld(_prepare_first_delivery(case, request, raw, snapshot=snapshot, tool_call_count=tool_call_count), raw)


def test_unknown_tool_count_withheld_while_authoritative_zero_tool_turn_delivers(tmp_path: Path) -> None:
    from agent_py_agent.agent.conversation.background_delivery import record_background_response
    from agent_py_agent.agent.delivery import DeliveryContext

    case = _credential_delivery_case(tmp_path)
    # 实时路径的请求带着未登记值的空回合绑定；结论只取决于本片权威计数。
    empty_tr = TurnCredentialRedaction(
        owner_key=case.owner_key, thread_id=case.thread.thread_id, turn_key="turn-live-" + uuid.uuid4().hex[:8]
    )

    def deliver(text: str, key: str, count: int | None):
        count_option = {} if count is None else {"tool_call_count": count}
        return record_background_response(
            case.deps,
            _credential_request(case, f"gwreq-{key}-" + uuid.uuid4().hex[:8], bound=empty_tr),
            DeliveryContext(
                channel="feishu",
                target=case.actor.actor_id,
                thread_id=case.thread.thread_id,
                task_id=case.task_id,
                mode="reply",
                idempotency_key=f"bg-r3-3-{key}",
            ),
            text,
            operation_verification=_ZERO_OPERATIONS,
            display_snapshot=_display_snapshot(case),
            deliver=True,
            delivery_reason="task_input_resolved",
            **count_option,
        )

    unknown_text = "UNKNOWN_COUNT_RAW_MARKER_6604"
    zero_text = "零工具回合安全回复：ZERO_TOOL_SAFE_02"
    try:
        unknown = deliver(unknown_text, "unknown", None)
        zero = deliver(zero_text, "zero", 0)
    finally:
        empty_tr.close()
    assert "已安全扣留" in unknown.content and unknown_text not in unknown.content
    assert zero.delivery_status == "sent" and zero_text in zero.content
    sent = [content for _target, content in case.adapter.finals]
    assert all(unknown_text not in content for content in sent) and any(zero_text in content for content in sent)
    finals = [
        row
        for row in case.agent.conversation_store.messages.history_page_report(case.thread.thread_id, limit=20).rows
        if row.metadata.get("assistant_part_id") == "final"
    ]
    assert finals[-2].metadata["credential_delivery_withheld"] == _WITHHELD_DIAGNOSTICS["credential_delivery_withheld"]
    assert "credential_delivery_withheld" not in finals[-1].metadata
    assert zero_text in finals[-1].content


def test_withheld_delivery_keeps_real_sink_display_events_out_of_user_history(tmp_path: Path) -> None:
    from agent_py_agent.agent.conversation.agent_activity import BackgroundMainActivitySink
    from agent_py_agent.agent.conversation.background_delivery import (
        cached_owner_delivery,
        record_background_response,
    )
    from agent_py_agent.agent.conversation.background_history import (
        background_display_turn_from_row,
    )
    from agent_py_agent.agent.delivery import DeliveryContext

    case = _credential_delivery_case(tmp_path)
    marker = "SINK_AUDIT_LABEL_7705"
    operations = {"schema": "operation_verification.public.v1", "status": "succeeded", "operation_count": 1}

    def sink_snapshot(request: SimpleNamespace) -> dict[str, object]:
        sink = BackgroundMainActivitySink(
            case.agent, thread_id=case.thread.thread_id, task_id=request.task_id, lifetime_owner=request
        )
        sink.write_thinking(f"核对审计标签 {marker}", duration_seconds=1)
        sink.write_progress(
            {"round": 1, "call_index": 1, "tool": "web_fetch", "phase": "completed", "ok": True, "output": f"抓取 {marker}"}
        )
        sink.finish()
        snapshot = sink.display_history_snapshot()
        assert snapshot["complete"] is True and marker not in json.dumps(snapshot, ensure_ascii=False)
        return snapshot

    # 已有 operation 且无可核验绑定：无论计数如何都应扣留，这里不传计数。
    def deliver(request: SimpleNamespace, key: str, text: str):
        return record_background_response(
            case.deps,
            request,
            DeliveryContext(
                channel="feishu",
                target=case.actor.actor_id,
                thread_id=case.thread.thread_id,
                task_id=case.task_id,
                mode="reply",
                idempotency_key=f"bg-r3-3-sink-{key}",
            ),
            text,
            display_snapshot=sink_snapshot(request),
            operation_verification=operations,
            deliver=True,
            delivery_reason="task_input_resolved",
        )

    def last_final_row():
        rows = case.agent.conversation_store.messages.history_page_report(case.thread.thread_id, limit=20).rows
        return [row for row in rows if row.metadata.get("assistant_part_id") == "final"][-1]

    # 所有发布端口用同一回合的扣留回执，待决展示只在 sink 内暂存。
    withheld_request = _credential_request(case, "gwreq-sink-withheld-" + uuid.uuid4().hex[:8])
    withheld = deliver(withheld_request, "withheld", f"后台汇总 {marker}")
    assert "已安全扣留" in withheld.content
    assert case.adapter.finals and all(marker not in str(item) for item in case.adapter.finals)
    row = last_final_row()
    assert row.metadata["credential_delivery_withheld"] == _WITHHELD_DIAGNOSTICS["credential_delivery_withheld"]
    assert marker not in json.dumps({"content": row.content, "metadata": row.metadata}, ensure_ascii=False, default=str)
    replay = background_display_turn_from_row(row)
    assert replay is not None
    assert any(event["payload"].get("publication_decision") == "withheld" for event in replay["events"])
    pending = case.agent.conversation_store.wakes.pending_one(withheld_request.wake_signal["wake_signal_id"])
    frozen = cached_owner_delivery(pending) if pending is not None else None
    assert frozen is None or marker not in json.dumps(frozen, ensure_ascii=False, default=str)

    # 2. Verified (exact current-turn binding): the full display snapshot stays replayable.
    verified_turn = "gwreq-sink-verified-" + uuid.uuid4().hex[:8]
    exact_tr = TurnCredentialRedaction(
        owner_key=case.owner_key,
        conversation_root=case.conversation_root,
        thread_id=case.thread.thread_id,
        turn_key=verified_turn,
    )
    exact_tr.register_values(("ExactTurnSecret_7706",))
    try:
        verified = deliver(_credential_request(case, verified_turn), "verified", "后台汇总已核验：VERIFIED_SINK_OK")
    finally:
        exact_tr.close()
    assert verified.delivery_status == "sent" and "VERIFIED_SINK_OK" in verified.content
    verified_row = last_final_row()
    assert verified_row.metadata.get("credential_delivery_scoped") is True
    replay = background_display_turn_from_row(verified_row)
    assert replay is not None and marker in json.dumps(replay, ensure_ascii=False)

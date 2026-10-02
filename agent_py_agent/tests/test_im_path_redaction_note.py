"""对外通道（IM）出口把宿主绝对路径收成最后一段后，在整条消息末尾统一附一次说明；本机通道不受影响。

背景（C14 复核 2c，2026-10-02）：插件越界安装的提示在飞书上变成“请把插件包放到 main 下再试”，用户不知道这是被脱敏过的路径。
脱敏规则不开例外；附不附说明只看投影函数报告的“这次替换过几处路径”这个结构化事实。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_py_agent.agent.conversation.channels import (
    HOST_PATH_REDACTION_NOTE,
    project_host_paths_for_channel,
    project_host_paths_report,
    project_message_paths_for_channel,
)
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)


# 函数用途: 记录投递服务实际交给 IM 适配器的正文。
@dataclass
class _ImAdapter:
    sent: list[str] = field(default_factory=list)

    def send_message(self, target: str, message: object) -> bool:
        self.sent.append(str(getattr(message, "content", "") or ""))
        return True


# 函数用途: 经真实 DeliveryService 把一段正文投递到假 IM 通道，返回适配器收到的正文。
def _deliver(content: str) -> str:
    registry = ChannelAdapterRegistry()
    adapter = _ImAdapter()
    registry.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(text=True, reply=True, proactive=True))
    receipt = DeliveryService(registry).deliver(DeliveryContext(channel="feishu", target="ou_bench_user", mode="proactive"),
                                                ReplyEnvelope(content=content))
    assert receipt.delivery_status == "sent", receipt
    [sent] = adapter.sent
    return sent


def test_plugin_unauthorized_reply_on_im_carries_one_redaction_note(tmp_path):
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy
    from agent_py_agent.tests.test_plugin_management import manager
    from agent_py_agent.tests.test_plugin_package import _bundle

    root = tmp_path / "owners" / "main"
    root.mkdir(parents=True)
    service, _source = manager(tmp_path, workspace=root,
                               path_policy=PathAccessPolicy.from_values(mode="normal", owner_scope_root=str(root)))
    outside = tmp_path / "elsewhere" / "pkg.zip"
    outside.parent.mkdir()
    outside.write_bytes(_bundle())
    reply = service.command(f'/plugins install "{outside}"', revision=service.catalog().revision, request_id="im")
    assert str(root.resolve()) in reply["message"], "插件层仍给出完整根目录（TUI 原样可见）"
    sent = _deliver(reply["message"])
    assert "请把插件包放到 main 下再试" in sent and str(root.resolve()) not in sent, "IM 出口按原规则只留最后一段"
    assert sent.count(HOST_PATH_REDACTION_NOTE) == 1 and sent.rstrip().endswith(HOST_PATH_REDACTION_NOTE)


def test_plain_messages_without_host_paths_get_no_note():
    text = "插件已安装，默认停用。相对路径 docs/readme.md 不是宿主绝对路径。"
    assert _deliver(text) == text
    assert project_host_paths_report(text, "feishu") == (text, 0)


def test_several_paths_in_one_message_add_the_note_once_and_reprojection_is_stable():
    text = "日志在 /Users/alice/proj/logs/app.log，配置在 /Users/alice/proj/config.yaml，备份在 ~/backup/db.sql。"
    once = project_message_paths_for_channel(text, "feishu")
    assert once.count(HOST_PATH_REDACTION_NOTE) == 1
    assert "app.log" in once and "config.yaml" in once and "/Users/alice" not in once
    assert project_message_paths_for_channel(once, "feishu") == once, "再经一次出口时没有路径可替换，不会重复附说明"
    assert _deliver(once) == once, "请求历史已附过说明的正文，投递服务不再追加"


@pytest.mark.parametrize("channel", ["tui", "chat", "cli", "local"])
def test_local_private_channels_keep_full_paths_and_no_note(channel):
    text = "插件包请放到 /Users/alice/owners/main 下再试。"
    assert project_message_paths_for_channel(text, channel) == text
    assert project_host_paths_report(text, channel) == (text, 0)


def test_fragment_projection_stays_note_free():
    # 摘要片段（审批摘要、逐行进度）仍用不附说明的投影，免得一条消息里出现多次说明。
    fragment = project_host_paths_for_channel("读取 /Users/alice/proj/secret.txt", "feishu")
    assert fragment == "读取 secret.txt" and HOST_PATH_REDACTION_NOTE not in fragment

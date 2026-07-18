# LLM: create_skill 模型工具——agent 把"这次验证有效的成体系方法"提议成一个 skill。
#   严格遵守 AGENTS.md 自学习约束(项目刻意比 长期助手 更保守):
#   ① 只在 enable_self_learning=true 时可用(默认关闭=零打扰零越权);
#   ② agent 绝不直接写正式 skill 库,只产"skill 草稿"落 owner skills/.drafts/,
#      与 lesson 草稿同一"产草稿→用户审核"通道(learning_drafts 的兄弟);
#   ③ 正式 owner skills 库只由用户确认后写入，下一轮由唯一 SkillsService snapshot 发现。
#   对标 长期助手 自动创建 skill 的能力,但落点是草稿而非正式库——这是 my-agent 的
#   保守确认机制。改动时同步 tests/test_create_skill_tool.py。
# 模块用途: 让 agent 把可复用方法提议成待确认的 skill 草稿,而不是擅自改正式技能库。
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from ..agent_core.runtime.owner_roots import runtime_owner_root
from ..common.json_io import write_text_file_atomic
from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from ..user_space.owner_quota import (
    OwnerQuotaChange,
    OwnerQuotaExceeded,
    OwnerQuotaUnavailable,
    owner_quota_enforcer_from_policy,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent


def build_create_skill_spec() -> ToolSpec:
    return ToolSpec(
        name="create_skill",
        category="capability",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "把你这次任务真正验证有效、成体系且有复用价值的方法/工具链,提议沉淀成一个 skill 草稿。"
            "这是自学习:做对了一件有方法论价值的事就把方法记下来供未来检索。"
            "注意:产出的是【待用户确认的草稿】,不会立刻生效(本项目不允许 agent 直接改正式技能库);"
            "且仅在 enable_self_learning 开启时可用。"
        ),
        use_cases=[
            "完成一个有复用价值的成体系方法(某类分析/某工具链的正确用法),想提议存成 skill",
            "踩坑后总结出可复用的正确做法,提议沉淀成技能草稿等用户确认",
        ],
        avoid_when=[
            "一次性琐碎、无复用价值的操作不必创建 skill",
            "只是一句话经验(那是 lessons 的范畴,不必动用 skill 草稿)",
        ],
        keywords=["创建技能", "沉淀方法", "自学习", "create skill", "学到的方法", "skill 草稿"],
        parameters={
            "name": "必填。skill 短名(kebab-case,如 arxiv-paper-fetch)。",
            "category": "必填。类目(如 research/documents/general)。",
            "description": "必填。一句话说明这是什么方法。",
            "when_to_use": "必填。什么场景该用它。",
            "body": "必填。完整的方法/步骤/工具链正文(markdown)。",
        },
        parameter_schema={
            "name": {"type": "string"},
            "category": {"type": "string"},
            "description": {"type": "string"},
            "when_to_use": {"type": "string"},
            "body": {"type": "string"},
        },
        required_parameters=["name", "description", "body"],
        examples=[
            '{"tool":"create_skill","name":"arxiv-paper-fetch","category":"research",'
            '"description":"从 arXiv 全字段+日期倒序检索最新论文","when_to_use":"找特定主题最新论文时",'
            '"body":"# 方法\\n1. 用 site:arxiv.org + 主题词\\n2. 按日期倒序..."}',
        ],
    )


class CreateSkillTool(BaseTool):
    # 类用途: 把"提议沉淀方法成 skill"暴露成模型工具;受 enable_self_learning gate,只写草稿区。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_create_skill_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        if not _self_learning_enabled(self.agent):
            return _fail("TOOL_UNAVAILABLE", "自学习未启用",
                         "create_skill 需 enable_self_learning=true;默认关闭以遵守自学习约束(agent 不直接改正式 skill)。")
        name = _slug(params.get("name"))
        category = _slug(params.get("category")) or "general"
        description = str(params.get("description") or "").strip()
        when_to_use = str(params.get("when_to_use") or "").strip()
        body = str(params.get("body") or "").strip()
        missing = [k for k, v in (("name", name), ("description", description), ("body", body)) if not v]
        if missing:
            return _fail("TOOL_INVALID_ARGUMENTS", f"缺少必填: {', '.join(missing)}", "name/description/body 必填")
        fields = {"name": name, "description": description, "when_to_use": when_to_use, "category": category}
        try:
            draft_path = _skill_drafts_dir(self.agent) / category / name / "SKILL.md"
            rendered = _render_skill_md(fields, body)
            owner_root = runtime_owner_root(self.agent)
            quota = getattr(self.agent, "owner_quota", None) or owner_quota_enforcer_from_policy(
                owner_root
            )
            with quota.reserve([OwnerQuotaChange(draft_path, len(rendered.encode("utf-8")))]):
                write_text_file_atomic(draft_path, rendered)
            official_target = owner_root / "skills" / category / name / "SKILL.md"
        except OwnerQuotaExceeded as exc:
            return _fail("OWNER_DISK_QUOTA_EXCEEDED", str(exc), "清理当前 owner 文件或联系管理员调整配额。")
        except OwnerQuotaUnavailable:
            return _fail("OWNER_QUOTA_UNAVAILABLE", "owner 配额策略当前不可用", "配额策略恢复前拒绝写入。")
        except Exception as exc:  # noqa: BLE001 — 写草稿任何异常都返回明确可重试码,不逃逸成 UNKNOWN_ERROR(对标 remember 健壮性)
            return _fail("TOOL_EXECUTION_FAILED", f"skill 草稿写入失败: {exc}",
                         "可重试一次;持续失败则检查 category/name 是否含非法路径字符或目标目录是否可写")
        payload = {
            "ok": True, "skill": name, "category": category, "status": "draft", "draft_path": str(draft_path),
            "hint": (
                f"已存为 skill 草稿(尚未生效)。请用户审核;确认无误后把草稿移到正式库 {official_target} "
                "即可被 skill_search 检索复用。agent 不直接写正式 skill(AGENTS.md 自学习约束)。"
            ),
        }
        return ToolExecutionResult("create_skill", True, json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(code: str, error: str, hint: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        "create_skill", False, json.dumps({"error": error, "hint": hint}, ensure_ascii=False), error_code=code
    )


def _self_learning_enabled(agent: object) -> bool:
    return bool(getattr(getattr(agent, "config", None), "enable_self_learning", False))


def _skill_drafts_dir(agent: object):
    # Drafts are private owner state.  Using agent.root here used to place
    # remote-user drafts in the shared service checkout because scoped agents
    # inherit that construction root even though their effective workspace is
    # owner-local.
    return runtime_owner_root(agent) / "skills" / ".drafts"


def _slug(value: object) -> str:
    return re.sub(r"[^\w一-鿿-]+", "-", str(value or "").strip().lower()).strip("-")


def _render_skill_md(fields: dict[str, str], body: str) -> str:
    front = ["---", *[f"{key}: {value}" for key, value in fields.items()], "scope: owner", "risk_level: low", "---", ""]
    return "\n".join(front) + body.rstrip() + "\n"


__all__ = ["CreateSkillTool", "build_create_skill_spec"]

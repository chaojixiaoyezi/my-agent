# LLM: v6 决策默认只在本模块定义；正式覆盖归模型目录和线程，不读取旧 Agent/Capability/Memory 平铺值。
# 模块用途: 为完整投影与零 I/O 开关读取提供同一份严格决策默认。
from __future__ import annotations

from copy import deepcopy

from .decision_settings_schema import validate_decision_field

# 旧调用方只能取得显式离线迁移表；运行默认与开关投影均不调用它，不读取旧字段。
from .model_credentials import decision_config_fields as decision_config_fields

# 点位的单次期限与模型引用只在用户长期设置/会话临时设置里按点位覆盖（TUI 决策菜单“逐接入点设置”、user_config 的
# decision_patch），配置文件里不再各设一份：默认一律继承通用 timeout/background_timeout 与 profile_id（2026-09-27 参数减量）。
# 选模型两个点位的仓库默认单次期限下限（已定做法 10“前台默认 3 秒，选模型保持 5 秒”）：冷连接下 Jev 实测约 3.2 秒，
# 按通用 3 秒会超时、保留原模型。
SELECTION_POINT_TIMEOUT_SECONDS = 5.0
# 点位单次期限的仓库默认下限：点位没有覆盖时取 max(通用期限, 下限)，通用期限调高时跟着变长；
# 用户长期/本会话对点位的覆盖照旧优先，想低于下限就单独覆盖这个点位。
POINT_TIMEOUT_FLOOR_SECONDS = {"model_selection": SELECTION_POINT_TIMEOUT_SECONDS,
                               "subagent_model": SELECTION_POINT_TIMEOUT_SECONDS}

_DECISION_DEFAULTS = {
    "enabled": False,
    "experiment_enabled": False,
    "observe_sampling_enabled": False,
    "observe_nonblocking_enabled": False,
    "timeout_seconds": 3.0,
    "stage_timeout_seconds": 5.0,
    "background_timeout_seconds": 4.0,
    "profile_id": "",
    "points.model_selection.mode": "off",
    "points.model_selection.cadence": "every_turn",
    "points.subagent_model.mode": "off",
    "points.subagent_model.candidate_profile_ids": [],
    "points.skill_tool.mode": "off",
    "points.skill_tool.context_policy": "progressive",
    "points.skill_tool.optional_categories": ["plugins"],
    "points.pre_recall.mode": "off",
    "points.pre_recall.fragment_material": "query_text",
    "points.recall.mode": "off",
    "points.curator.mode": "off",
    "points.curator_relation.mode": "off",
    "points.external_material_order.mode": "off",
    "points.planning.mode": "off",
    "points.delivery_quality.mode": "off",
    "points.action_candidate.mode": "off",
    "points.skill_proposal_review.mode": "off",
}


# LLM: context 保留调用签名但不读取；返回独立列表副本，禁止配置对象、环境或磁盘成为第二份决策默认权威。
# 函数用途: 返回经正式 schema 验证的代码默认和准确来源，供 owner/thread 层叠加。
def decision_defaults(context: object) -> tuple[dict, dict]:
    values = {path: validate_decision_field(path, deepcopy(value)) for path, value in _DECISION_DEFAULTS.items()}
    return values, dict.fromkeys(values, "decision_defaults")

# LLM: 决策点“数量够不够、会不会太多”的资格界限只在这里定义一次：各点位的资格判定与诊断大白话
#   （decision_reach_counts 的界限标签）都在调用时读本模块属性，改一处两边同时生效，标签里的数字不会过时。
#   只放宿主规则，不是用户参数，不进 agent_config.yaml。新增或改动界限须同步用到它的点位、标签与 test_decision_reach_counts.py。
# 模块用途: 集中存放各决策点的数量界限，让“为什么没触发”的说明永远和真实规则一致。
"""Shared eligibility count limits for optional decision points."""

from __future__ import annotations

# 交付复核：本轮不同验证焦点至少、至多几个（含两端）。
DELIVERY_FOCUSES_MIN = 2
DELIVERY_FOCUSES_MAX = 12
# 可选操作建议：页面观察里至少几个候选；上限是观察格式自己的 plugin_observation.MAX_CANDIDATES。
ACTION_CANDIDATES_MIN = 2
# 阅读顺序：一次抓取至少几个网页才值得排顺序。
MATERIAL_PAGES_MIN = 2
# 待办优先级：未完成待办至少、至多几个（含两端）。
PLANNING_TODOS_MIN = 2
PLANNING_TODOS_MAX = 24
# Skill 提案审核顺序：待确认提案至少、至多几条（含两端）。上限是本地延迟与输入保护：30 条最长草稿仍在 Jev 单题窗口门内，
# 不是供应商题数上限。
SKILL_PROPOSALS_MIN = 2
SKILL_PROPOSALS_MAX = 30
# 召回重排：普通（非 HOT/lesson）记忆至少几条。
RECALL_MEMORIES_MIN = 2
# 召回重排：单条记忆正文送入决策模型的安全上限（字符）。只防超长条目拖慢请求，
# 正常长度原样送，不为一刀切省 token 破坏以后评估 apply 时的排序质量。
RECALL_CONTENT_MAX_CHARS = 800
# observe 采样：开关打开后，每个点位每个自然小时里成功调用达到这个次数就不再调用决策模型
# （失败与超时不计入）。只是内部观察节奏，不是用户参数。
OBSERVE_SAMPLED_SUCCESS_LIMIT = 6

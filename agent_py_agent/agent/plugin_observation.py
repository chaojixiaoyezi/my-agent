# LLM: 观察候选的唯一宿主合同（插件与 MCP 共用）：声明数据类（PluginToolObservation / PluginToolObservationRef 及其与 effect、
#   输入 schema 的配对规则 validate_observation_declaration）、校验只读工具结果里的 my_agent_observation 载荷形状（整份接受或整份
#   拒绝，记结构化原因码；可选几何扩展 frame / 候选 region 同样整份校验，原因码 frame / candidate_region）、用宿主上下文铸
#   observation_id / candidate_id，生成写进该次调用原归档 tool_result_envelope.observation 的记录与模型可见的有界投影（几何只进
#   归档与 tool_completed 事件，不进投影；候选 region 进规范形式参与 content_hash，frame 不进），并按 owner 权威库 runtime_events
#   里 tool_completed 事件的 seq 顺序判定"当前观察"和候选新鲜度。provider_id 只记来源（plugin:<id> / mcp:<server>），宿主逻辑不读它。
#   role/label 只是 external_data，不参与任何机器判断；不解析自由文本；不另建观察账本。改动须同步 tooling/observation_binding、
#   plugin_manifest、tooling/mcp_declarations、agent_core/tool_runtime_ledger、runtime_db.repository.events_for_agent_run 与决策线的
#   action_candidate 点。
# 模块用途: 让"这次观察看到了哪些可操作对象"成为结构化事实，供模型填候选 ID、宿主发送前复核新鲜度、决策点只在 ID 里选。
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# 插件在成功结果 structuredContent 里放候选的保留键，以及动作工具拒绝候选时的保留键
OBSERVATION_KEY = "my_agent_observation"
OBSERVATION_ERROR_KEY = "my_agent_observation_error"
OBSERVATION_SCHEMA = "plugin_observation.v1"
# 动作调用时宿主附给插件的 _meta 扩展键与版本
OBSERVATION_META_EXTENSION = "my-agent/observation"
OBSERVATION_META_VERSION = "1"
# 观察调用时宿主附给提供方的截图意愿扩展键与版本（J16 第 9 节 vision2）：适配器只认版本匹配且 enabled 为 true 才生成缩略图；
#   缺键、版本不符或形状不对一律按"不要图"处理（旧宿主从严），旧适配器不认识该键则保持原行为、由宿主兜底剥离。
OBSERVATION_SCREENSHOT_META_EXTENSION = "my-agent/observation-screenshot"
OBSERVATION_SCREENSHOT_META_VERSION = "1"
# 宿主发送前复核的结构化拒绝码
OBSERVATION_STALE = "OBSERVATION_STALE"
OBSERVATION_CANDIDATE_UNKNOWN = "OBSERVATION_CANDIDATE_UNKNOWN"
# 观察候选数量的宿主硬上限；声明的 max_candidates 不能超过它，载荷超出整份拒绝（不截断）。
MAX_CANDIDATE_COUNT = 64
# 单次观察最多报告的动作数；防止一次观察携带海量动作。
MAX_ACTION_COUNT = 8
# 观察标签的最大字符数；超长截断，防止标签撑爆载荷。
MAX_LABEL_CHARS = 120
_OBSERVATION_ID = re.compile(r"obs-[0-9a-f]{24}\Z")
_CANDIDATE_ID = re.compile(r"cand-[0-9a-f]{16}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")     # key / role：短标识，不含空白
_TARGET_TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,128}\Z")           # target.ref ≤128、generation ≤64 由长度另限
_TARGET_KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")                 # 观察目标类型：开放字符串，只校验形状
_OBSERVATION_PARAM = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")     # 动作工具接收候选 ID 的参数名（排除 "__" 宿主参数）
# 几何扩展 frame 的必填键与可选键；候选项除固定四键外只允许可选 region
_FRAME_REQUIRED_KEYS = frozenset({"space", "origin", "size", "scale"})
_FRAME_OPTIONAL_KEYS = frozenset({"captured_at", "capture", "occluded"})
_CANDIDATE_REQUIRED_KEYS = frozenset({"key", "role", "label", "actions"})


# LLM: 观察声明只允许出现在 read_only 工具上（观察本身不能有副作用）；target_kind 是开放字符串，只校验形状，用于把观察和
#   动作配对；max_candidates 由宿主再夹一次上限。声明不授予任何权限，也不改变结果的信任级别。插件 manifest v5 与 MCP
#   逐工具声明表（mcp_servers.<server>.tool_observations）共用这个类型。
# 类用途: 声明某只读工具的成功结果会带 my_agent_observation 候选载荷。
@dataclass(frozen=True)
class PluginToolObservation:
    target_kind: str
    max_candidates: int = MAX_CANDIDATE_COUNT

    # 函数用途: 拒绝形状不合规的目标类型与越界的候选上限。
    def __post_init__(self) -> None:
        if not isinstance(self.target_kind, str) or not _TARGET_KIND.fullmatch(self.target_kind):
            raise ValueError("观察目标类型无效")
        if type(self.max_candidates) is not int or not 1 <= self.max_candidates <= MAX_CANDIDATE_COUNT:
            raise ValueError("观察候选上限无效")


# LLM: param 指向动作工具输入 schema 里一个可选的 string 参数，名字由提供方自定，宿主只读这条映射；不能与 _meta 或宿主注入的
#   "__" 参数同名（形状正则已排除）。同一提供方里必须有同 target_kind 的观察工具与之配对。
# 类用途: 声明某动作工具可以接受同类观察的候选 ID。
@dataclass(frozen=True)
class PluginToolObservationRef:
    target_kind: str
    param: str

    # 函数用途: 拒绝形状不合规的目标类型与参数名。
    def __post_init__(self) -> None:
        if not isinstance(self.target_kind, str) or not _TARGET_KIND.fullmatch(self.target_kind):
            raise ValueError("观察目标类型无效")
        if not isinstance(self.param, str) or not _OBSERVATION_PARAM.fullmatch(self.param):
            raise ValueError("候选参数名无效")


# LLM: code 是稳定机器码：observation_conflict / observation_effect / observation_ref_shape / observation_ref_param；
#   它是 ValueError 子类，manifest 调用方按 ValueError 处理，MCP 声明表按 code 转成结构化拒绝原因。
# 类用途: 观察声明与工具 effect / 输入 schema 对不上时的结构化错误。
class ObservationDeclarationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# LLM: 插件 manifest 与 MCP 声明表共用的配对规则：观察只能挂只读工具；观察引用要求 param 在 schema.properties 里、type 为 string、
#   不在 required 里；两者互斥。只看结构化字段，不看工具名或说明文字。
# 函数用途: 校验一个工具的观察/观察引用声明与其 effect、输入 schema 是否一致。
def validate_observation_declaration(observation: object, observation_ref: object, *, effect: str, input_schema: object) -> None:
    if observation is not None and observation_ref is not None:
        raise ObservationDeclarationError("observation_conflict", "观察工具不能同时是动作工具")
    if observation is not None and (not isinstance(observation, PluginToolObservation) or effect != "read_only"):
        raise ObservationDeclarationError("observation_effect", "只有只读工具可以声明观察")
    if observation_ref is None:
        return
    if not isinstance(observation_ref, PluginToolObservationRef):
        raise ObservationDeclarationError("observation_ref_shape", "观察引用无效")
    properties = input_schema.get("properties") if isinstance(input_schema, dict) else None
    required = input_schema.get("required") if isinstance(input_schema, dict) else None
    spec = properties.get(observation_ref.param) if isinstance(properties, dict) else None
    if (not isinstance(spec, dict) or spec.get("type") != "string"
            or (isinstance(required, list) and observation_ref.param in required)):
        raise ObservationDeclarationError("observation_ref_param", "候选参数必须是输入 schema 里可选的 string 参数")


# LLM: reason 是稳定机器码（observation_rejected:<code> 里的 code 部分）；错误正文不含载荷内容。
# 类用途: 观察载荷形状不合规时整份拒绝的原因。
class ObservationRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(f"插件观察载荷不合规：{code}")
        self.code = code


# LLM: 身份全部来自宿主（run/task/operation/激活/注册名），不取提供方自报；provider_id 只记来源（plugin:<id> / mcp:<server>），
#   新鲜度与复核不读它；actions 映射是同一提供方内"远端动作工具名 → 宿主注册名"，只含与本观察同 target_kind 的 observation_ref 工具。
# 类用途: 铸造观察 ID 与校验候选 actions 所需的宿主上下文。
@dataclass(frozen=True)
class ObservationHostContext:
    run_id: str
    task_id: str
    operation_id: str
    activation_id: str
    provider_id: str
    tool_name: str
    target_kind: str
    max_candidates: int
    action_tools: Mapping[str, str]


# 类用途: 一个宿主铸过 ID 的候选；key 是提供方自己能解析回对象的键，宿主不解释它；region 是可选的截图像素外框 (x, y, w, h)。
@dataclass(frozen=True)
class ObservationCandidate:
    candidate_id: str
    key: str
    role: str
    label: str
    actions: tuple[str, ...]
    region: tuple[float, ...] | None = None


# LLM: to_envelope 是归档里的唯一权威形状（含提供方自己的 target_ref 与代次，动作时要原样交还提供方复核；几何 frame / 候选
#   region 只在载荷带了时出现，旧载荷字节不变）；model_projection 隐去 key / target_ref / 代次 / 几何；event_payload 是写进
#   runtime_events 的查找投影（字段与信封一致，不含 label/role，几何只带候选 region 与 frame 的 size/scale 供决策点算粗位置）。
# 类用途: 一次合规观察的完整宿主记录。
@dataclass(frozen=True)
class ObservationRecord:
    observation_id: str
    provider_id: str
    activation_id: str
    tool_name: str
    target_kind: str
    target_ref: str
    target_ref_hash: str
    generation: str
    content_hash: str
    candidates: tuple[ObservationCandidate, ...]
    run_id: str = ""
    task_id: str = ""
    operation_id: str = ""
    frame: Mapping[str, Any] | None = None

    # 函数用途: 归档 tool_result_envelope.observation 的写法。
    def to_envelope(self) -> dict[str, Any]:
        return {
            "schema": OBSERVATION_SCHEMA, "observation_id": self.observation_id, "provider_id": self.provider_id,
            "activation_id": self.activation_id, "tool": self.tool_name, "target_kind": self.target_kind,
            "target_ref": self.target_ref, "target_ref_hash": self.target_ref_hash, "generation": self.generation,
            "content_hash": self.content_hash,
            **({"frame": dict(self.frame)} if self.frame is not None else {}),
            "candidates": [{"candidate_id": c.candidate_id, "key": c.key, "role": c.role, "label": c.label,
                            "actions": list(c.actions), **_region_item(c.region)} for c in self.candidates],
        }

    # 函数用途: 模型可见结果里替换 my_agent_observation 的有界投影：只给宿主铸的 ID、role、label 与动作工具名。
    def model_projection(self) -> dict[str, Any]:
        return {"observation_id": self.observation_id,
                "candidates": [{"candidate_id": c.candidate_id, "role": c.role, "label": c.label, "actions": list(c.actions)}
                               for c in self.candidates]}

    # 函数用途: 写进 runtime_events tool_completed 载荷的查找投影（不含 label/role；几何只留候选 region 与 frame 的 size/scale）。
    def event_payload(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id, "activation_id": self.activation_id, "target_kind": self.target_kind,
            "target_ref": self.target_ref, "target_ref_hash": self.target_ref_hash, "generation": self.generation,
            "content_hash": self.content_hash, "task_id": self.task_id, "operation_id": self.operation_id,
            **_frame_item(self.frame),
            "candidates": [{"candidate_id": c.candidate_id, "key": c.key, "actions": list(c.actions), **_region_item(c.region)}
                           for c in self.candidates],
        }


# 函数用途: 候选 region 的投影片段：有几何时给 [x, y, w, h]，没有就什么都不加（旧载荷字节不变）。
def _region_item(region: object) -> dict[str, Any]:
    return {"region": list(region)} if isinstance(region, (list, tuple)) and len(region) == 4 else {}


# 函数用途: frame 的事件投影片段：只带 size 与 scale（决策点算归一化粗位置用），没有 frame 就什么都不加。
def _frame_item(frame: object) -> dict[str, Any]:
    if not isinstance(frame, Mapping) or "size" not in frame or "scale" not in frame:
        return {}
    return {"frame": {"size": list(frame["size"]), "scale": list(frame["scale"])}}


# 函数用途: 规范 JSON 后取 sha256 十六进制。
def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# LLM: 宿主对候选 key / role 的唯一形状规则；提供方（如屏幕观察核心）先用它过滤自己的候选，免得一个怪 role 让整份观察被拒。
# 函数用途: 一段文字能不能当候选的 key / role（短标识，不含空白）。
def observation_token_ok(value: object) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


# 函数用途: 校验一段短标识（key / role）。
def _token(value: object, code: str) -> str:
    if not observation_token_ok(value):
        raise ObservationRejected(code)
    return value


# LLM: 形状不合规就整份拒绝：schema、target.ref/generation、候选数量（1..min(声明, 宿主上限)）、key 重复、label 超长、
#   actions 越界（必须是同一提供方同 target_kind 的 observation_ref 工具名，1..8 个，不重复）、可选几何不合规（原因码 frame /
#   candidate_region）都算不合规。不部分采纳。候选 region 进规范形式（参与 content_hash 与 observation_id），frame 不进
#   （captured_at 每次都变）。
# 函数用途: 把提供方载荷校验成宿主观察记录，并铸 observation_id / candidate_id。
def parse_observation(payload: object, context: ObservationHostContext) -> ObservationRecord:
    if not isinstance(payload, dict) or payload.get("schema") != OBSERVATION_SCHEMA:
        raise ObservationRejected("schema")
    if set(payload) - {"schema", "target", "candidates", "frame"}:
        raise ObservationRejected("unknown_field")
    ref, generation = _parse_target(payload.get("target"))
    frame = _parse_frame(payload["frame"]) if "frame" in payload else None
    rows = payload.get("candidates")
    limit = max(1, min(int(context.max_candidates), MAX_CANDIDATE_COUNT))
    if not isinstance(rows, list) or not 1 <= len(rows) <= limit:
        raise ObservationRejected("candidate_count")
    canonical = tuple(_canonical_candidate(row, context, frame) for row in rows)
    if len({row["key"] for row in canonical}) != len(canonical):
        raise ObservationRejected("duplicate_key")
    identity = [context.run_id, context.task_id, context.operation_id, context.activation_id, context.tool_name, ref, generation, list(canonical)]
    observation_id = "obs-" + _digest(identity)[:24]
    candidates = tuple(
        ObservationCandidate("cand-" + _digest([observation_id, row["key"]])[:16], row["key"], row["role"], row["label"],
                             tuple(row["actions"]), tuple(row["region"]) if "region" in row else None)
        for row in canonical
    )
    return ObservationRecord(
        observation_id=observation_id, provider_id=context.provider_id, activation_id=context.activation_id,
        tool_name=context.tool_name, target_kind=context.target_kind, target_ref=ref, target_ref_hash=_digest(ref)[:24],
        generation=generation, content_hash=_digest(list(canonical))[:24], candidates=candidates,
        run_id=context.run_id, task_id=context.task_id, operation_id=context.operation_id, frame=frame,
    )


# 函数用途: 校验 target{ref, generation} 并返回两项。
def _parse_target(target: object) -> tuple[str, str]:
    if not isinstance(target, dict) or set(target) != {"ref", "generation"}:
        raise ObservationRejected("target")
    ref, generation = target.get("ref"), target.get("generation")
    if not isinstance(ref, str) or not _TARGET_TEXT.fullmatch(ref):
        raise ObservationRejected("target_ref")
    if not isinstance(generation, str) or not 1 <= len(generation) <= 64 or not _TARGET_TEXT.fullmatch(generation):
        raise ObservationRejected("target_generation")
    return ref, generation


# 函数用途: 判断一个值是有限数（排除 bool：Python 里 True 也算 int）。
def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# 函数用途: 校验 frame 里的二元数组（origin / size / scale）；positive 要求两项都大于 0。
def _pair(value: object, *, positive: bool) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(_finite(item) for item in value):
        raise ObservationRejected("frame")
    if positive and any(item <= 0 for item in value):
        raise ObservationRejected("frame")
    return list(value)


# LLM: 通用几何扩展，不是屏幕专项合同：space / origin / size / scale 必填（origin 可为负，多屏全局坐标可能是负的），
#   captured_at / capture / occluded 可选；多余键、非有限数、bool 冒充数字、非正尺寸或缩放都整份拒绝，原因码 frame。
# 函数用途: 校验并规范化观察载荷里的 frame。
def _parse_frame(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value) >= _FRAME_REQUIRED_KEYS or set(value) - _FRAME_REQUIRED_KEYS - _FRAME_OPTIONAL_KEYS:
        raise ObservationRejected("frame")
    frame: dict[str, Any] = {"space": _token(value["space"], "frame"), "origin": _pair(value["origin"], positive=False),
                             "size": _pair(value["size"], positive=True), "scale": _pair(value["scale"], positive=True)}
    if "captured_at" in value:
        if not _finite(value["captured_at"]) or value["captured_at"] < 0:
            raise ObservationRejected("frame")
        frame["captured_at"] = value["captured_at"]
    if "capture" in value:
        frame["capture"] = _token(value["capture"], "frame")
    if "occluded" in value:
        if not isinstance(value["occluded"], bool):
            raise ObservationRejected("frame")
        frame["occluded"] = value["occluded"]
    return frame


# LLM: region 用截图像素：x、y ≥ 0，w、h > 0，x+w ≤ size_w×scale_x，y+h ≤ size_h×scale_y；有 region 就必须有 frame。
# 函数用途: 校验一个候选的外框，返回 [x, y, w, h]。
def _parse_region(value: object, frame: dict[str, Any] | None) -> list[float]:
    if frame is None or not isinstance(value, (list, tuple)) or len(value) != 4 or not all(_finite(item) for item in value):
        raise ObservationRejected("candidate_region")
    x, y, w, h = value
    width, height = frame["size"][0] * frame["scale"][0], frame["size"][1] * frame["scale"][1]
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise ObservationRejected("candidate_region")
    return [x, y, w, h]


# 函数用途: 校验一个候选项并把 actions 换成宿主注册名（顺序保留、去重）；可选 region 校验后进规范形式。
def _canonical_candidate(row: object, context: ObservationHostContext, frame: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict) or not set(row) >= _CANDIDATE_REQUIRED_KEYS or set(row) - _CANDIDATE_REQUIRED_KEYS - {"region"}:
        raise ObservationRejected("candidate_shape")
    key = _token(row["key"], "candidate_key")
    role = _token(row["role"], "candidate_role")
    label = row["label"]
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS or any(ord(ch) < 32 for ch in label):
        raise ObservationRejected("candidate_label")
    actions = row["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTION_COUNT or len(set(actions)) != len(actions):
        raise ObservationRejected("candidate_actions")
    mapped = []
    for action in actions:
        if not isinstance(action, str) or action not in context.action_tools:
            raise ObservationRejected("action_not_declared")
        mapped.append(context.action_tools[action])
    canonical: dict[str, Any] = {"key": key, "role": role, "label": label, "actions": mapped}
    if "region" in row:
        canonical["region"] = _parse_region(row["region"], frame)
    return canonical


# LLM: 只读 runtime_events：先按既有 run_id 找权威 AgentRun，再取该 agent_run 全部 tool_completed 事件载荷（跨 attempt 共享，
#   按事件序）；任何库错误按"没有事件"处理，不抛给调用方。观察与动作事实的读取都从这里取载荷。
# 函数用途: 列出一个 run 里按发生顺序排列的工具完成事件载荷。
def _completed_payloads(repo: object, run_id: str) -> list[dict[str, Any]]:
    if repo is None or not run_id:
        return []
    try:
        agent_run = repo.agent_run_for_run_id(run_id)
        if agent_run is None:
            return []
        events = repo.events_for_agent_run(str(agent_run["agent_run_id"]), event_type="tool_completed")
    except (sqlite3.Error, OSError, AttributeError, KeyError, TypeError):
        return []
    return [event["payload"] for event in events if isinstance(event, dict) and isinstance(event.get("payload"), dict)]


# LLM: 只保留 ok 且带 observation 的事件，按 task 过滤（task_id 空表示不过滤）。
# 函数用途: 列出一个 run 里按发生顺序排列的观察事件载荷。
def _observation_events(repo: object, *, run_id: str, task_id: str) -> list[dict[str, Any]]:
    rows = []
    for payload in _completed_payloads(repo, run_id):
        observation = payload.get("observation")
        if not isinstance(observation, dict) or payload.get("ok") is not True:
            continue
        if task_id and str(observation.get("task_id") or "") != task_id:
            continue
        rows.append(observation)
    return rows


# LLM: 动作事实不看 ok：只要动作工具按候选真的发送过（复核通过），不论提供方成功、失败或按代次拒绝，都算碰过这个观察；
#   宿主自动执行与模型自己的动作共用这条事实。按 task 过滤（task_id 空表示不过滤）。
# 函数用途: 列出一个 run 里按发生顺序排列的动作事实（observation_id / candidate_id / tool / actor）。
def observation_actions(repo: object, *, run_id: str, task_id: str) -> list[dict[str, Any]]:
    rows = []
    for payload in _completed_payloads(repo, run_id):
        action = payload.get("observation_action")
        if not isinstance(action, dict):
            continue
        if task_id and str(action.get("task_id") or "") != task_id:
            continue
        rows.append({**action, "actor": str(payload.get("actor") or "model")})
    return rows


# LLM: 同一 run/task、同一 activation_id 与 target_ref_hash 下最新一次成功观察为 current，更早的都 stale；顺序取事件 seq。
# 函数用途: 取当前观察的事件载荷，没有则 None。
def current_observation(repo: object, *, run_id: str, task_id: str, activation_id: str, target_ref_hash: str) -> dict[str, Any] | None:
    current = None
    for observation in _observation_events(repo, run_id=run_id, task_id=task_id):
        if observation.get("activation_id") == activation_id and observation.get("target_ref_hash") == target_ref_hash:
            current = observation
    return current


# LLM: True 只在该 observation_id 存在且仍是其 (activation_id, target_ref_hash) 下最新一次成功观察时成立；查不到或已被更新的观察
#   取代都是 False，不猜。
# 函数用途: 决策点与发送前复核共用的新鲜度判定。
def observation_is_current(repo: object, *, run_id: str, task_id: str, observation_id: str) -> bool:
    if not isinstance(observation_id, str) or not _OBSERVATION_ID.fullmatch(observation_id):
        return False
    events = _observation_events(repo, run_id=run_id, task_id=task_id)
    match = next((row for row in events if row.get("observation_id") == observation_id), None)
    if match is None:
        return False
    latest = current_observation(repo, run_id=run_id, task_id=task_id, activation_id=str(match.get("activation_id") or ""),
                                 target_ref_hash=str(match.get("target_ref_hash") or ""))
    return latest is not None and latest.get("observation_id") == observation_id


# LLM: 发送前复核：候选 ID 形状、在当前 run/task 的观察事件里存在、所属观察仍 current、候选 actions 含本动作工具（注册名）。
#   任一不成立抛 ObservationRejected(OBSERVATION_STALE | OBSERVATION_CANDIDATE_UNKNOWN)；通过返回 (观察载荷, 候选)。
# 函数用途: 把模型填的 candidate_id 换回插件的 key 与目标代次，供 _meta 回填。
def resolve_action_candidate(repo: object, *, run_id: str, task_id: str, candidate_id: str, action_tool: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id):
        raise ObservationRejected(OBSERVATION_CANDIDATE_UNKNOWN)
    for observation in reversed(_observation_events(repo, run_id=run_id, task_id=task_id)):
        candidate = next((c for c in observation.get("candidates", ()) if isinstance(c, dict) and c.get("candidate_id") == candidate_id), None)
        if candidate is None:
            continue
        if not observation_is_current(repo, run_id=run_id, task_id=task_id, observation_id=str(observation.get("observation_id") or "")):
            raise ObservationRejected(OBSERVATION_STALE)
        if action_tool not in (candidate.get("actions") or ()):
            raise ObservationRejected(OBSERVATION_CANDIDATE_UNKNOWN)
        return observation, candidate
    raise ObservationRejected(OBSERVATION_CANDIDATE_UNKNOWN)


# 函数用途: 动作调用时附给插件的 _meta 载荷：观察 ID、插件自己的 key 与目标引用/代次，让插件按代次复核后再执行。
def observation_meta(observation: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {"version": OBSERVATION_META_VERSION, "observation_id": observation.get("observation_id"), "key": candidate.get("key"),
            "target": {"ref": observation.get("target_ref"), "generation": observation.get("generation")}}


# LLM: 只读归档信封里 ObservationBinding 写过的 observation_action（observation_id / candidate_id / tool 都是字符串），
#   形状不对返回 None；供 tool_completed 事件写载荷时复用同一投影，任务编号由调用方按归档给。
# 函数用途: 从归档 tool_result_envelope.observation_action 还原事件载荷投影。
def observation_action_payload_from_envelope(value: object, *, task_id: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    fields = {key: value.get(key) for key in ("observation_id", "candidate_id", "tool")}
    if any(not isinstance(item, str) or not item for item in fields.values()):
        return None
    return {**fields, "task_id": task_id}


# LLM: 只读归档信封里宿主写过的 observation，形状不对返回 None；供 tool_completed 事件写载荷时复用同一投影。
# 函数用途: 从归档 tool_result_envelope.observation 还原事件载荷投影。
def observation_event_payload_from_envelope(envelope: object, *, task_id: str, operation_id: str) -> dict[str, Any] | None:
    if not isinstance(envelope, dict) or envelope.get("schema") != OBSERVATION_SCHEMA:
        return None
    candidates = envelope.get("candidates")
    if not isinstance(candidates, list):
        return None
    return {
        "observation_id": envelope.get("observation_id"), "activation_id": envelope.get("activation_id"),
        "target_kind": envelope.get("target_kind"), "target_ref": envelope.get("target_ref", ""),
        "target_ref_hash": envelope.get("target_ref_hash"), "generation": envelope.get("generation"),
        "content_hash": envelope.get("content_hash"), "task_id": task_id, "operation_id": operation_id,
        **_frame_item(envelope.get("frame")),
        "candidates": [{"candidate_id": c.get("candidate_id"), "key": c.get("key"), "actions": list(c.get("actions") or []),
                        **_region_item(c.get("region"))} for c in candidates if isinstance(c, dict)],
    }


__all__ = [
    "MAX_ACTION_COUNT", "MAX_CANDIDATE_COUNT", "MAX_LABEL_CHARS", "OBSERVATION_CANDIDATE_UNKNOWN", "OBSERVATION_ERROR_KEY",
    "OBSERVATION_KEY", "OBSERVATION_META_EXTENSION", "OBSERVATION_META_VERSION", "OBSERVATION_SCHEMA",
    "OBSERVATION_SCREENSHOT_META_EXTENSION", "OBSERVATION_SCREENSHOT_META_VERSION", "OBSERVATION_STALE",
    "ObservationCandidate", "ObservationDeclarationError", "ObservationHostContext", "ObservationRecord", "ObservationRejected",
    "PluginToolObservation", "PluginToolObservationRef", "current_observation", "observation_action_payload_from_envelope",
    "observation_actions", "observation_event_payload_from_envelope", "observation_is_current", "observation_meta",
    "observation_token_ok", "parse_observation", "resolve_action_candidate", "validate_observation_declaration",
]

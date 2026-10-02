# LLM: 智能程度自动检测的唯一入口（宿主侧）。用已保存的凭据和正式 HTTP 后端，对当前会话模型按 low / max / 不带字段
#   各发 PROBE_ROUND_COUNT 次同一道短题，只把 usage 的 token 字段交给 reasoning_probe_judge 判定；凭据只在后端内部使用，
#   不进入模型上下文、工具参数、检测记录或回执，回复正文直接丢弃。检测在后台线程运行（控制命令必须很快返回），结果按用户、
#   按模型档案记在模型档案旁的 .reasoning-probes.json（模型指纹变了即失效）。确认支持时经参数中心写 reasoning_control: effort：
#   私有档案写本人档案（set_profile_field，可 /effort revert 撤销）；部署默认模型只在管理员触发时写全局配置
#   （set_parameter，可 /settings revert 撤销）；共享模型只记结果。开关 reasoning_control_auto_probe 只管 /effort 设档位时的
#   自动检测，/effort probe 手动检测不受它限制。改动须同步 test_reasoning_probe.py、gateway_parts/control_service 的 /effort
#   处理与 docs/design/REASONING_EFFORT.md 第 8 节。
# 模块用途: 检测当前模型是否真的支持按推理强度档位调节思考，记录结果并把确认的控制方式写进模型档案。
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from ..backends import get_backend
from ..backends.base import ProviderRequestOptions
from ..backends.provider_headers import provider_runtime_scope
from ..backends.reasoning_control import (
    CONTROL_LABELS,
    normalize_reasoning_control,
    normalize_reasoning_level,
    resolved_reasoning_control,
)
from ..common.json_io import locked_json_path, read_json_object, write_json_file_atomic_unlocked
from ..conversation.host_notices import clear_host_notices, host_notice, queue_host_notice
from .model_profiles import model_profiles_path, selected_model_config
from .parameter_changes import (
    ChangeOrigin,
    ProfileFieldChange,
    WritePaths,
    revert_profile_change,
    set_parameter,
    set_profile_field,
)
from .reasoning_probe_judge import (
    MEASURE_REASONING,
    PROBE_LEVELS,
    PROBE_ROUND_COUNT,
    VERDICT_INCONCLUSIVE,
    VERDICT_SUPPORTED,
    ProbeSample,
    ProbeVerdict,
    judge_reasoning_samples,
    output_token_count,
    reasoning_token_count,
)
from .shared_model_catalog import shared_profile_key
from .thread_model_selection import thread_model_profile_id
from .user_config_capability import user_config_path

# 与 2026-09-26 实测同一道题（答案 64），需要几步推理；只要最后的数字，答案部分很短，token 差别主要来自思考。
PROBE_PROMPT = "请计算：1 到 2000 中，既是 3 的倍数、各位数字之和又能被 7 整除的整数有多少个？最后只写出这个数字。"
_RECORDS_SCHEMA = "reasoning_probe_records.v1"
# 单次请求的等待上限（max 档可能想很久）；running 记录超过这个时长仍未结束视为已中断（例如 Gateway 重启）。
_REQUEST_TIMEOUT_SECONDS = 180
# 推理探测 running 记录 3600 秒未结束视为已中断：防 Gateway 重启后陈旧记录卡死。
_STALE_RUNNING_SECONDS = 3600
_ACTOR = "reasoning_probe"
# 检测结论作为宿主提示的来源名：同一会话只留最新一条，/effort 看过就清掉。
_NOTICE_SOURCE = "reasoning_probe"
_TOTAL_REQUESTS = PROBE_ROUND_COUNT * len(PROBE_LEVELS)
# 进程内登记：正在跑的检测（防同一档案重复检测）与各检测的开始时间。
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()
_STARTED: dict[str, float] = {}


# LLM: profile_id 是会话选定的模型编号（"default"、私有 UUID 或 "shared:<UUID>"），config 是按它解析出的运行配置；
#   thread_id 是发起检测（或查看结论）的会话，检测结论作为宿主提示送回这个会话。
# 类用途: 一次检测针对的模型档案。
@dataclass(frozen=True)
class ProbeTarget:
    profile_id: str
    config: object
    thread_id: str = ""


# LLM: admin 在触发时按已认证 owner 身份算好（后台线程里不再判断）；key 是进程内防重复检测的登记键。
# 类用途: 一次后台检测任务携带的全部上下文。
@dataclass(frozen=True)
class _ProbeJob:
    agent: object
    target: ProbeTarget
    trigger: str
    admin: bool
    key: str


# LLM: 位置由可信 home 身份决定（与 model_profiles_path 同一摘要），每个用户一份，不接受调用方指定。
# 函数用途: 检测记录文件的位置：该用户模型档案文件旁的 .reasoning-probes.json。
def probe_records_path(home_paths: object) -> Path:
    return model_profiles_path(home_paths).with_suffix(".reasoning-probes.json")


# LLM: 只由协议、接口地址和模型名决定；换了其中任何一个，旧检测结果就不再适用。不含密钥。
# 函数用途: 计算模型指纹。
def model_fingerprint(config: object) -> str:
    identity = [str(getattr(config, key, "") or "") for key in ("model_backend", "api_base", "model_name")]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()[:16]


# 函数用途: 档案里声明的思考控制方式（未声明为 auto）。
def _declared(config: object) -> str:
    return normalize_reasoning_control(getattr(config, "model_reasoning_control", "auto")) or "auto"


# 函数用途: 按声明、接口地址和协议解析出的实际控制方式。
def _resolved(config: object, declared: str) -> str:
    return resolved_reasoning_control(declared, getattr(config, "api_base", ""), getattr(config, "model_backend", ""))


# LLM: 只有“声明为 effort 时真的会按档位发字段”的协议才值得检测（OpenAI / Anthropic 兼容）；Responses 等协议写了也不生效。
# 函数用途: 判断当前模型能不能做这项检测。
def _probeable(config: object) -> bool:
    return _resolved(config, "effort") == "effort"


# LLM: 会话还没有可用模型、模型已删除、共享被撤销或档案暂不可读时都返回 None（与 /effort 回执的“模型暂不可解析”同一宽容度），
#   由调用方说明原因，不能让档位设置的回执整体失败；不发网络请求。
# 函数用途: 取出会话当前选定的模型档案。
def _thread_target(agent: object, thread_id: str) -> ProbeTarget | None:
    try:
        profile_id = thread_model_profile_id(agent, thread_id)
        return ProbeTarget(profile_id, selected_model_config(agent, profile_id=profile_id), thread_id)
    except Exception:  # noqa: BLE001 - 模型解析失败只是不检测，档位设置照常回执。
        return None


# LLM: 文件缺失、损坏或结构版本不对都当作没有记录（这只是检测结果缓存，不是配置），下一次写入会重建。只读。
# 函数用途: 读取当前用户的全部检测记录。
def _load_records(path: Path) -> dict[str, dict]:
    try:
        data = read_json_object(path) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    profiles = data.get("profiles") if data.get("schema") == _RECORDS_SCHEMA else None
    return {key: row for key, row in (profiles or {}).items() if isinstance(row, dict)}


# LLM: 锁内读改写，只替换这个档案的记录。副作用：写检测记录文件。
# 函数用途: 保存一个模型档案的检测记录。
def _save_record(agent: object, profile_id: str, record: dict) -> None:
    path = probe_records_path(agent.home_paths)
    with locked_json_path(path):
        profiles = {**_load_records(path), profile_id: record}
        write_json_file_atomic_unlocked(path, {"schema": _RECORDS_SCHEMA, "profiles": profiles})


# LLM: 指纹不符（模型换了）或 running 已超时（进程中断）都视为没有记录，允许重新检测。只读。
# 函数用途: 取出当前模型档案仍然有效的检测记录。
def current_record(agent: object, target: ProbeTarget) -> dict | None:
    record = _load_records(probe_records_path(agent.home_paths)).get(target.profile_id)
    if not record or record.get("fingerprint") != model_fingerprint(target.config):
        return None
    stale = record.get("status") == "running" and time.time() - float(record.get("started_at") or 0) > _STALE_RUNNING_SECONDS
    return None if stale else record


# LLM: /effort 的唯一挂点：set 可能触发自动检测；probe 手动检测；revert 撤销检测写入的档案修改；其余只展示已有记录。
#   返回追加在 /effort 回执后面的中文行，失败也只返回说明，不抛异常打断回执。
# 函数用途: 生成 /effort 回执里与智能程度检测有关的几行。
def effort_probe_lines(agent: object, thread_id: str, command: object) -> list[str]:
    operation = str(getattr(command, "operation", "") or "")
    if operation == "revert":
        return [_revert_text(agent, str(getattr(command, "value", "") or ""))]
    target = _thread_target(agent, thread_id)
    if target is None:
        return ["当前会话还没有可用的模型，无法检测智能程度支持情况。"] if operation == "probe" else []
    if operation == "probe":
        return [_manual_start(agent, target)]
    note = _auto_start(agent, target, getattr(command, "value", "")) if operation == "set" else ""
    return [note] if note else _record_lines(agent, target)


# LLM: 用户把档位设成 auto 以外的值、开关开着、档案没有显式声明控制方式（auto）且解析为 none、协议可检测、还没有有效记录，
#   五条都满足才自动检测一次；显式声明过 none 的档案尊重用户声明，不自动检测。
# 函数用途: 按条件自动开始一次后台检测，返回回执说明（不检测时返回空串）。
def _auto_start(agent: object, target: ProbeTarget, level: object) -> str:
    wanted = normalize_reasoning_level(level) not in {"", "auto"}
    enabled = bool(getattr(getattr(agent, "config", None), "reasoning_control_auto_probe", True))
    declared = _declared(target.config)
    unknown = declared == "auto" and _resolved(target.config, declared) == "none" and _probeable(target.config)
    if not (wanted and enabled and unknown) or current_record(agent, target) is not None:
        return ""
    return _start(agent, target, "auto")


# 函数用途: 用户用 /effort probe 手动检测：协议不支持时说明原因，否则开始一次后台检测（已有结果也重新检测）。
def _manual_start(agent: object, target: ProbeTarget) -> str:
    if not _probeable(target.config):
        return "当前模型的接口协议暂不支持按档位发送推理参数，无法检测。"
    return _start(agent, target, "manual")


# LLM: 同一用户同一档案同时只跑一个检测（进程内登记）；先写 running 记录再起线程，写失败就撤销登记。
#   管理员身份在这里按已认证 owner 算好传给后台。副作用：写检测记录、清掉本会话旧结论的待送达提示、启动后台线程。
# 函数用途: 登记并启动一次后台检测，返回给用户的说明。
def _start(agent: object, target: ProbeTarget, trigger: str) -> str:
    from ..user_space.approval_mode import is_permission_admin

    key = f"{probe_records_path(agent.home_paths)}::{target.profile_id}"
    with _RUNNING_LOCK:
        if key in _RUNNING:
            return "当前模型的智能程度检测正在进行中，完成后发 /effort 查看结果。"
        _RUNNING.add(key)
    job = _ProbeJob(agent, target, trigger, is_permission_admin(getattr(agent, "home_paths", None)), key)
    try:
        _save_record(agent, target.profile_id, _record_base(job, status="running"))
    except OSError:
        _release(key)
        return "检测记录暂时无法保存，这次没有开始检测，请稍后重试。"
    clear_host_notices(getattr(agent, "conversation_store", None), target.thread_id, _NOTICE_SOURCE)  # 旧结论已作废
    _spawn(lambda: _run(job))
    head = "当前模型还没检测过是否支持调节智能程度，已" if trigger == "auto" else "已"
    return (f"{head}在后台开始检测：同一道短题按“低”“最高”和不带参数各发 {PROBE_ROUND_COUNT} 次，共 {_TOTAL_REQUESTS} 次请求，"
            "约需 1～5 分钟，会额外消耗少量 token；完成后发 /effort 查看结果。")


# 函数用途: 起一个后台守护线程执行检测（测试里替换成同步执行）。
def _spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="reasoning-probe", daemon=True).start()


# LLM: 检测登记与开始时间一起清掉；写 running 记录失败和检测结束两条路径都走这里，下次检测重新计时。
# 函数用途: 撤销进程内的检测登记。
def _release(key: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING.discard(key)
        _STARTED.pop(key, None)


# 函数用途: 生成一条检测记录的公共字段（指纹、模型名、触发方式、开始时间与进度）。
def _record_base(job: _ProbeJob, *, status: str, completed: int = 0) -> dict:
    return {"fingerprint": model_fingerprint(job.target.config), "model_name": str(getattr(job.target.config, "model_name", "")),
            "trigger": job.trigger, "status": status, "started_at": _job_started(job), "completed_requests": completed,
            "total_requests": _TOTAL_REQUESTS}


# 函数用途: 任务开始时间：同一任务第一次写记录时取当前时间，之后沿用（进度更新不改开始时间）。
def _job_started(job: _ProbeJob) -> float:
    return _STARTED.setdefault(job.key, time.time())


# LLM: 后台线程主体：发请求、判定、确认支持时写档案，最后一定写 done 记录并撤销登记；任何异常都只记成“无法判定”，
#   不能拖垮 Gateway。副作用：网络请求、写检测记录、可能写模型档案或全局配置及账本。
# 函数用途: 执行一次完整检测。
def _run(job: _ProbeJob) -> None:
    record = _record_base(job, status="done", completed=_TOTAL_REQUESTS)
    try:
        samples = _collect_samples(job)
        verdict = judge_reasoning_samples(samples)
        record.update(_verdict_fields(verdict), samples=[asdict(sample) for sample in samples])
        if verdict.verdict == VERDICT_SUPPORTED:
            record["applied"] = _apply_supported(job)
    except Exception as exc:  # noqa: BLE001 - 后台检测失败只记结论，不能影响 Gateway 其它工作。
        record.update(verdict=VERDICT_INCONCLUSIVE, reason="probe_failed", error_type=type(exc).__name__)
    finally:
        _finish(job, record)


# LLM: 写最终记录、撤销登记（写失败也撤销，允许之后重新检测），再把结论作为宿主提示送回发起检测的会话
#   （记录写失败也送，结论本身仍然成立）。副作用：写检测记录、改写会话线程记录。
# 函数用途: 结束一次检测：保存结论并通知发起会话。
def _finish(job: _ProbeJob, record: dict) -> None:
    final = {**record, "finished_at": time.time()}
    try:
        _save_record(job.agent, job.target.profile_id, final)
    except OSError:
        pass
    finally:
        _release(job.key)
    _queue_verdict_notice(job, final)


# LLM: 提示正文只由结构化结论生成（与 /effort 查看同一套文案），带模型名；同一会话只留最新一条（queue_host_notice 按来源替换）。
#   写不进去只是少一行提示，不影响检测结果。副作用：改写会话线程记录。
# 函数用途: 把检测结论排进发起会话的待送达宿主提示。
def _queue_verdict_notice(job: _ProbeJob, record: dict) -> None:
    applied = _applied_text(record.get("applied")) if isinstance(record.get("applied"), dict) else ""
    text = f"模型 {record.get('model_name') or ''}：{_verdict_text(record)}{applied}"
    notice = host_notice(_NOTICE_SOURCE, str(record.get("reason") or ""), text)
    queue_host_notice(getattr(job.agent, "conversation_store", None), job.target.thread_id, notice)


# 函数用途: 把判定结论转成记录字段。
def _verdict_fields(verdict: ProbeVerdict) -> dict:
    return {key: value for key, value in asdict(verdict).items() if value is not None}


# LLM: 后端按档案配置构建，只把控制方式临时当作 effort（这样 low/max 才会真的发字段）并放宽单次等待；不改档案。
#   三组按轮交替发送，每完成一次更新进度。会话头按独立的检测身份生成，不借用任何会话。副作用：网络请求、写进度。
# 函数用途: 发出全部检测请求并收集样本。
def _collect_samples(job: _ProbeJob) -> list[ProbeSample]:
    config = replace(job.target.config, model_reasoning_control="effort", request_timeout=_REQUEST_TIMEOUT_SECONDS)
    backend = get_backend(config.model_backend, config)
    plan = [level for _round in range(PROBE_ROUND_COUNT) for level in PROBE_LEVELS]
    samples: list[ProbeSample] = []
    with provider_runtime_scope(job.agent, SimpleNamespace(thread_id="reasoning-probe:" + job.target.profile_id)):
        for level in plan:
            samples.append(_one_sample(backend, level))
            _save_record(job.agent, job.target.profile_id, _record_base(job, status="running", completed=len(samples)))
    return samples


# LLM: 只保留档位、token 数、HTTP 状态和异常类型；回复正文与异常消息都不保存（可能回显密钥或请求头）。
# 函数用途: 发一次检测请求并得到一个样本。
def _one_sample(backend: object, level: str) -> ProbeSample:
    try:
        response = backend.generate(PROBE_PROMPT, request_options=ProviderRequestOptions(reasoning_effort=level))
    except Exception as exc:  # noqa: BLE001 - 单次失败只记成失败样本，由判定决定结论。
        return ProbeSample(level, status_code=_status_code(exc), error_type=type(exc).__name__ or "Error")
    usage = getattr(response, "usage", None)
    return ProbeSample(level, reasoning_token_count(usage), output_token_count(usage))


# 函数用途: 取出异常里的 HTTP 状态码，取不到或不是整数时为 0。
def _status_code(exc: Exception) -> int:
    value = getattr(exc, "status_code", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# LLM: 档案已经按档位发送（声明为 effort，或未声明但已知表解析为 effort）就不再写；私有档案写本人档案（下一轮生效），
#   部署默认模型只在管理员触发时写全局配置（重启生效），共享模型不写。回执只留编号、动作、结果码与说明，不留路径。
#   副作用：可能写模型档案或全局配置与对应账本。
# 函数用途: 确认支持后把 reasoning_control: effort 写进这个模型档案，返回写入结果摘要。
def _apply_supported(job: _ProbeJob) -> dict:
    target = job.target
    origin = ChangeOrigin(_ACTOR, "自动检测确认该模型按推理强度档位调节思考")
    if _resolved(target.config, _declared(target.config)) == "effort":
        return {"ok": True, "kind": "unchanged"}
    if target.profile_id == "default":
        if not job.admin:
            return {"ok": False, "kind": "config", "code": "NEEDS_ADMIN"}
        report = set_parameter("model_reasoning_control", "effort",
                               paths=WritePaths(user_path=user_config_path(job.agent.config)), origin=origin)
        return _applied_summary(report, "config")
    if shared_profile_key(target.profile_id):
        return {"ok": False, "kind": "shared", "code": "SHARED_PROFILE"}
    change = ProfileFieldChange(target.profile_id, "reasoning_control", "effort")
    return _applied_summary(set_profile_field(job.agent, change, origin=origin), "profile")


# 函数用途: 从参数中心回执里取出要记下的字段（不含路径）。
def _applied_summary(report: dict, kind: str) -> dict:
    return {"kind": kind, **{key: report[key] for key in ("ok", "code", "error", "change_id") if key in report}}


# LLM: 展示已有结论时顺带清掉本会话同来源的待送达提示（用户已经在这里看到了）；进行中只显示进度。副作用：改写会话线程记录。
# 函数用途: 按记录生成 /effort 里展示的检测结果行；没有记录时不显示。
def _record_lines(agent: object, target: ProbeTarget) -> list[str]:
    record = current_record(agent, target)
    if record is None:
        return []
    if record.get("status") == "running":
        return [f"智能程度检测进行中：已完成 {record.get('completed_requests', 0)}/{_TOTAL_REQUESTS} 次请求，完成后再发 /effort 查看。"]
    clear_host_notices(getattr(agent, "conversation_store", None), target.thread_id, _NOTICE_SOURCE)  # 这里已看到结论
    lines = [_verdict_text(record)]
    applied = _applied_text(record.get("applied")) if isinstance(record.get("applied"), dict) else ""
    return lines + ([applied] if applied else [])


# LLM: 文案只由结构化结论（verdict / reason / measure / 中位数）生成，不含请求正文或服务商原话。
# 函数用途: 把检测结论写成一句大白话。
def _verdict_text(record: dict) -> str:
    when = time.strftime("%m-%d %H:%M", time.localtime(float(record.get("finished_at") or record.get("started_at") or 0)))
    unit = "推理 token" if record.get("measure") == MEASURE_REASONING else "输出 token"
    note = "" if record.get("measure") == MEASURE_REASONING else "（该接口不单独报告推理 token，按输出 token 判定）"
    low, high, default = (record.get(key) for key in ("low_median", "max_median", "default_median"))
    texts = {
        "max_above_low": f"支持按档位调节——{unit} 中位数：“最高”档 {high}，“低”档 {low}，不带参数 {default}{note}。",
        "no_difference": (f"不支持调节——“低”和“最高”两档的{unit} 中位数分别是 {low} 和 {high}，没有明显差别{note}；"
                          "/effort 设置不会改变请求。"),
        "no_reasoning": "不支持调节——这个模型不产生推理 token；/effort 设置不会改变请求。",
        "field_rejected": "不支持调节——服务商拒绝了推理强度参数（带参数的请求都返回参数错误）；/effort 设置不会改变请求。",
        "incomplete": "没有得出结论——有请求失败，样本不够；可以用 /effort probe 重新检测。",
        "no_usage": "没有得出结论——服务商返回的用量里没有 token 统计；可以用 /effort probe 重新检测。",
    }
    reason = str(record.get("reason") or "")
    detail = texts.get(reason, f"没有得出结论——检测过程出错（{record.get('error_type') or reason}）；可以用 /effort probe 重新检测。")
    return f"智能程度检测（{when}）：{detail}"


# LLM: 编号只显示前 8 位（撤销时至少输入 6 位即可）；部署默认模型写的是全局配置，要重启 Gateway 才生效，如实说明。
# 函数用途: 把写入结果写成一句大白话。
def _applied_text(applied: dict) -> str:
    short = str(applied.get("change_id") or "")[:8]
    if applied.get("kind") == "unchanged":
        return "档案里已经是“按推理强度档位发送”，无需修改。"
    if applied.get("code") == "NEEDS_ADMIN":
        return "这是部署默认模型，全局配置只有管理员能改；请管理员执行 /settings set model_reasoning_control effort。"
    if applied.get("code") == "SHARED_PROFILE":
        return "这是管理员共享的模型，本次结果只做记录；需要管理员在自己的会话里对原模型执行 /effort probe 写入。"
    if not applied.get("ok"):
        return f"没能写入档案：{applied.get('error') or applied.get('code') or '未知原因'}"
    if applied.get("kind") == "config":
        return (f"已把全局配置 model_reasoning_control 改为 effort，重启 Gateway 后生效"
                f"（修改编号 {short}，撤销：/settings revert {short}）。")
    return f"已把这个模型的思考控制改为“按推理强度档位发送”，下一轮对话起生效（修改编号 {short}，撤销：/effort revert {short}）。"


# LLM: 只能撤销本人档案账本里的记录（revert_profile_change 按可信 home 定位账本），编号至少 6 位。副作用：改写模型档案与账本。
# 函数用途: 处理 /effort revert <编号>，返回一句说明。
def _revert_text(agent: object, change_id: str) -> str:
    report = revert_profile_change(agent, change_id, origin=ChangeOrigin("chat", "撤销智能程度检测写入的档案修改"))
    if not report.get("ok"):
        return f"撤销没有成功：{report.get('error')}"
    label = CONTROL_LABELS.get(str(report.get("saved") or ""), "自动")
    return f"已撤销：模型 {report.get('model_name')} 的思考控制恢复为“{label}”，{report.get('effect_text')}"


__all__ = [
    "PROBE_PROMPT",
    "ProbeTarget",
    "current_record",
    "effort_probe_lines",
    "model_fingerprint",
    "probe_records_path",
]

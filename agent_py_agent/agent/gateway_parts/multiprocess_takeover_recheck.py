# LLM: 此模块只给受信持锁C状态转换，不读取文件、不探测、不停止进程、不解除实际隔离。
# 模块用途: 定义隔离复核退避和新鲜回执的纯决策入口，接线由组合根负责。
import hashlib
import json
import math
import secrets

from .multiprocess_records import MultiprocessRecordError, validate_record

_receipt_origin = object()


# LLM: 私有来源只区分本进程verify调用与可序列化证据，不抵御受信Python任意代码或授予C锁权。
# 类用途: 保持原三字段JSON形状，另存不能从磁盘恢复的新鲜性印记。
class _VerificationReceipt(dict):
    pass


# LLM: 只有verify核实后调用；印记绑定原对象身份不写JSON，浅/深拷贝及内容改变都失效。
# 函数用途: 为本进程核实结果附开始/结束单调时刻和唯一调用身份。
def _verified_result(payload, started, finished):
    result = _VerificationReceipt(payload)
    result._seal = (_receipt_origin, started, finished, secrets.token_hex(16), _digest(payload), id(result))
    return result


# LLM: JSON是范围和内容绑定的唯一规范化方式，非法数值不能进入摘要。
# 函数用途: 计算稳定内容哈希，不读取文件。
def _digest(value):
    content = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()


# LLM: 单调时间由调用方提供，拒绝bool/NaN/无穷避免期限比较失效。
# 函数用途: 校验纯状态机所用的时间数值。
def _checked_time(value):
    try:
        valid = type(value) in {int, float} and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("quarantine_time_invalid")
    return value


# LLM: 协议退避不是用户设置，输入是已失败复核次数，不产生时钟或I/O副作用。
# 函数用途: 返回有上限的下一次复核间隔。
def quarantine_backoff(attempt):
    if type(attempt) is not int or attempt < 0:
        raise ValueError("quarantine_attempt_invalid")
    return 60 if attempt >= 6 else 2 ** attempt


# LLM: 状态绑定当前C和旧登记范围，实际持锁权仍由调用方验证。
# 函数用途: 创建首次隔离复核状态。
def new_quarantine_state(current_c_run_id, scope, now):
    now = _checked_time(now)
    if type(current_c_run_id) is not str or not current_c_run_id.strip():
        raise ValueError("quarantine_current_c_invalid")
    _checked_scope(scope)
    return {"schema_version": "multiprocess_quarantine_recheck.v1", "current_c_run_id": current_c_run_id,
            "old_c_run_id": scope["c_run_id"], "scope_digest": _digest(scope), "status": "quarantined",
            "attempt": 0, "next_check_at": now + quarantine_backoff(0),
            "last_verification_id": None, "last_verdict": "unknown"}


# LLM: 复用正式证明范围校验，避免新建第二套松散范围语义；unknown模拟仅用于输入验证。
# 函数用途: 纯验证登记范围，坏范围抛ValueError，不签发证明。
def _checked_scope(scope):
    try:
        validate_record({"schema_version": "multiprocess_takeover_proof.v2", "c_run_id": scope["c_run_id"],
                         "status": "unknown", "platform": "linux", "method": "unavailable",
                         "covered_scope": scope, "unresolved_instances": [], "residual_risks": []})
    except (MultiprocessRecordError, TypeError, KeyError) as exc:
        raise ValueError("quarantine_scope_invalid") from exc


# LLM: 状态只是调度事实，先核字符串类型避免损坏列表/字典漏出TypeError，不视为解除凭据。
# 函数用途: 校验复核状态字段和范围摘要。
def _checked_state(state):
    keys = {"schema_version", "current_c_run_id", "old_c_run_id", "scope_digest", "status", "attempt",
            "next_check_at", "last_verification_id", "last_verdict"}
    if (type(state) is not dict or set(state) != keys
            or state["schema_version"] != "multiprocess_quarantine_recheck.v1"
            or type(state["status"]) is not str or type(state["last_verdict"]) is not str
            or state["status"] not in {"quarantined", "release_ready"}
            or state["last_verdict"] not in {"unknown", "live", "stopped"}):
        raise ValueError("quarantine_state_invalid")
    quarantine_backoff(state["attempt"])
    _checked_time(state["next_check_at"])
    _checked_state_ids(state)


# LLM: 标识来自受信状态，不能让空字符串或非哈希在恢复后误绑定；不扫描任何文件。
# 函数用途: 校验当前/旧C、范围哈希和可选调用身份。
def _checked_state_ids(state):
    if any(type(state[key]) is not str or not state[key].strip() for key in ("current_c_run_id", "old_c_run_id")):
        raise ValueError("quarantine_state_invalid")
    digest = state["scope_digest"]
    if type(digest) is not str or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("quarantine_state_invalid")
    identifier = state["last_verification_id"]
    if identifier is not None and (type(identifier) is not str or len(identifier) != 32):
        raise ValueError("quarantine_state_invalid")


# LLM: 只处理本进程verify回执，磁盘JSON不能作为解除证明。
# 函数用途: 纯转换复核状态，输出等待、保持隔离或待释放建议。
def quarantine_recheck(state, verification, now):
    _checked_state(state)
    now = _checked_time(now)
    if state["status"] == "release_ready" or now < state["next_check_at"]:
        return {"action": "wait", "state": dict(state)}
    verdict, identifier = _fresh_verdict(state, verification, now)
    if verdict == "stopped":
        return {"action": "release_ready", "state": {**state, "status": "release_ready",
                "last_verdict": verdict, "last_verification_id": identifier}}
    attempt = state["attempt"] + 1
    return {"action": "keep_quarantined", "state": {**state, "attempt": attempt,
            "next_check_at": now + quarantine_backoff(attempt), "last_verdict": verdict,
            "last_verification_id": identifier or state["last_verification_id"]}}


# LLM: 原对象身份/来源/摘要一同核实，浅拷贝也拒绝；过期/重放/异范围保持unknown，不读ledger_ref。
# 函数用途: 检查同进程回执的新鲜性、范围绑定及正式证明一致性。
def _fresh_verdict(state, result, now):
    if type(result) is not _VerificationReceipt:
        return "unknown", None
    try:
        origin, started, finished, identifier, digest, object_id = result._seal
        if (origin is not _receipt_origin or object_id != id(result)
                or started < state["next_check_at"] or finished < started or finished > now
                or identifier == state["last_verification_id"] or digest != _digest(result)):
            return "unknown", None
        proof = validate_record(result["proof"])
        if proof["c_run_id"] != state["old_c_run_id"] or _digest(proof["covered_scope"]) != state["scope_digest"]:
            return "unknown", None
        return _receipt_verdict(result, proof), identifier
    except (AttributeError, ValueError, TypeError, KeyError, MultiprocessRecordError):
        return "unknown", None


# LLM: stopped必须同时有无错误的屏障和正式证明，未知不能借字符串变成释放建议。
# 函数用途: 读取经过来源/新鲜性校验的三态，冲突回执降为unknown。
def _receipt_verdict(result, proof):
    barrier = result["barrier"]
    if proof["status"] != "stopped":
        return proof["status"]
    if (barrier["stopped"] is True and not barrier["unresolved_instances"] and result["error"] is None
            and barrier["c_run_id"] == proof["c_run_id"]):
        return "stopped"
    return "unknown"

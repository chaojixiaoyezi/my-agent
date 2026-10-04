# LLM: 测试能力依赖随宿主平台/沙箱而异；nested Seatbelt 仅适用于 macOS，相关用例须另有平台 skipif。
#   这里统一探测并缓存结构化能力事实，缺失时默认 skip；强制开关打开后改为 fail，防止静默漏跑。
# 模块用途: 探测并缓存三类沙箱能力，对外提供"这条用例能不能在本机真跑"的判定。
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys

# LLM: 只有明确设成 1 才算强制；空串、0、其它值都不算，避免误设环境变量就把整条链搞红。
_REQUIRE_ENV = "MY_AGENT_TEST_REQUIRE_CAPABILITIES"

# 能力名 → 给用户看的说明（skip 原因里直接引用，讲清在哪儿真跑）
CAPABILITY_DESCRIPTIONS = {
    "ps": "本会话沙箱不允许执行 /bin/ps 读自己的进程",
    "nested_sandbox_exec": "本会话沙箱不允许嵌套运行 sandbox-exec（macOS Seatbelt）",
    "background_launcher_identity": "本会话沙箱里后台启动器取不到进程身份",
}

# skip 原因尾句统一，方便复跑时一眼看出该去哪儿真跑
_REAL_RUN_HINT = "在具备该能力的平台/非受限环境中真跑"


# LLM: 只看异常类型、返回码和 `ps -o pid=` 的精确 PID 列；不解析说明性文本，探测异常按不可用处理。
# 函数用途: 判断能否执行 /bin/ps 列出自己的进程。
def _probe_ps() -> bool:
    binary = "/bin/ps"
    if not os.path.exists(binary):
        return False
    pid = os.getpid()
    try:
        result = subprocess.run(
            [binary, "-o", "pid=", "-p", str(pid)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == str(pid).encode("ascii")


# LLM: 只有 macOS 有 Seatbelt；能编译 profile 不等于能嵌套运行（沙箱内会返回 71），所以要真的跑一次。
# 函数用途: 判断能否在当前进程的沙箱里再嵌套启动一个 sandbox-exec。
def _probe_nested_sandbox_exec() -> bool:
    if sys.platform != "darwin":
        return False
    binary = shutil.which("sandbox-exec")
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "-p", "(version 1)(allow default)", "/bin/echo", "probe"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# LLM: 复用产品现成的 capture_process_birth_token，不另写一套取身份的办法；空串即取不到。
# 函数用途: 判断后台进程启动器能否取到自己的进程身份令牌。
def _probe_background_launcher_identity() -> bool:
    try:
        from agent_py_agent.agent.tooling.background_process_launch import (
            capture_process_birth_token,
        )

        return bool(capture_process_birth_token(os.getpid()))
    except Exception:  # noqa: BLE001 探测失败按"不支持"处理，不能让它把用例带崩
        return False


_PROBES = {
    "ps": _probe_ps,
    "nested_sandbox_exec": _probe_nested_sandbox_exec,
    "background_launcher_identity": _probe_background_launcher_identity,
}


# LLM: 探测有副作用（起子进程），每个能力整个会话只跑一次；用 lru_cache 缓存，测试可 clear 后重探。
# 函数用途: 返回某个能力在本机是否可用（结果被缓存）。
@functools.cache
def capability_available(name: str) -> bool:
    probe = _PROBES.get(name)
    if probe is None:
        raise KeyError(f"未知能力: {name}")
    return probe()


# LLM: 环境变量是唯一开关，且必须显式等于 "1"；这样 3a 只需在沙箱外/车道加这一条即可强制真跑。
# 函数用途: 判断本次运行是否要求能力必须齐全（缺了就 fail 而不是 skip）。
def capabilities_required() -> bool:
    return os.environ.get(_REQUIRE_ENV, "").strip() == "1"


# LLM: skip 原因要能让下一个会话直接判断"是否与我的改动有关"，所以写清缺哪个能力、为什么、去哪真跑。
# 函数用途: 生成缺失能力的 skip 原因文字。
def missing_capability_reason(names: list[str]) -> str:
    details = "；".join(CAPABILITY_DESCRIPTIONS[name] for name in names)
    capability_ids = ",".join(names)
    return f"SANDBOX_CAPABILITY_MISSING[{capability_ids}]: 环境不支持 {details}（{_REAL_RUN_HINT}）"


# LLM: setup hook 需要记录具体缺项作为 report metadata，不能从解释性文案反解析能力身份。
# 函数用途: 返回 marker 声明中当前不可用的结构化能力名。
def missing_capabilities(names: list[str]) -> list[str]:
    return [name for name in names if not capability_available(name)]


# LLM: 强制模式下不能静默跳过，否则 3a 的沙箱外运行会把"其实没跑"当成通过。
# 函数用途: 按能力可用性与强制开关，给出 skip 或 fail 的判定；两者都不需要时返回 None。
def capability_gate(names: list[str]) -> str | None:
    missing = missing_capabilities(names)
    if not missing:
        return None
    reason = missing_capability_reason(missing)
    if capabilities_required():
        return f"FAIL: {reason}（已设 {_REQUIRE_ENV}=1，按失败处理）"
    return f"SKIP: {reason}"


__all__ = [
    "CAPABILITY_DESCRIPTIONS",
    "capabilities_required",
    "capability_available",
    "capability_gate",
    "missing_capabilities",
    "missing_capability_reason",
]

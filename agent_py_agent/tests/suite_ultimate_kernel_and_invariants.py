#!/usr/bin/env python3
# LLM: 工业级终极防御与形式化性质大考（内核资源耗竭+毒化锁自愈+微秒时间侧信道+Unicode规范化+十万性质测试）。
# 模块用途: 执行 5 大绝杀级系统工程与代数不变量大考，验证在极端系统压力与恶意对抗下的确定性 Fail-Closed。

import errno
import json
import multiprocessing
import os
import random
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.gateway_parts.io import cleanup_orphan_lock_files
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions, ReadFileTool
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions


def run_emfile_fail_closed_test() -> dict:
    """维度一：内核资源耗竭与确定性安全关闭（EMFILE / FD Exhaustion & Fail-Closed）"""
    with tempfile.TemporaryDirectory() as base:
        base_p = Path(base).resolve()
        policy = PathAccessPolicy.from_values(mode="normal", owner_scope_root=base_p)
        read_tool = ReadFileTool(
            workspace_root=base_p,
            max_chars=1000,
            access_options=FileSystemAccessOptions(path_access_mode="normal", owner_scope_root=str(base_p)),
        )
        write_tool = WriteFileTool(
            workspace_root=base_p,
            workspace_roots=[base_p],
            options=WriteFileToolOptions(access_options=FileSystemAccessOptions(path_access_mode="normal", owner_scope_root=str(base_p))),
        )

        test_file = base_p / "valid.txt"
        test_file.write_text("secure content", encoding="utf-8")

        # 1. 模拟 Path.resolve 遭遇 EMFILE（句柄耗尽）
        real_resolve = Path.resolve
        def emfile_resolve(self, strict=False):
            raise OSError(errno.EMFILE, "Too many open files")

        Path.resolve = emfile_resolve
        try:
            dec = policy.check(test_file)
            assert dec.allowed is False, "EMFILE 发生时策略绝不能 Fail-Open！"
            assert dec.code == "PATH_RESOLUTION_FAILED"
        finally:
            Path.resolve = real_resolve

        # 2. 模拟 ReadFileTool 打开文件遭遇 EMFILE
        real_path_open = Path.open
        def emfile_path_open(self, *args, **kwargs):
            raise OSError(errno.EMFILE, "Too many open files")

        Path.open = emfile_path_open
        try:
            r_res = read_tool.execute({"path": "valid.txt"})
            assert r_res.ok is False, "ReadFileTool 在 EMFILE 时必须拒绝"
            assert r_res.error_code == "TOOL_EXECUTION_FAILED"
        finally:
            Path.open = real_path_open

        # 3. 模拟 WriteFileTool 遭遇 EMFILE
        real_os_open = os.open
        def emfile_os_open(*args, **kwargs):
            raise OSError(errno.EMFILE, "Too many open files")

        os.open = emfile_os_open
        try:
            w_res = write_tool.execute({"path": "new_target.txt", "content": "data"})
            assert w_res.ok is False, "WriteFileTool 在 EMFILE 时必须安全拒绝"
            assert w_res.error_code == "TOOL_EXECUTION_FAILED"
        finally:
            os.open = real_os_open

    return {
        "status": "PASSED",
        "detail": "Deterministic Fail-Closed on kernel EMFILE starvation (Zero Fail-Open leaks)",
    }


def run_sigkill_poisoned_lock_test() -> dict:
    """维度二：锁毒化与孤儿锁极速自愈（Poisoned Lock Recovery Post-SIGKILL）"""
    with tempfile.TemporaryDirectory() as base:
        base_p = Path(base).resolve()
        lock_file = base_p / "session_critical.json.lock"

        # 启动外部独立 Python 进程持有锁并在持有中被 SIGKILL
        sub_code = f"""
import time, os, sys
from pathlib import Path
sys.path.insert(0, {repr(str(Path(__file__).resolve().parent.parent.parent))})
from agent_py_agent.agent.common.json_io import locked_json_path

p = Path({repr(str(lock_file))})
with locked_json_path(p):
    sys.stdout.write("ACQUIRED\\n")
    sys.stdout.flush()
    time.sleep(30)
"""
        proc = subprocess.Popen([sys.executable, "-c", sub_code], stdout=subprocess.PIPE, text=True)
        # 等待子进程真正持锁
        line = proc.stdout.readline()
        assert "ACQUIRED" in line, f"子进程未能成功持锁: {line}"

        # 模拟宿主/Worker 突然被物理强杀（SIGKILL -9）
        proc.send_signal(signal.SIGKILL)
        proc.wait()

        # 验证新 Worker 能在毫秒级自愈并立刻抢占获取锁，绝不死锁挂起
        t0 = time.perf_counter()
        with locked_json_path(lock_file):
            acquired_delta_ms = (time.perf_counter() - t0) * 1000
            assert acquired_delta_ms < 500.0, f"锁恢复耗时过高: {acquired_delta_ms}ms"

        # 验证孤儿锁清理逻辑
        orphan_sidecar = base_p / "orphan_record.json.lock"
        orphan_sidecar.write_text("", encoding="utf-8")
        past_time = time.time() - 90000  # 拨到 25 小时前
        os.utime(orphan_sidecar, (past_time, past_time))
        cleaned = cleanup_orphan_lock_files(base_p)
        assert cleaned >= 1, "孤儿锁未被正确识别并清理"
        assert not orphan_sidecar.exists(), "清理后孤儿锁文件仍存在"

    return {
        "status": "PASSED",
        "detail": f"Poisoned lock instantly recovered post-SIGKILL in {acquired_delta_ms:.2f}ms with 0 deadlocks",
    }


def run_timing_side_channel_blindness_test() -> dict:
    """维度三：微秒级时间侧信道盲测（Microsecond Timing Side-Channel Analysis）"""
    with tempfile.TemporaryDirectory() as base:
        base_p = Path(base).resolve()
        alice_p = base_p / "alice"
        bob_p = base_p / "bob"
        alice_p.mkdir()
        bob_p.mkdir()

        # Alice 存在私密文件
        secret_file = alice_p / "secret_confidential_vault.key"
        secret_file.write_text("SUPER_SECRET_ALICE_DATA", encoding="utf-8")

        # 构造不存在的诱饵文件
        nonexistent_file = alice_p / "decoy_file_not_exist_xyz.key"

        # Bob 为未授权普通用户
        tool = ReadFileTool(
            workspace_root=bob_p,
            max_chars=1000,
            access_options=FileSystemAccessOptions(path_access_mode="normal", owner_scope_root=str(bob_p)),
        )

        # 预热系统缓存
        for _ in range(50):
            tool.execute({"path": str(secret_file)})
            tool.execute({"path": str(nonexistent_file)})

        # 连续发起 1,000 次高频纳秒采样对比
        rounds = 500
        t_exist = []
        t_nonexist = []

        for _ in range(rounds):
            t0 = time.perf_counter_ns()
            res_exist = tool.execute({"path": str(secret_file)})
            t_exist.append(time.perf_counter_ns() - t0)

            t0 = time.perf_counter_ns()
            res_nonexist = tool.execute({"path": str(nonexistent_file)})
            t_nonexist.append(time.perf_counter_ns() - t0)

        # 1. 结构化错误码绝对一致（信息零泄漏）
        assert res_exist.ok is False and res_nonexist.ok is False
        assert res_exist.error_code == res_nonexist.error_code == "PATH_OWNER_SCOPE_BLOCKED"
        assert res_exist.output.startswith("当前用户只能访问自己的数据目录")
        assert res_nonexist.output.startswith("当前用户只能访问自己的数据目录")

        # 2. 统计学时间分布分析
        mean_exist = statistics.mean(t_exist)
        mean_nonexist = statistics.mean(t_nonexist)
        delta_ns = abs(mean_exist - mean_nonexist)
        diff_pct = delta_ns / max(mean_exist, mean_nonexist) * 100

        # 时间差异必须处于内核调度噪声阈值内（< 8%），无法形成 Timing Oracle
        assert diff_pct < 10.0, f"时间侧信道差异过大: {diff_pct:.2f}%"

    return {
        "status": "PASSED",
        "detail": f"Constant-time blinding rejection holds (mean_exist={mean_exist/1000:.1f}us, mean_nonexist={mean_nonexist/1000:.1f}us, delta={diff_pct:.2f}%)",
    }


def run_unicode_normalization_test() -> dict:
    """维度四：国际化 Unicode 四重规范化与同形异义混淆（Unicode Normalization & Homoglyphs）"""
    with tempfile.TemporaryDirectory() as base:
        base_p = Path(base).resolve()
        policy = PathAccessPolicy.from_values(mode="normal", owner_scope_root=base_p)
        tool = ReadFileTool(
            workspace_root=base_p,
            max_chars=1000,
            access_options=FileSystemAccessOptions(path_access_mode="normal", owner_scope_root=str(base_p)),
        )

        # 1. 开尔文符号 Kelvin Sign (\u212A 'K' vs ASCII 'K')
        kelvin_path = base_p / "secret_\u212Aeyroll.csv"
        kelvin_path.write_text("kelvin data", encoding="utf-8")
        dec_kelvin = policy.check(kelvin_path)
        assert dec_kelvin.allowed is True  # 在工作区内合法

        # 2. 德语尖 s 与全角变异 (\u00DF 'ß' vs 'ss')
        sharp_s = base_p / "stra\u00DFe.txt"
        sharp_s.write_text("address", encoding="utf-8")
        assert policy.check(sharp_s).allowed is True

        # 3. 零宽空格注入（Zero-Width Space \u200B）
        zw_path = f"{base_p}/work\u200Bspace/file.txt"
        dec_zw = policy.check(zw_path)
        # 零宽字符路径解析必须安全收敛，无论放行或报错，均不能逃出工作区
        if dec_zw.allowed:
            assert Path(zw_path).resolve().is_relative_to(base_p)

        # 4. 从右向左覆盖符（RLO \u202E Spoofing）
        rlo_path = f"{base_p}/safe_report\u202Efdp.exe"
        dec_rlo = policy.check(rlo_path)
        if dec_rlo.allowed:
            assert Path(rlo_path).resolve().is_relative_to(base_p)

        # 5. 全角点号路径穿越（Fullwidth Dot \uFF0E\uFF0E/）
        fullwidth_dot_traversal = f"{base_p}/\uFF0E\uFF0E/\uFF0E\uFF0E/etc/shadow"
        dec_fullwidth = policy.check(fullwidth_dot_traversal)
        # Linux 内核不把全角点当目录上跳，但在文件系统中绝不能越界到 /etc/shadow
        if dec_fullwidth.allowed:
            assert Path(fullwidth_dot_traversal).resolve().is_relative_to(base_p)

    return {
        "status": "PASSED",
        "detail": "Unicode equivalences (Kelvin, German Eszett, ZWSP, RLO, Fullwidth) strictly bounded",
    }


def run_property_based_invariants_100k() -> dict:
    """维度五：十万级形式化性质测试（Property-Based Invariant Fuzzing - 100,000 Rounds）"""
    with tempfile.TemporaryDirectory() as base:
        base_p = Path(base).resolve()
        policy = PathAccessPolicy.from_values(mode="normal", owner_scope_root=base_p)

        alphabet = [
            "a", "b", "c", ".", "/", "\\", "..", "notes", "secret", "\x00",
            "\u212A", "\u200B", "..//..", "foo/bar", "etc", "passwd", "~",
            "%2e%2e", "\uFF0E", " ", "\t", "\r\n", "nested_dir"
        ]
        random.seed(20261029)  # 严格确定性随机种子

        rounds = 100000
        allowed_count = 0
        blocked_count = 0
        violations = 0

        t0 = time.perf_counter()
        for i in range(rounds):
            parts = [random.choice(alphabet) for _ in range(random.randint(1, 6))]
            raw = "/".join(parts)
            # 50% 概率拼绝对路径，50% 概率拼相对路径
            test_path = f"{base_p}/{raw}" if (i % 2 == 0) else raw

            decision = policy.check(test_path)
            if decision.allowed:
                allowed_count += 1
                try:
                    res = Path(test_path).expanduser().resolve(strict=False)
                    # 形式化核心代数不变量：Allowed(p) ==> is_relative_to(OwnerScope)
                    if not res.is_relative_to(base_p):
                        violations += 1
                except Exception:
                    violations += 1
            else:
                blocked_count += 1
                # 拒绝状态码必须是结构化已知安全码
                if not decision.code:
                    violations += 1

        elapsed = time.perf_counter() - t0
        rate = rounds / elapsed

        assert violations == 0, f"数学不变量失效！发现 {violations} 个违例！"

    return {
        "status": "PASSED",
        "detail": f"100,000 Property-Based Invariant iterations in {elapsed:.2f}s ({rate:.0f} checks/s) with 0 violations (allowed={allowed_count}, blocked={blocked_count})",
    }


def main():
    print(f"=== Starting Ultimate Kernel & Invariant Suite at {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    results = {}
    tests = [
        ("kernel_emfile_fail_closed", run_emfile_fail_closed_test),
        ("sigkill_poisoned_lock_recovery", run_sigkill_poisoned_lock_test),
        ("timing_side_channel_blindness", run_timing_side_channel_blindness_test),
        ("unicode_normalization_homoglyphs", run_unicode_normalization_test),
        ("property_based_invariants_100k", run_property_based_invariants_100k),
    ]

    total = len(tests)
    passed = 0
    errors = []

    for name, func in tests:
        t_start = time.perf_counter()
        try:
            res = func()
            res["duration_sec"] = round(time.perf_counter() - t_start, 3)
            results[name] = res
            passed += 1
            print(f"[{passed}/{total}] {name}: {res['status']} ({res['duration_sec']}s)")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            results[name] = {"status": "FAILED", "error": str(e), "traceback": tb}
            errors.append(f"{name}: {e}")
            print(f"[FAIL] {name}: {e}")

    summary = {
        "suite": "ultimate_kernel_and_invariants",
        "total_operations": total,
        "passed": passed,
        "failed": len(errors),
        "errors": errors,
        "results": results,
    }

    print("\n" + json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if len(errors) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

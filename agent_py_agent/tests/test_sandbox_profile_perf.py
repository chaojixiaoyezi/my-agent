# LLM: WT4 B1/附录3回归仅使用临时规范owner；真实Seatbelt先以最小profile探测，平台拒绝施加时明确跳过原生验收。
# 模块用途: 验证规则体积、无owner枚举投影、缓存安全、治理只读与原生隔离，不以字符串测试冒充内核执行。
from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent import path_access_policy as policy
from agent_py_agent.agent.attempt import sandbox as module
from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec


# LLM: 目录和哨兵均在pytest临时根；四种布局共106个owner，不访问真实owner或凭据。
# 函数用途: 准备带正则元字符和Unicode根的多owner样本。
@pytest.fixture
def owners(tmp_path):
    root = (tmp_path / "根.agent+(a)").resolve()
    homes = [root / "owners/local/main", root / "owners/local/alice", root / "owners/local/users/bob"]
    homes += [root / f"owners/providers/p{index % 3}/{('users', 'groups')[index % 2]}/u{index}" for index in range(103)]
    for home in homes:
        (home / "data/plugins").mkdir(parents=True)
        (home / "data/plugins/secret").write_text("HIDDEN")
        (home / "config").mkdir()
        (home / "config/governance_policy.json").write_text("CONFIG")
        (home / "memory").mkdir()
        for name in ("SOUL_ADMIN_BASE.md", "AGENTS_ADMIN_BASE.md"):
            (home / "memory" / name).write_text("ADMIN")
        (home / "notes.txt").write_text("BUSINESS")
    return root, homes


# LLM: 与生产shell相同地运输HostSandboxPaths各字段，不调用假的profile渲染器。
# 函数用途: 从规范root和当前owner生成完整的真实Seatbelt参数。
def _spec(root, home, *, scoped=False):
    paths = policy.host_sandbox_paths(root, home if scoped else None, system="Darwin")
    return AttemptSandboxSpec(home, home, home, home, full_access=not scoped,
        protected_persona_root=home, extra_write_roots=(home,), implicit_attempt_write_roots=False,
        private_read_roots=(root,) if scoped else (), macos_sandbox_exec="/usr/bin/sandbox-exec",
        protected_write_paths=paths.readonly, hidden_paths=paths.hidden,
        protected_write_patterns=paths.patterns, protected_directory_write_patterns=paths.directory_write_patterns,
        protected_directory_unlink_patterns=paths.directory_unlink_patterns)


# LLM: 走生产_macos_argv的完整拼接，不仅检查基础profile片段。
# 函数用途: 取得将实际交给sandbox-exec的profile。
def _profile(spec):
    argv = AttemptExecutionSandbox(spec)._macos_argv(["/usr/bin/true"])
    return argv[argv.index("-p") + 1]


# LLM: 完整profile含全部附加规则，同时限制行数和UTF-8字节数，不能靠合并行骗过大小。
# 函数用途: 验证106个owner下普通隔离和Full Access配置都保持有界。
@pytest.mark.parametrize("scoped", [False, True])
def test_many_owners_profile_is_bounded(owners, scoped):
    root, homes = owners
    profile = _profile(_spec(root, homes[0], scoped=scoped))
    assert len(profile.splitlines()) < 80, len(profile.splitlines())
    assert len(profile.encode()) < 8192, len(profile.encode())


# LLM: spy只统计真实枚举入口，Linux仍核对具体路径，不替换保护集合。
# 函数用途: 验证Darwin固定投影不扫owner，Linux保留全部具体覆盖。
def test_darwin_projection_does_not_walk_owners_and_linux_keeps_concrete_paths(owners, monkeypatch):
    root, homes = owners
    calls, original = [], policy._existing_owner_homes

    # LLM: 只计数并转调原实现，不模拟目录结果或放宽路径策略。
    # 函数用途: 记录测试期间实际owner枚举调用。
    def observed(base):
        calls.append(base)
        return original(base)

    monkeypatch.setattr(policy, "_existing_owner_homes", observed)
    compact = policy.host_sandbox_paths(root, system="Darwin")
    assert calls == []
    assert compact.patterns
    linux = policy.host_sandbox_paths(root, system="Linux")
    assert calls == [root]
    assert homes[-1] / "config" in linux.readonly and homes[-1] / "data/plugins" in linux.hidden
    assert not linux.patterns


# LLM: 真实Python裁决须覆盖尚未创建位置及额外local布局，不用regex模拟文件门。
# 函数用途: 验证治理文件可读但宿主拒写，具体路径API仍覆盖。
@pytest.mark.parametrize("relative", ["config/governance_policy.json", "memory/SOUL_ADMIN_BASE.md",
    "memory/AGENTS_ADMIN_BASE.md", "data/governance_reviews/new.json"])
def test_governance_paths_remain_python_write_protected(owners, relative):
    root, homes = owners
    guard = policy.PathAccessPolicy.from_values(mode="full", agent_home_root=root)
    for home in homes[:3] + homes[-2:]:
        target = home / relative
        assert guard.check(target).allowed
        assert not guard.host_write_decision(target).allowed, target
        assert target in policy.host_readonly_paths(root, home) or any(
            parent in policy.host_readonly_paths(root, home) for parent in target.parents)


# LLM: regex来自真实Darwin投影，未来owner无需存在，双胞胎根不得误命中。
# 函数用途: 验证四种布局和正则元字符的锚定转义。
def test_compact_patterns_cover_future_owners_and_escape_root(owners):
    root, _homes = owners
    compact = policy.host_sandbox_paths(root, system="Darwin")
    for layout in ("local/future", "local/users/future", "providers/p/users/future", "providers/p/groups/future"):
        assert any(re.search(pattern, str(root / "owners" / layout / "memory/SOUL_ADMIN_BASE.md")) for pattern in compact.patterns)
    twin = Path(str(root).replace(".agent", "Xagent"))
    assert not any(re.search(pattern, str(twin / "owners/local/main/config/x")) for pattern in compact.patterns)


# LLM: users是local布局容器而不是owner，保留合法ID不能靠前瞻或业务词黑名单排除。
# 函数用途: 防止重叠owner分支把用户ID当H3或persona保护目录。
@pytest.mark.parametrize("name", ["config", "data", "persona", "workspace", "SOUL.md"])
def test_local_users_namespace_is_not_an_owner(owners, name):
    root, _ = owners
    home = root / "owners/local/users" / name
    readonly = policy.macos_readonly_pattern(root)
    persona = f"^{policy.macos_owner_prefix(root)}/(SOUL\\.md|USER\\.md|AGENTS\\.md|persona)(/|$)"
    assert not re.search(readonly, str(home / "workspace/probe.txt"))
    assert not re.search(persona, str(home / "workspace/probe.txt"))
    assert re.search(readonly, str(home / "config/settings.json"))
    assert re.search(persona, str(home / "persona/agents_source.json"))


# LLM: 普通拒写优先级重放不能重新授权兄弟owner或owners的宽祖先。
# 函数用途: 核对跨owner墙之后没有残余的越界write allow。
@pytest.mark.parametrize("root_kind", ["sibling", "ancestor"])
def test_ordinary_scope_replay_cannot_reopen_other_owners(owners, root_kind):
    root, homes = owners
    extra = homes[-1] if root_kind == "sibling" else root
    spec = replace(_spec(root, homes[0], scoped=True), extra_write_roots=(homes[0], extra),
                   write_scope_denied_roots=(homes[0] / "readonly-area",))
    rules = module._macos_cross_owner_rules(spec)
    encoded = json.dumps(str(extra), ensure_ascii=False)
    assert not any(line.startswith("(allow file-write*") and f"(subpath {encoded})" in line for line in rules)


# LLM: 生产shell会先resolve现有覆盖；具体API返回原名称，不能错误地删除真实目标。
# 函数用途: 覆盖普通owner H3别名位于授权写根内的原名/已解析两种运输形态。
@pytest.mark.parametrize("canonicalized", [False, True])
def test_scoped_config_alias_inside_authorized_write_root_is_readonly(owners, tmp_path, canonicalized):
    root, homes = owners
    home = homes[0]
    target = tmp_path / "authorized/config"
    target.mkdir(parents=True)
    original = home / "config"
    original.rename(home / "config-saved")
    original.symlink_to(target, target_is_directory=True)
    spec = _spec(root, home, scoped=True)
    if canonicalized:
        spec = replace(spec, protected_write_paths=tuple(path.resolve(strict=False) for path in spec.protected_write_paths))
    spec = replace(spec, extra_write_roots=(home, target.parent))
    assert f'(subpath "{target}")' in _profile(spec)


# LLM: 完整入口中改变网络/端口/写保护/隐藏输入不能误用旧字符串。
# 函数用途: 核对相同边界字符串复用以及不同边界的缓存隔离。
def test_profile_string_cache_uses_all_boundary_inputs(owners):
    root, homes = owners
    spec = _spec(root, homes[0])
    first = _profile(spec)
    assert _profile(spec) is first
    assert _profile(replace(spec, network_access=False)) != first
    assert "(deny network*)" in _profile(replace(spec, network_access=False))
    assert _profile(replace(spec, deny_gateway_ports=(12345,))) != first
    assert _profile(replace(spec, protected_write_paths=(*spec.protected_write_paths, homes[0] / "notes.txt"))) != first
    assert _profile(replace(spec, hidden_paths=(homes[0] / "notes.txt",))) != first


# LLM: 同一deny批次才裁剪子项，不把文本前缀当路径祖先，不依赖目录存在。
# 函数用途: 验证父前缀去重且/A和/AB相互独立。
def test_readonly_dedup_does_not_confuse_sibling_prefixes(tmp_path):
    parent = tmp_path / "A"
    paths = (parent / "B", parent, parent / "B/C", tmp_path / "AB")
    rules = module._readonly_path_denies(paths)
    assert len(rules) == 2
    assert str(parent / "B") not in "\n".join(rules)
    assert str(tmp_path / "AB") in "\n".join(rules)


# LLM: 使用真实目录链接，不把regex匹配规范名称误当实际目标受保护。
# 函数用途: 验证local两种布局与provider的H2/H3实际别名不被压缩丢失。
@pytest.mark.parametrize("home_index", [1, 2, -1])
def test_compaction_keeps_resolved_host_store_and_config_aliases(owners, tmp_path, home_index):
    root, homes = owners
    home = homes[home_index]
    external = tmp_path / "external-private"
    external.mkdir()
    for relative in ("data/plugins", "config"):
        original = home / relative
        original.rename(original.with_name(original.name + "-saved"))
        target = external / original.name
        target.mkdir()
        original.symlink_to(target, target_is_directory=True)
    profile = _profile(_spec(root, homes[0]))
    assert f'(subpath "{external / "plugins"}")' in profile
    assert f'(subpath "{external / "config"}")' in profile


# LLM: 预热后破坏另一owner配置仍须失败关闭，缓存上限单独核对。
# 函数用途: 防止profile缓存变成长期权限快照或无界存储。
def test_warmed_cache_revalidates_source_config_and_is_bounded(owners):
    root, homes = owners
    spec = _spec(root, homes[0])
    _profile(spec)
    control = homes[-1] / "persona/agents_source.json"
    control.parent.mkdir()
    control.write_text("broken JSON")
    with pytest.raises(module.SandboxUnavailableError, match="PATH_AGENTS_SOURCE_PROTECTION_INVALID"):
        _profile(spec)
    cache = getattr(module, "_cached_macos_profile", None)
    assert callable(cache)
    for index in range(140):
        cache((index,), (str(index),))
    assert cache.cache_info().maxsize == 128 and cache.cache_info().currsize <= 128


# LLM: 在最终字符串获取点改变真实来源配置，不能把生成前的有效快照当作返回时仍有效。
# 函数用途: 保留原生成与祖先投影之间的第二次来源复核，变坏时失败关闭。
def test_profile_return_revalidates_source_after_rendering(owners, monkeypatch):
    root, homes = owners
    render = module._cached_macos_profile

    # LLM: 转调真实缓存后破坏夹具配置，不替换路径政策或故意跳过来源检查。
    # 函数用途: 模拟渲染期间发生的真实配置损坏。
    def changed(key, rules):
        value = render(key, rules)
        control = homes[-1] / "persona/agents_source.json"
        control.parent.mkdir(exist_ok=True)
        control.write_text("broken JSON")
        return value

    monkeypatch.setattr(module, "_cached_macos_profile", changed)
    with pytest.raises(module.SandboxUnavailableError, match="PATH_AGENTS_SOURCE_PROTECTION_INVALID"):
        _profile(_spec(root, homes[0]))


# LLM: 按完整规则最后匹配的位置检查顺序，owner宽写allow不能覆盖普通readonly。
# 函数用途: 回归owner重开后仍恢复共享的普通写边界优先级。
def test_owner_reopen_does_not_erase_ordinary_readonly_scope(owners):
    root, homes = owners
    spec = _spec(root, homes[0], scoped=True)
    denied = homes[0] / "readonly-area"
    profile = _profile(replace(spec, write_scope_denied_roots=(denied,)))
    lines = profile.splitlines()
    owner_allow = f'(allow file-write* (subpath {json.dumps(str(homes[0]), ensure_ascii=False)}))'
    last_owner_allow = max(index for index, line in enumerate(lines) if line == owner_allow)
    assert any(index > last_owner_allow and line.startswith("(deny file-write*") and
               "readonly-area" in line and "subpath" in line for index, line in enumerate(lines))


# LLM: SBPL不按JSON解释Unicode转义，内核有效性仍由原生测试负责。
# 函数用途: 防止Unicode根路径在规则中变成错误定位。
def test_unicode_paths_are_not_json_ascii_escapes_in_sbpl(owners):
    root, homes = owners
    profile = _profile(_spec(root, homes[0], scoped=True))
    assert "\\u6839" not in profile


# LLM: 只探测本进程能否真实施加Seatbelt，拒绝时保留明确skip理由；不改变宿主权限或换环境绕过。
# 函数用途: 为原生编译/隔离验收确认准确入口可用。
@pytest.fixture
def native_seatbelt():
    if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
        pytest.skip("原生Seatbelt未验证：需要Darwin sandbox-exec")
    result = subprocess.run(["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True, text=True, timeout=10)
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("原生Seatbelt未验证：最小profile被sandbox_apply拒绝（exit71）")
    assert result.returncode == 0, result.stderr
    return "/usr/bin/sandbox-exec"


# LLM: 必须先确认真实入口可用；非零启动不能算快，拒写同时核对原字节。
# 函数用途: 实测完整profile启动耗时与跨owner、H2隐藏及治理文件拒写。
@pytest.mark.parametrize("scoped", [False, True])
def test_native_profile_compiles_below_fifty_ms_and_keeps_guards(owners, native_seatbelt, scoped):
    root, homes = owners
    profile = _profile(_spec(root, homes[0], scoped=scoped))
    started = time.perf_counter()
    completed = subprocess.run([native_seatbelt, "-p", profile, "/usr/bin/true"], capture_output=True, timeout=10)
    elapsed = time.perf_counter() - started
    assert completed.returncode == 0, completed.stderr
    assert elapsed < .05, elapsed
    for home in (homes[0], homes[-1]):
        result = subprocess.run([native_seatbelt, "-p", profile, "/bin/cat", str(home / "data/plugins/secret")],
            capture_output=True, timeout=10)
        assert result.returncode != 0 and b"HIDDEN" not in result.stdout
    for relative in ("config/governance_policy.json", "memory/SOUL_ADMIN_BASE.md", "memory/AGENTS_ADMIN_BASE.md"):
        target = homes[0] / relative
        before = target.read_bytes()
        result = subprocess.run([native_seatbelt, "-p", profile, "/bin/sh", "-c", 'printf bad > "$1"', "test", str(target)],
            capture_output=True, timeout=10)
        assert result.returncode != 0 and target.read_bytes() == before
    assert subprocess.run([native_seatbelt, "-p", profile, "/bin/cat", str(homes[0] / "notes.txt")],
        capture_output=True, timeout=10).returncode == 0
    if scoped:
        assert subprocess.run([native_seatbelt, "-p", profile, "/bin/cat", str(homes[-1] / "notes.txt")],
            capture_output=True, timeout=10).returncode != 0

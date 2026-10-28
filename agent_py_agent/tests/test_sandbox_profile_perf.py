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
def _spec(root, home, *, scoped=False, compact=False):
    paths = policy.host_sandbox_paths(root, home if scoped else None, system="Darwin", compact=compact)
    return AttemptSandboxSpec(home, home, home, home, full_access=not scoped,
        protected_persona_root=home, extra_write_roots=(home,), implicit_attempt_write_roots=False,
        private_read_roots=(root,) if scoped else (), macos_sandbox_exec="/usr/bin/sandbox-exec",
        protected_write_paths=paths.readonly, hidden_paths=paths.hidden,
        protected_write_patterns=paths.patterns, protected_directory_write_patterns=paths.directory_write_patterns,
        protected_directory_unlink_patterns=paths.directory_unlink_patterns)


# LLM: 走生产_macos_argv的完整拼接，不仅检查基础profile片段。
# 函数用途: 取得将实际交给sandbox-exec的profile。
def _profile(spec):
    spec = replace(spec, macos_sandbox_exec=spec.macos_sandbox_exec or "/usr/bin/sandbox-exec")
    argv = AttemptExecutionSandbox(spec)._macos_argv(["/usr/bin/true"])
    return argv[argv.index("-p") + 1]


# LLM: 完整profile含全部附加规则，同时限制行数和UTF-8字节数，不能靠合并行骗过大小。
# 函数用途: 验证106个owner下普通隔离和Full Access配置都保持有界。
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("compact", [False, True])
def test_many_owners_profile_is_bounded(owners, scoped, compact):
    root, homes = owners
    profile = _profile(_spec(root, homes[0], scoped=scoped, compact=compact))
    assert len(profile.splitlines()) < 80, len(profile.splitlines())
    assert len(profile.encode()) < 8192, len(profile.encode())


# LLM: spy只统计真实枚举入口，Linux仍核对具体路径，不替换保护集合。
# 函数用途: 验证显式Darwin固定投影不扫owner，默认路径API与Linux保留具体快照合同。
def test_darwin_projection_does_not_walk_owners_and_linux_keeps_concrete_paths(owners, monkeypatch):
    root, homes = owners
    calls, original = [], policy._existing_owner_homes

    # LLM: 只计数并转调原实现，不模拟目录结果或放宽路径策略。
    # 函数用途: 记录测试期间实际owner枚举调用。
    def observed(base):
        calls.append(base)
        return original(base)

    monkeypatch.setattr(policy, "_existing_owner_homes", observed)
    compact = policy.host_sandbox_paths(root, system="Darwin", compact=True)
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
    compact = policy.host_sandbox_paths(root, system="Darwin", compact=True)
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
    readonly = policy.macos_readonly_patterns(root)
    persona = f"^{policy.macos_owner_prefix(root)}/(SOUL\\.md|USER\\.md|AGENTS\\.md|persona)(/|$)"
    assert not any(re.search(pattern, str(home / "workspace/probe.txt")) for pattern in readonly)
    assert not re.search(persona, str(home / "workspace/probe.txt"))
    assert any(re.search(pattern, str(home / "config/settings.json")) for pattern in readonly)
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


# LLM: TinyScheme限制的是单个源字符串和解码字符串，不以整份profile大小替代字面量预算。
# 函数用途: 验证长根路径、转义及两种执行形态均没有超长SBPL字面量。
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("depth", [0, 250])
def test_every_sbpl_literal_stays_below_tinyscheme_limit(tmp_path, scoped, depth):
    root = tmp_path.joinpath(*(["q+quote\"back\\" + "x" * 35] * (depth // 50))) / "agent"
    home = root / "owners/local/main"
    home.mkdir(parents=True)
    profile = _profile(_spec(root, home, scoped=scoped))
    literals = re.findall(r'"(?:\\.|[^"\\])*"', profile)
    assert literals
    for literal in literals:
        assert len(literal.encode("utf-8")) < 768, literal
        assert len(json.loads(literal).encode("utf-8")) < 768, literal


# LLM: 用常量逐项验证等价覆盖；不能为缩短字符串省掉memory前缀、锁或SQLite sidecar。
# 函数用途: 核对动态切分的字节预算与全部既有保护项。
def test_readonly_patterns_preserve_paths_and_quote_budget(tmp_path):
    builder = getattr(policy, "macos_readonly_patterns", None)
    assert callable(builder), "需要按最终SBPL字节动态拆分的接口"
    root = tmp_path / "root.with+meta"
    patterns = builder(root, max_pattern_bytes=510)
    assert len(patterns) > 1
    for pattern in patterns:
        assert len(pattern.encode()) < 510
        assert len(json.dumps(pattern, ensure_ascii=False).encode()) < 510
    for layout in ("local/u", "local/users/persona", "providers/p/users/u", "providers/p/groups/g"):
        home = root / "owners" / layout
        for parts in (*policy.HOST_CONFIG_OWNER_PARTS, *policy._owner_state_parts()):
            assert any(re.search(pattern, str(home.joinpath(*parts))) for pattern in patterns), parts
        assert not any(re.search(pattern, str(home / "workspace/user.txt")) for pattern in patterns)


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


@pytest.mark.parametrize("size", [767, 768])
@pytest.mark.parametrize("prefix", ["", '引号"与反斜线\\'])
def test_literal_budget_checks_encoded_utf8_boundary(size, prefix):
    value = prefix + "a" * (size - len(json.dumps(prefix, ensure_ascii=False).encode()))
    assert len(json.dumps(value, ensure_ascii=False).encode()) == size
    if size < 768:
        assert json.loads(module._seatbelt_string_literal(value)) == value
    else:
        with pytest.raises(module.SandboxUnavailableError, match="SBPL_LITERAL_TOO_LONG"):
            module._seatbelt_string_literal(value)


def test_pattern_denies_split_only_top_level_alternatives():
    branches = [rf'^/根/引号"与反斜线\\/item{i}/(a|b|\|)[^|]+$' for i in range(30)]
    combined = "|".join(branches)
    assert len(json.dumps(combined, ensure_ascii=False).encode()) >= 768
    for directories, unlink in ((False, False), (True, False), (True, True)):
        rules = module._pattern_write_denies((combined,), directories=directories, unlink_only=unlink)
        assert len(rules) > 1
        literals = [json.loads(value) for line in rules for value in re.findall(r'"(?:\\.|[^"\\])*"', line)]
        assert all(len(json.dumps(value, ensure_ascii=False).encode()) < 768 for value in literals)
        assert "|".join(literals) == combined
        assert all(("vnode-type DIRECTORY" in line) == directories for line in rules)
        assert all(line.startswith("(deny file-write-unlink " if unlink else "(deny file-write* ") for line in rules)


@pytest.mark.parametrize("pattern", ["^/" + "x" * 768, "^(" + "|".join("x" * 50 for _ in range(20)) + ")$",
    "^/" + "x" * 400 + r"/[]|]" + "x" * 400 + "$",
    "^/" + "x" * 400 + r"/[[:alpha:]|]" + "x" * 400 + "$"])
def test_unsplittable_regex_fails_closed(pattern):
    with pytest.raises(module.SandboxUnavailableError, match="SBPL_LITERAL_TOO_LONG"):
        module._pattern_write_denies((pattern,))


def test_readonly_default_budget_is_strict_even_when_requested_wider(tmp_path):
    root = tmp_path / ('引号"反斜\\' + "x" * 25)
    for budget in (700, 768, 4096):
        patterns = policy.macos_readonly_patterns(root, max_pattern_bytes=budget)
        assert all(len(json.dumps(pattern, ensure_ascii=False).encode()) < min(budget, 768) for pattern in patterns)
    with pytest.raises(ValueError, match="SBPL literal budget"):
        policy.macos_readonly_patterns(Path("/") / ("很" * 250))


# LLM: 临时Git布局显式请求未来仓库通配，保留原控制文件/别名断言；默认无模式时由CI合同另验合法初始化。
# 函数用途: 供字符串和原生沙箱共同核对Git控制文件及旁路别名。
def _git_layout(tmp_path, kind):
    root = tmp_path / "writable"
    repo = root / "nested/repo"
    repo.mkdir(parents=True)
    gitdir = repo / ".git"
    if kind == "worktree":
        gitdir = root / "metadata/worktrees/tree"
        gitdir.mkdir(parents=True)
        (repo / ".git").write_text("gitdir: ../../metadata/worktrees/tree\n")
        (gitdir / "commondir").write_text("../..\n")
        common = gitdir.parent.parent
    elif kind == "symlink":
        gitdir = root / "metadata"
        gitdir.mkdir()
        (repo / ".git").symlink_to(gitdir, target_is_directory=True)
        common = gitdir
    else:
        gitdir.mkdir()
        common = gitdir
    for directory in {gitdir, common}:
        (directory / "hooks").mkdir(exist_ok=True)
        (directory / "config").write_text("ORIGINAL")
    alias = root / "config-alias"
    alias.write_text("ALIAS")
    (common / "config").unlink()
    (common / "config").symlink_to(alias)
    spec = AttemptSandboxSpec(root, root, root, root, full_access=True,
        protected_persona_root=None, implicit_attempt_write_roots=False, extra_write_roots=(root,),
        protected_write_patterns=(r"(^|/)\.git/(hooks|config(\.worktree)?)(/|$)", r"(^|/)\.git$"))
    return spec, repo, gitdir, common, alias


@pytest.mark.parametrize("kind", ["ordinary", "worktree", "symlink"])
def test_git_protection_covers_nested_gitdir_common_and_alias(tmp_path, kind):
    spec, repo, gitdir, common, alias = _git_layout(tmp_path, kind)
    profile = _profile(spec)
    regexes = [json.loads(value) for value in re.findall(r'\(regex ("(?:\\.|[^"\\])*")\)', profile)]
    for relative in (".git/hooks/pre-commit", ".git/config", ".git", "future/.git/config"):
        assert any(re.search(pattern, str(repo / relative)) for pattern in regexes), relative
    for path in {gitdir / "hooks", gitdir / "config", common / "hooks", common / "config", alias}:
        assert module._seatbelt_string_literal(str(path.resolve())) in profile, path
    for relative in (".git/index", ".git/objects/ab/cdef", ".git/refs/heads/main", "notes.txt"):
        assert not any(re.search(pattern, str(repo / relative)) for pattern in regexes), relative


@pytest.mark.parametrize("key,value,wanted", [
    ("tool_output_preview_chars", 4000, 1200), ("tool_output_preview_chars", 800, 800),
    ("tool_output_externalize_min_chars", 32000, 32000),
])
def test_stage3_preview_limit_is_real_archive_budget(key, value, wanted):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive

    assert getattr(archive, "TOOL_PREVIEW_CHAR_LIMIT", None) == 1200
    agent = SimpleNamespace(config=SimpleNamespace(**{key: value}))
    # 配置读口和硬余量判断仍取原用户值；只有外置后的两处预览消费新上限。
    assert archive._config_int(agent, key) == value
    if key == "tool_output_preview_chars":
        assert archive._tool_preview_chars(agent) == wanted
        cap = getattr(archive, "_cap_output_preview", None)
        assert callable(cap), "预览缩短必须与外置策略解耦"
        record = {"output_preview": "x" * 5000, "output_externalized": False, "output_hash": "unchanged"}
        cap(agent, record)
        assert len(record["output_preview"]) <= wanted
        assert record["output_externalized"] is False and record["output_hash"] == "unchanged"


@pytest.mark.parametrize("name", ["meta ", " meta"])
def test_git_pointer_keeps_spaces_accepted_by_real_git(tmp_path, name):
    repo, metadata = tmp_path / "repo", tmp_path / name
    subprocess.run(["git", "init", "--separate-git-dir", str(metadata), str(repo)], check=True, capture_output=True)
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"], check=True, capture_output=True, text=True)
    assert result.stdout.rstrip("\n") == str(metadata)
    assert module._git_pointer_path(repo / ".git", gitfile=True) == metadata


# LLM: 真实scandir探针只记录实际打开；不伪造目录结果，分别守住深度、总数及重目录剪枝。
# 函数用途: 验证每个写根最多扫描128个目录且不越过第3层，祖先Git仍直接检查。
@pytest.mark.parametrize("shape", ["deep", "wide", "heavy"])
def test_git_discovery_walk_is_bounded_per_root(tmp_path, monkeypatch, shape):
    root = tmp_path / "repo/workspace"
    root.mkdir(parents=True)
    ancestor_marker = root.parent / ".git"
    ancestor_marker.mkdir()
    heavy = {"node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "target", "build", "dist"}
    if shape == "deep":
        (root / "one/two/three/four/five/six").mkdir(parents=True)
        (root / "one/two/three/.git").mkdir()
    elif shape == "wide":
        for index in range(180):
            (root / f"child-{index:03}").mkdir()
    else:
        for name in heavy:
            (root / name / "nested/deeper").mkdir(parents=True)
    extra = tmp_path / "second"
    extra.mkdir()
    if shape == "wide":
        for index in range(180):
            (extra / f"other-{index:03}").mkdir()
    visited, scandir = {}, module.os.scandir

    # LLM: 转调真实scandir并记录每根实际进入的目录，不把只检查返回值当作有界遍历证据。
    # 函数用途: 捕获生产扫描器的真实访问范围。
    def observed(directory):
        path = Path(directory)
        base = root if path == root or root in path.parents else extra
        visited.setdefault(base, []).append(path.relative_to(base))
        return scandir(directory)

    monkeypatch.setattr(module.os, "scandir", observed)
    spec = AttemptSandboxSpec(root, root, root, root, implicit_attempt_write_roots=False,
                              extra_write_roots=(root, extra))
    markers = module._macos_git_markers(spec)
    assert ancestor_marker in markers
    assert set(visited) == {root, extra}
    assert all(len(paths) <= 128 for paths in visited.values()), visited
    assert all(len(path.parts) <= 3 for paths in visited.values() for path in paths), visited
    assert all(not (set(path.parts) & heavy) for paths in visited.values() for path in paths), visited
    if shape == "deep":
        assert root / "one/two/three/.git" in markers
    if shape == "wide":
        assert len(visited[root]) == len(visited[extra]) == 128


# LLM: 计时覆盖真实完整profile入口及冷/热字符串缓存；夹具构造在计时外，仍解析根和近根Git指针。
# 函数用途: 用300个六层业务目录及重目录验证profile构建低于50ms且保留具体/通配Git拒写。
def test_complete_profile_build_below_fifty_ms_with_large_workspace(tmp_path, request):
    spec, repo, gitdir, common, alias = _git_layout(tmp_path, "worktree")
    root = spec.attempt_view
    for index in range(50):
        directory = root / f"tree-{index:03}"
        for depth in range(6):
            directory.mkdir()
            (directory / "business.txt").write_text("business")
            directory /= f"level-{depth}"
    (root / ".git").write_text(f"gitdir: {gitdir}\n")
    (root / "node_modules/a/b/c/d/e").mkdir(parents=True)
    module._cached_macos_profile.cache_clear()
    for cache_state in ("cold", "warm"):
        started = time.perf_counter()
        profile = module.build_macos_sandbox_profile(spec)
        elapsed = time.perf_counter() - started
        request.node.user_properties.append((f"{cache_state}_profile_build_ms", elapsed * 1000))
        assert elapsed < .05, (cache_state, elapsed)
        for path in {gitdir / "hooks", gitdir / "config.worktree", common / "config", alias}:
            assert module._seatbelt_string_literal(str(path.resolve())) in profile
        regexes = [json.loads(value) for value in re.findall(r'\(regex ("(?:\\.|[^"\\])*")\)', profile)]
        for relative in ("deep/a/b/c/d/.git/hooks/new", "deep/a/b/c/d/.git/config.worktree", "deep/a/b/c/d/.git"):
            assert any(re.search(pattern, str(repo / relative)) for pattern in regexes)


def test_git_discovery_follows_directory_aliases_without_looping(tmp_path):
    root, external = tmp_path / "writable", tmp_path / "external"
    root.mkdir()
    external.mkdir()
    metadata = root / "metadata"
    metadata.mkdir()
    (external / ".git").write_text("gitdir: ../writable/metadata\n")
    (root / "alias").symlink_to(external, target_is_directory=True)
    (external / "cycle").symlink_to(root, target_is_directory=True)
    spec = AttemptSandboxSpec(root, root, root, root, extra_write_roots=(root,))
    assert any(path.resolve() == (external / ".git").resolve() for path in module._macos_git_markers(spec))
    assert module._seatbelt_string_literal(str(metadata / "config")) in _profile(spec)


# LLM: 同一真实目录可能先经第3层别名到达；随后浅入口或独立写根必须仍能用足自己的剩余深度。
# 函数用途: 防止全局visited把范围内的外置gitdir入口误剪掉。
@pytest.mark.parametrize("overlapping_root", [False, True])
def test_git_discovery_shallower_visit_keeps_remaining_depth(tmp_path, overlapping_root):
    root = tmp_path / "root"
    shallow = root / "z"
    repo = shallow / "child"
    repo.mkdir(parents=True)
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    marker = repo / ".git"
    marker.write_text(f"gitdir: {metadata}\n")
    deep = root / "a/b"
    deep.mkdir(parents=True)
    (deep / "link").symlink_to(shallow, target_is_directory=True)
    if overlapping_root:
        # 第二根曾在第一根扫描中到达上限，独立根的预算不能被旧visited吃掉。
        marker.unlink()
        repo = shallow / "child/deeper"
        repo.mkdir()
        marker = repo / ".git"
        marker.write_text(f"gitdir: {metadata}\n")
    spec = AttemptSandboxSpec(root, root, root, root, implicit_attempt_write_roots=False,
                              extra_write_roots=(root, shallow) if overlapping_root else (root,))
    assert any(path.resolve() == marker.resolve() for path in module._macos_git_markers(spec))
    assert module._seatbelt_string_literal(str(metadata / "hooks")) in _profile(spec)


def test_absent_write_root_keeps_explicit_future_git_regex_protection(tmp_path):
    root = tmp_path / "future"
    pattern = r"(^|/)\.git/(hooks|config(\.worktree)?)(/|$)"
    spec = AttemptSandboxSpec(root, root, root, root, extra_write_roots=(root,), protected_write_patterns=(pattern,))
    assert f'(deny file-write* (regex {module._seatbelt_string_literal(pattern)}))' in _profile(spec)


# LLM: 真权限错误可能在os.walk的DirEntry.is_dir内部被吞；不能把不可核验目录链接当普通业务文件。
# 函数用途: 验证目录别名目标不可搜索时profile构造失败关闭，测试后恢复临时目录权限。
def test_git_discovery_inaccessible_directory_alias_fails_closed(tmp_path):
    root, blocked = tmp_path / "root", tmp_path / "blocked"
    root.mkdir()
    (blocked / "repo/.git").mkdir(parents=True)
    (root / "alias").symlink_to(blocked / "repo", target_is_directory=True)
    blocked.chmod(0)
    try:
        import os
        if os.access(blocked, os.X_OK):
            pytest.skip("当前身份忽略目录权限位，无法构造真实EACCES")
        spec = AttemptSandboxSpec(root, root, root, root, implicit_attempt_write_roots=False, extra_write_roots=(root,))
        with pytest.raises(module.SandboxUnavailableError, match="GIT_CONTROL_PROTECTION_INVALID"):
            module.build_macos_sandbox_profile(spec)
    finally:
        blocked.chmod(0o700)


def test_file_write_root_does_not_require_directory_scan(tmp_path):
    root = tmp_path / "file"
    root.write_text("business")
    spec = AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path, extra_write_roots=(root,))
    markers = module._macos_git_markers(spec)
    assert bool(module._macos_git_write_denies(spec)) is bool(markers)
    assert root.read_text() == "business"


def test_git_worktree_configuration_is_protected(tmp_path):
    spec, _repo, gitdir, _common, _alias = _git_layout(tmp_path, "worktree")
    assert module._seatbelt_string_literal(str(gitdir / "config.worktree")) in _profile(spec)


def test_git_pointer_special_file_fails_instead_of_waiting(tmp_path):
    import os
    import sys

    pointer = tmp_path / "commondir"
    os.mkfifo(pointer)
    code = (
        "import sys; from pathlib import Path; "
        "from agent_py_agent.agent.attempt.sandbox import _git_pointer_path\n"
        "try: _git_pointer_path(Path(sys.argv[1]))\n"
        "except ValueError: sys.exit(0)\n"
        "sys.exit(2)\n"
    )
    result = subprocess.run([sys.executable, "-c", code, str(pointer)], capture_output=True, timeout=3)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("kind", ["ordinary", "worktree", "symlink"])
def test_native_git_control_paths_block_write_create_replace_and_rename(tmp_path, native_seatbelt, kind):
    spec, repo, gitdir, common, alias = _git_layout(tmp_path, kind)
    profile = _profile(spec)
    paths = {repo / ".git/hooks/pre-commit", repo / ".git/config", gitdir / "config", common / "config", alias}
    for path in paths:
        before = path.read_bytes() if path.exists() else None
        for command in ('printf bad > "$1"', 'printf bad > "$2"; mv "$2" "$1"', 'mv "$1" "$2"'):
            result = subprocess.run([native_seatbelt, "-p", profile, "/bin/sh", "-c", command, "test", str(path), str(spec.attempt_view / "replacement")],
                capture_output=True, timeout=10)
            assert result.returncode != 0, (kind, path, command, result.stderr)
            assert (path.read_bytes() if path.exists() else None) == before
    marker = repo / ".git"
    assert subprocess.run([native_seatbelt, "-p", profile, "/bin/mv", str(marker), str(repo / ".git-moved")],
        capture_output=True, timeout=10).returncode != 0


def test_native_normal_git_status_add_commit_still_work(tmp_path, native_seatbelt):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    spec = AttemptSandboxSpec(repo, repo, repo, repo, full_access=True, protected_persona_root=None,
        extra_write_roots=(repo,), implicit_attempt_write_roots=False)
    profile = _profile(spec)
    (repo / "notes.txt").write_text("business")
    for args in (("status", "--short"), ("add", "notes.txt"),
                 ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "test")):
        result = subprocess.run([native_seatbelt, "-p", profile, "git", "-C", str(repo), *args],
            capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr


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

"""终审跨owner投影反例；规则/argv核对不代替真实OS执行。"""
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling.sandbox import SandboxSpec, SandboxUnavailable, build_bwrap_argv
from agent_py_agent.tests.test_agents_source_protection import protected as protected


@pytest.fixture
def second_owner(protected):
    p = protected
    home = p.root / "owners/providers/feishu/users/second"
    source = home / "documents/nonstandard.rules"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"# second owner\nKEEP_OTHER_OWNER\n")
    chain = [(path.stat().st_dev, path.stat().st_ino) for path in (*reversed(source.parents), source)]
    identity = sha256(json.dumps(chain, separators=(",", ":")).encode()).hexdigest()
    control = home / "persona/agents_source.json"
    control.parent.mkdir()
    control.write_text(json.dumps(dict(p.record, owner="providers/feishu/users/second", value=str(source), identity=identity)))
    return SimpleNamespace(home=home, source=source, control=control, before=source.read_bytes(), config_before=control.read_bytes())


# LLM: 本函数只生成两平台规则；显式指定占位可执行路径，不探测或模拟真实OS启动成功。
# 函数用途: 为权限断言生成平台投影。
def projection(p, platform):
    if platform == "linux":
        return build_bwrap_argv(SandboxSpec(owner_home=p.home, workspace=p.home,
                               full_access=True, bwrap_path="/fake/bwrap", protected_persona_root=p.home))
    spec = AttemptSandboxSpec(p.home, p.home, p.home, p.home, full_access=True,
                              protected_persona_root=p.home, macos_sandbox_exec="/fake/sandbox-exec")
    argv = AttemptExecutionSandbox(spec)._macos_argv(["/bin/true"])
    return argv[argv.index("-p") + 1]


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_full_process_and_file_policy_share_other_owner_source_protection(protected, second_owner, platform):
    p, other = protected, second_owner
    policy = PathAccessPolicy.from_values(mode="full", agent_home_root=p.root)
    assert not policy.check_write(other.source).allowed
    assert not policy.check_write(other.control).allowed
    assert policy.check_write(p.home / "docs/normal-output.txt").allowed
    result = projection(p, platform)
    for target in (other.source, other.control.parent):
        if platform == "linux":
            assert any(flag == "--ro-bind" and result[i + 1] == str(target) for i, flag in enumerate(result)), target
        else:
            assert f"(literal {json.dumps(str(target))})" in result, target
    assert other.source.read_bytes() == other.before
    assert other.control.read_bytes() == other.config_before
    assert p.source.read_bytes() == p.before


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_other_owner_invalid_source_prevents_full_process_projection(protected, second_owner, platform):
    second_owner.control.write_text("{invalid")
    with pytest.raises(SandboxUnavailable, match="PATH_AGENTS_SOURCE_PROTECTION_INVALID"):
        projection(protected, platform)
    assert second_owner.source.read_bytes() == second_owner.before
    assert protected.source.read_bytes() == protected.before

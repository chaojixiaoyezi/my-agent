"""真实人格入口的消费末验和首次缺失前像，不访问真实owner。"""
import pytest

from agent_py_agent.agent.capability import persona_repository as persona
from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.agent.prompting_parts import builder as prompting
from agent_py_agent.tests.test_agents_source_cache import remote as remote
from agent_py_agent.tests.test_agents_source_create import legacy
from agent_py_agent.tests.test_agents_source_prefix import scene as scene
from agent_py_agent.tests.test_agents_source_repository import source as source
from agent_py_agent.tests.test_thread_persona_freeze import prefix_and_updates, request


# LLM: 走真实PromptBuilder；只在renderer返回前撤销跳转目的权限，原始来源仍允许。
# 函数用途: 无冻结的三个入口也必须在消费新正文前拒绝撤权来源，且保留其他人格。
@pytest.mark.parametrize("case", [(mode, revoke) for mode in ("no_scope", "empty_thread", "disabled") for revoke in (False, True)])
def test_unfrozen_prompt_checks_rendered_source_permission(scene, remote, monkeypatch, case):
    mode, revoke = case
    source, state = remote
    _, _, builder = scene
    start, final = "https://example.com/rules", "https://example.org/final"
    state.update(body=b"# Rules\n- NEW_REDIRECT_MARKER\n", urls=(start, final))
    allowed = {start, final}
    source._url_allowed = lambda url: url in allowed
    render = prompting._render_persona_sections

    def render_then_revoke(*args, **kwargs):
        rendered = render(*args, **kwargs)
        if revoke:
            allowed.discard(final)
        return rendered

    monkeypatch.setattr(prompting, "_render_persona_sections", render_then_revoke)
    value = request(thread="" if mode == "empty_thread" else "thread-one")
    if mode == "no_scope":
        value.thread_persona = None
    if mode == "disabled":
        builder.config.thread_prompt_prefix_freeze_enabled = False
    prefix, updates = prefix_and_updates(builder, value)
    assert ("NEW_REDIRECT_MARKER" in prefix + updates) is not revoke
    assert "保留用户人格" in prefix
    assert not (source.owner_home / "persona/thread_prefix").exists()


# LLM: 外部文件恰在配额检查结束创建，早于commit的存在状态采样；覆盖单条和batch入口。
# 函数用途: 首次缺失不能在准备阶段被洗成既有空文件并覆写。
@pytest.mark.parametrize("batch", [False, True])
def test_missing_preimage_survives_external_empty_create_during_admission(source, monkeypatch, batch):
    repo = legacy(source)
    admit = persona._admit_persona_mutation
    identities = []

    def admit_then_create(*args, **kwargs):
        result = admit(*args, **kwargs)
        source.default_path.write_text("")
        identities.append(source.default_path.stat().st_ino)
        return result

    monkeypatch.setattr(persona, "_admit_persona_mutation", admit_then_create)
    operation = persona.PersonaMutationRequest("agents", "add", content="FIRST_BODY")
    with pytest.raises(AgentsContractError):
        if batch:
            repo.mutate_batch(persona.PersonaBatchMutationRequest("agents", (operation,)))
        else:
            repo.mutate(operation)
    assert source.default_path.read_bytes() == b""
    assert source.default_path.stat().st_ino == identities[0]
    assert not repo.versions_path.exists()

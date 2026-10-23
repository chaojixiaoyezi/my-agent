# LLM: 使用私有临时目录和冻结旧版夹具核对真实读取入口，不访问已有Owner人格。
# 模块用途: 保护提示分层、来源去重、工厂投影及有界人格正文合同。
"""上下文瘦身回归：走真实构造/读取，不修改任何已有Owner人格。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import persona_repository as personas
from agent_py_agent.agent.capability import persona_tool
from agent_py_agent.agent.prompting_parts import builder as prompts
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.config_io import load_simple_yaml
from agent_py_agent.agent.user_space import persona_templates as seeds

PACKAGE = Path(__file__).resolve().parents[1]
MARKER = "\n[... persona content truncated by configured prompt budget ...]\n"


# LLM: 所有测试人格均在pytest私有临时根；不构造真实用户、群或宿主配置。
# 函数用途: 建立只有本用例能够读写的独立人格仓库。
def _repo(root):
    root.mkdir(parents=True)
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        (root / name).write_text("# " + name[:-3] + "\n", encoding="utf-8")
    return personas.PersonaRepository(owner_home=root, soul_path=root / "SOUL.md",
        user_path=root / "USER.md", agents_path=root / "AGENTS.md")


# LLM: 默认值与随包YAML必须相等，显式用户覆盖不得被核心提示压缩改写。
# 函数用途: 核对核心提示体积、安全关键词及配置一致性。
def test_core_and_shipped_yaml_stay_equal_and_compact():
    text = AgentConfig().system_prompt
    assert load_simple_yaml(PACKAGE / "config/agent_config.yaml")["system_prompt"] == text
    assert len(text) < 1400
    assert "my-agent" in text and "安全" in text and "未验证" in text
    assert AgentConfig(system_prompt="explicit custom core").system_prompt == "explicit custom core"


# LLM: 同时读取公开schema与文本，不能靠虚构confirmed参数使提示测试通过。
# 函数用途: 核对USER自主、AGENTS只读与SOUL宿主审批的文字合同。
def test_persona_guidance_obeys_actual_schema_and_agents_read_only():
    default = (PACKAGE / "prompts/default.md").read_text()
    spec = persona_tool.build_update_persona_model_spec()
    assert "自主维护 AGENTS" not in default and "target=agents` 自主" not in default
    assert "/agents" in default and "只读" in default
    assert "confirmed=true" not in default
    assert "soul" in default and "审批" in default
    assert "AGENTS" in spec.hints.deferred_summary and "只读" in spec.hints.deferred_summary
    assert "confirmed" not in spec.input_schema["properties"]


# LLM: 覆盖部署cwd和包根两种别名路径，断言只注入一次真实随包提示。
# 函数用途: 核对配置与extra_files里的builtin/legacy重复项。
@pytest.mark.parametrize("package_root", [False, True])
def test_prompt_builtin_legacy_alias_and_config_extra_are_read_once(tmp_path, package_root):
    builder = prompts.PromptBuilder(AgentConfig(prompt_files=["prompts/default.md"]), PACKAGE if package_root else tmp_path)
    chunks = builder.read_prompt_files(["builtin:prompts/default.md", "builtin:prompts/default.md"])
    assert len(chunks) == 1
    assert chunks[0].startswith("# Prompt File: builtin:prompts/default.md\n")


# LLM: 用同一临时文件的相对、绝对和含..路径核对来源身份，不按内容合并。
# 函数用途: 确保本地路径别名去重后仍按首次出现顺序输出。
def test_prompt_canonical_local_alias_preserves_first_seen_order(tmp_path):
    (tmp_path / "a.md").write_text("first")
    (tmp_path / "b.md").write_text("second")
    (tmp_path / "nested").mkdir()
    builder = prompts.PromptBuilder(AgentConfig(prompt_files=["a.md", "b.md"]), tmp_path)
    chunks = builder.read_prompt_files([str(tmp_path / "a.md"), "nested/../b.md"])
    assert len(chunks) == 2 and chunks[0].endswith("first") and chunks[1].endswith("second")


# LLM: 使用真实prepare入口分别验证主会话与两种子域，不丢Owner归属边界。
# 函数用途: 确保仅主会话保留目录教程，所有作用域保留工作区事实。
@pytest.mark.parametrize("scope", ["default", "task_local", "control_plane"])
def test_task_local_omits_home_tutorial_but_keeps_owner_and_workspace(tmp_path, scope):
    home = SimpleNamespace(owner_home_dir=tmp_path, owner_kind="user")
    builder = prompts.PromptBuilder(AgentConfig(prompt_files=[]), tmp_path, home_paths=home)
    prepared = builder.prepare_render_input(prompts.PromptBuildRequest("work", [], context_scope=scope))
    assert "这个用户的私人空间" in prepared.owner_scope
    assert str(tmp_path) in prepared.workspace_context
    assert ("# 家目录与整理约定" in prepared.owner_scope) is (scope == "default")


# LLM: 仅替换失败的外部索引读口；完整提示渲染须显式降级且不泄露异常信息。
# 函数用途: 验证普通和已选方法目录故障都不会冒充空库。
@pytest.mark.parametrize("selected", [None, ("builtin:some-method",)])
def test_skill_catalog_failure_is_visible_but_does_not_leak_error(tmp_path, selected):
    # LLM: 固定异常含敏感路径标识，不读取实际目录。
    # 类用途: 模拟方法索引构造失败。
    class BrokenRouter:
        # LLM: 只触发目录失败分支，不依赖服务或磁盘状态。
        # 函数用途: 抛出应被安全降级而非回显的异常。
        def render_skill_metadata_index(self, **kwargs):
            raise OSError("PRIVATE/owner/secret")
    builder = prompts.PromptBuilder(AgentConfig(prompt_files=[]), tmp_path)
    builder.capability_router = BrokenRouter()
    prepared = builder.prepare_render_input(prompts.PromptBuildRequest("do it", [],
        tools=prompts.ToolSections(selected_skill_ids=selected)))
    rendered = prompts.render_prepared_prompt(prepared)
    assert "catalog_state: degraded" in rendered
    assert "skill_search" in rendered and "PRIVATE" not in rendered
    assert "do it" in rendered


# LLM: 逐行对照原文而非只测长度，避免切断一条规则仍被判通过。
# 函数用途: 验证人格预算截断保留完整首尾规则。
def test_persona_truncation_retains_complete_lines_on_both_sides():
    lines = [f"- rule-{i:02d}: " + "约束内容" * 8 + "\n" for i in range(20)]
    text, truncated = personas._bounded_prompt_content("".join(lines), 350)
    assert truncated and len(text) <= 350 and MARKER in text
    head, tail = text.split(MARKER)
    assert head and tail
    assert all(line in lines for line in head.splitlines(keepends=True))
    assert all(line in lines for line in tail.splitlines(keepends=True))


# LLM: 极小预算也必须遵守硬上限，放得下时保留原样marker。
# 函数用途: 覆盖零预算、标记边界和超长单行的字符兜底。
@pytest.mark.parametrize("budget", [0, 1, 20, len(MARKER), 100, 256])
def test_persona_budget_is_a_hard_character_bound(budget):
    result, truncated = personas._bounded_prompt_content("x" * 1000, budget)
    assert truncated and len(result) <= budget
    if budget >= len(MARKER):
        assert MARKER in result


# LLM: 冻结真实旧版原文并核对字节、mtime和账本，不能以写回冒充只读投影。
# 函数用途: 验证旧个人与群出厂种子安全显示新版且保留原件。
@pytest.mark.parametrize("group", [False, True])
def test_legacy_factory_seed_is_projected_without_writing_source(tmp_path, group):
    repository = _repo(tmp_path / "owner")
    original = LEGACY_GROUP_AGENTS if group else LEGACY_AGENTS
    path = repository.paths["agents"]
    path.write_bytes(original.encode())
    before = path.stat().st_mtime_ns
    snapshot = repository.load("agents")
    expected = seeds.GROUP_AGENTS_TEMPLATE if group else seeds.AGENTS_TEMPLATE
    assert snapshot.content == expected
    assert snapshot.content != original
    assert snapshot.diagnostic.sha256 == hashlib.sha256(original.encode()).hexdigest()
    assert "factory_seed" in snapshot.diagnostic.detail
    assert path.read_bytes() == original.encode() and path.stat().st_mtime_ns == before
    assert not repository.versions_path.exists()


# LLM: 任何原始字节变化均视作自定义，不允许strip或相似匹配。
# 函数用途: 确保追加规则和纯空白修改均不被工厂视图覆盖。
@pytest.mark.parametrize("suffix", ["\n", "\n- 用户定制规则:不可覆盖\n", " "])
def test_customized_factory_seed_is_never_projected(tmp_path, suffix):
    repository = _repo(tmp_path / "owner")
    raw = LEGACY_AGENTS + suffix
    repository.paths["agents"].write_bytes(raw.encode())
    assert repository.load("agents").content == raw
    assert repository.paths["agents"].read_bytes() == raw.encode()


# LLM: 已存在的账本/来源配置即使为空或损坏也不推断无用户历史。
# 函数用途: 验证工厂识别对可疑历史保守保留旧内容。
@pytest.mark.parametrize("ledger", ["", '{"target":"agents","version":1}\n', "unreadable ledger data"])
@pytest.mark.parametrize("name", ["versions.jsonl", "agents_source.json"])
def test_any_existing_version_ledger_prevents_factory_projection(tmp_path, ledger, name):
    repository = _repo(tmp_path / "owner")
    repository.paths["agents"].write_bytes(LEGACY_AGENTS.encode())
    repository.versions_path.parent.mkdir()
    (repository.versions_path.parent / name).write_text(ledger)
    assert repository.load("agents").content == LEGACY_AGENTS


# LLM: 匹配摘要必须来自原始字节，不因显示换行归一化把修改版认作出厂版。
# 函数用途: 确保CRLF改动不触发工厂投影且磁盘字节不变。
def test_factory_crlf_bytes_are_not_the_exact_known_seed(tmp_path):
    repository = _repo(tmp_path / "owner")
    data = LEGACY_AGENTS.replace("\n", "\r\n").encode()
    repository.paths["agents"].write_bytes(data)
    snapshot = repository.load("agents")
    assert snapshot.content == LEGACY_AGENTS and "factory_seed" not in snapshot.diagnostic.detail
    assert repository.paths["agents"].read_bytes() == data


# 基线529f4df原始出厂字节的冻结夹具，不从待测新模板生成期望。
LEGACY_AGENTS = '# AGENTS\n<!-- 你与用户之间的长期工作约定:默认怎么干活、什么算干完。简短要点;不写可执行命令/脚本,不试图覆盖系统或安全规则。某项目专属规则放该项目目录的 AGENTS.md。 -->\n\n## 干活方式\n- 默认直接把任务做完,而非只给计划(除非用户要的是方案讨论)。\n- 多步任务先列简短计划再执行;简单任务直接做。\n- 多步 / 建东西的任务用 task_progress 登记待办、随做随勾(真做完才标 done——测试没过 / 只写一半都不算);待办没清空时继续推进或如实说明阻塞，不要声称已经完成。\n- 遇阻先自己尝试,卡住再问;提问带已知信息和具体选项。\n\n## 响应优先\n- 重活、长活派子代理并行去做;你自己保持轻量协调者姿态——干活期间也能随时回用户的新消息,别埋头把对话堵死。\n- 子代理在后台推进,有进展或完成了再回来向用户汇总。\n- 派子代理干大活时:先想清楚这活能拆成哪几块【能并行的独立部分】(比如建一个系统=后端 / 前端 / 数据 / 测试),然后【一块派一个子代理、一次把该派的都派齐】——别只派 1 个把整个活原样扔给它(那等于没拆、它一个人扛不动就烂尾),更别自己写份计划 / 架构 / 规格文档就当交付(那不是成品)。等子代理都干完,你再统筹整合成一个【真能跑的成品】、亲手跑通验证后交付。\n- 子代理的进展、阻塞和完成由宿主以生命周期事件送回直接父级;不派新代理查进度,不用 shell sleep 或状态工具轮询。\n\n## 输出\n- 先结论、后细节;长内容用要点和小标题;给结论附依据/来源或文件位置;用用户默认语言回复。\n\n## 完成标准\n- 交付前自查:是否答了原问题、是否可验证、有无遗漏;涉及代码能跑/能测优先,并说明如何验证;有假设或不确定处明确标出。\n- 用户要你「做 / 建 / 开发一个能跑的东西」(网站 / 系统 / 工具等),交付物必须是【真能跑的成品】(把它建出来、亲手跑通),不是规格 / 架构 / 计划文档——只写一份文档就宣布完成 = 没完成。该派子代理去建就派、别拿文档顶交付。\n\n## 协作\n- 不可逆/高风险动作前先确认;值得长期保留的偏好与教训写进记忆,不堆在本文件。\n'
LEGACY_GROUP_AGENTS = '# AGENTS\n<!-- 代理与这个群组之间的长期工作约定。群组事实不能冒充某位成员的私人指令。 -->\n\n## 干活方式\n- 默认直接把任务做完,而非只给计划(除非群组当前要的是方案讨论)。\n- 多步任务先列简短计划再执行;简单任务直接做。\n- 遇阻先自己尝试,卡住再向当前会话说明已知信息和具体选项。\n\n## 响应优先\n- 重活、长活可以派子代理并行推进;主代理保持可继续接收群组新消息。\n- 子代理只向主代理汇报,由主代理基于真实进度向群组统一说明。\n\n## 输出与完成\n- 先结论、后细节;用群组当前语言回复,结论附依据或验证结果。\n- 只有真实交付和验证完成后才能说完成;未完成、失败或阻塞必须如实说明。\n\n## 协作边界\n- 群内的长期偏好、历史记忆、任务和成果属于这个群组,不等于任何成员的私人资料。\n- 群会话不能直接访问成员的私人空间;需要共享时,由成员把内容复制、上传或通过受控分享带进群空间。\n- 不可逆/高风险动作前先确认;值得长期保留的群组共识写进群组记忆。\n'


# LLM: 来源服务快照仍冻结原SHA和元数据；显式来源或已修订来源不能自动升级。
# 函数用途: 覆盖初始默认来源及用户切换后的本地/远程来源保护。
@pytest.mark.parametrize("kind,revision,upgrade", [("owner_default", 0, True), ("owner_default", 1, False), ("owner_default", 2, False), ("local_path", 0, False), ("url", 0, False)])
def test_bound_agents_source_keeps_user_revision_and_explicit_source(tmp_path, kind, revision, upgrade):
    from agent_py_agent.agent.capability.agents_source_contract import AgentsSourceSnapshot
    repository = _repo(tmp_path / "owner")
    repository.paths["agents"].write_bytes(LEGACY_AGENTS.encode())
    sha = hashlib.sha256(LEGACY_AGENTS.encode()).hexdigest()
    locator = "https://example.invalid/rules" if kind == "url" else str(repository.paths["agents"])
    source = AgentsSourceSnapshot(1, "local/main", kind, locator, 1, revision, sha, 1.0, "", "ready", "", False, False)
    repository.agents_source = SimpleNamespace(load=lambda: SimpleNamespace(content=LEGACY_AGENTS, snapshot=source),
        activation_metadata=lambda s: {"content_sha256": s.content_sha256})
    snapshot = repository.load("agents")
    assert snapshot.content == (seeds.AGENTS_TEMPLATE if upgrade else LEGACY_AGENTS)
    assert snapshot.agents_source is source and snapshot.agents_source_metadata["content_sha256"] == sha
    assert repository.paths["agents"].read_bytes() == LEGACY_AGENTS.encode()


# LLM: 行能放入总正文预算时必须调整首尾比例，不能因75/25固定分配走字符兜底。
# 函数用途: 覆盖长首行与带/不带尾换行的长末行。
@pytest.mark.parametrize("side,trailing", [("head", True), ("tail", True), ("tail", False)])
def test_persona_rebalances_budget_for_complete_edge_line(side, trailing):
    edge = "A" * 80 + ("\n" if trailing else "")
    content = edge + "b\n" * 200 if side == "head" else "b\n" * 200 + edge
    result, truncated = personas._bounded_prompt_content(content, len(MARKER) + 100)
    head, tail = result.split(MARKER)
    assert truncated and len(result) <= len(MARKER) + 100
    assert (head.startswith(edge) if side == "head" else tail.endswith(edge))
    assert all(line in content.splitlines(keepends=True) for line in (head + tail).splitlines(keepends=True))


# LLM: 两端各自能放入但不能同时放入时宁缺一端，不截断任何本可完整保留的规则。
# 函数用途: 核对首尾长行争用预算时仍输出完整行。
def test_persona_does_not_fragment_competing_complete_edge_lines():
    content = "A" * 80 + "\n" + "b\n" * 200 + "Z" * 80 + "\n"
    result, truncated = personas._bounded_prompt_content(content, len(MARKER) + 100)
    head, tail = result.split(MARKER)
    assert truncated and head + tail
    assert len(result) <= len(MARKER) + 100
    assert all(line in content.splitlines(keepends=True) for line in (head + tail).splitlines(keepends=True))


# LLM: 不同本地来源即使正文等于builtin也不合并，符号链接仅规范其真实本地来源身份。
# 函数用途: 保护真实项目覆盖和显式builtin提示同时存在的行为。
@pytest.mark.parametrize("linked", [False, True])
def test_local_project_override_remains_distinct_from_builtin(tmp_path, linked):
    (tmp_path / "prompts").mkdir()
    local = tmp_path / "prompts/default.md"
    if linked:
        target = tmp_path / "custom.md"
        target.write_text("CUSTOM PROJECT RULE")
        local.symlink_to(target)
    else:
        local.write_text("CUSTOM PROJECT RULE")
    builder = prompts.PromptBuilder(AgentConfig(prompt_files=["prompts/default.md"]), tmp_path)
    chunks = builder.read_prompt_files(["builtin:prompts/default.md"])
    assert len(chunks) == 2
    assert chunks[0].startswith(f"# Prompt File: {local.resolve()}\n")
    assert chunks[0].endswith("CUSTOM PROJECT RULE")
    assert chunks[1].startswith("# Prompt File: builtin:prompts/default.md\n")


# LLM: 原任务要求legacy默认别名与同一builtin文件只注入一次，包括指向该文件的别名链接。
# 函数用途: 锁住同一默认来源的路径别名消重，而不是按物理位置制造重复提示。
def test_legacy_default_symlink_to_builtin_is_injected_once(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/default.md").symlink_to(PACKAGE / "prompts/default.md")
    builder = prompts.PromptBuilder(AgentConfig(prompt_files=["prompts/default.md"]), tmp_path)
    chunks = builder.read_prompt_files(["builtin:prompts/default.md"])
    assert len(chunks) == 1 and chunks[0].startswith("# Prompt File: builtin:prompts/default.md\n")


# LLM: 使用真实来源仓库从原始字节加载，并核对换行定制不被SHA归一化掩盖。
# 函数用途: 验证来源服务及人格投影整条读链保持精确SHA与原件不变。
@pytest.mark.parametrize("crlf", [False, True])
def test_real_agents_source_hashes_original_bytes_before_factory_projection(tmp_path, crlf):
    from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy

    root = tmp_path / "data"
    repository = _repo(root / "owners/local/main")
    data = (LEGACY_AGENTS.replace("\n", "\r\n") if crlf else LEGACY_AGENTS).encode()
    repository.paths["agents"].write_bytes(data)
    policy = PathAccessPolicy.from_values(mode="full_access", agent_home_root=root)
    repository.agents_source = AgentsSourceRepository(owner="local/main", owner_home=repository.owner_home, path_policy=policy)
    source = repository.agents_source.load()
    assert source.snapshot.status == "ready"
    assert source.snapshot.content_sha256 == hashlib.sha256(data).hexdigest()
    assert source.content == data.decode()
    snapshot = repository.load("agents")
    assert snapshot.content == (LEGACY_AGENTS if crlf else seeds.AGENTS_TEMPLATE)
    assert ("factory_seed" in snapshot.diagnostic.detail) is (not crlf)
    assert repository.paths["agents"].read_bytes() == data

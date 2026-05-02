"""skill/tool 统一能力路由测试。"""

from pathlib import Path
import tempfile

from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.skills import SkillRegistry, parse_skill_file
from agent_py_agent.agent.tools import ToolRegistry


def test_skill_card_parsing():
    with tempfile.TemporaryDirectory() as td:
        skill_dir = Path(td) / "skills" / "python-debug"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            """---
name: python-debug
description: 排查 Python 程序报错和超时
when_to_use: 遇到 Python 异常、超时、测试失败时使用
capabilities:
  - python_debugging
  - timeout_analysis
tools_required: [read_file, search_text]
risk_level: low
---

# Python Debug

按错误信息定位代码，再做最小复现。
""",
            encoding="utf-8",
        )

        card = parse_skill_file(skill_file, source="test")

        assert card.name == "python-debug"
        assert card.capabilities == ["python_debugging", "timeout_analysis"]
        assert card.tools_required == ["read_file", "search_text"]
        assert "超时" in card.when_to_use


def test_skill_registry_and_capability_router():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skill_dir = root / "skills" / "api-check"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: api-check
description: 检查 REST API 返回和错误码
when_to_use: 用户需要测试接口、检查 HTTP 状态或分析 JSON 返回
tags: [api, http, rest]
capabilities: [api_testing]
risk_level: low
---

读取接口文档，发起最小请求，检查状态码和返回体。
""",
            encoding="utf-8",
        )

        skills = SkillRegistry([root / "skills"])
        skills.scan()
        router = CapabilityRouter(
            config=CapabilityConfig(capability_candidate_limit=3),
            skill_registry=skills,
        )

        hits = router.search("帮我检查 REST API 返回")

        assert hits
        assert hits[0].card.kind == "skill"
        assert hits[0].card.name == "api-check"


def test_tool_specs_become_capability_cards():
    registry = ToolRegistry(
        Path.cwd(),
        max_chars=6000,
        max_entries=200,
        max_matches=50,
        web_max_chars=12000,
        http_timeout=30,
        catalog_limit=20,
        retrieval_limit=3,
        vector_search_enabled=False,
        shell_tool_timeout=30,
    )
    router = CapabilityRouter(
        config=CapabilityConfig(capability_candidate_limit=2),
        tool_specs=registry.specs(),
    )

    hits = router.search("需要请求 REST API 并检查返回")
    names = [hit.card.name for hit in hits]

    assert "http_request" in names
    http_card = next(hit.card for hit in hits if hit.card.name == "http_request")
    assert http_card.kind == "tool"
    assert http_card.risk_level == "medium"
    assert "network_request" in http_card.side_effects


def test_zero_limit_means_unlimited():
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "capability_config.yaml"
        config_path.write_text(
            "capability_candidate_limit: 0\n"
            "capability_request_max_tokens: 0\n",
            encoding="utf-8",
        )
        config = load_capability_config(config_path)
    registry = ToolRegistry(
        Path.cwd(),
        max_chars=6000,
        max_entries=200,
        max_matches=50,
        web_max_chars=12000,
        http_timeout=30,
        catalog_limit=20,
        retrieval_limit=3,
        vector_search_enabled=False,
        shell_tool_timeout=30,
    )
    router = CapabilityRouter(config=config, tool_specs=registry.specs())

    hits = router.search("文件")

    assert config.capability_candidate_limit == 0
    assert config.capability_request_max_tokens == 0
    assert len(hits) > 1

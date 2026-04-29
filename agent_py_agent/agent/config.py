from __future__ import annotations

"""智能体配置加载工具。

这个模块干的事情不复杂，但很关键：
- 定义程序运行时到底有哪些配置项
- 从磁盘读取一个简化版 YAML 配置
- 把未知字段过滤掉，避免用户多写了配置就直接把程序搞崩

这里坚持只用标准库，目的是让项目在 Windows / Linux / macOS 上都能轻装运行。
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any


@dataclass
class AgentConfig:
    """运行时配置总表。

    你可以把它理解成“智能体启动前的总开关面板”。
    大到模型后端，小到工具返回长度限制，都从这里统一进来。
    """

    agent_name: str = "SimplePythonAgent"
    system_prompt: str = "你是一个谨慎、可扩展、会记录记忆、会在必要时调用工具的 Python CLI 智能体。先理解任务，再给出结构化回答。"
    model_backend: str = "echo"
    memory_path: str = "data/memory.jsonl"
    memory_top_k: int = 5
    auto_save_memory: bool = True
    enable_self_learning: bool = False
    prompt_files: list[str] = field(default_factory=list)
    enable_subagents: bool = True
    max_subagents: int = 5
    subagent_workspace: str = "data/subagents"
    task_max_subagents: int = 0
    task_max_grandchildren: int = 0
    scheduler_mode: str = "auto"
    runner_concurrency: str = "auto"
    runner_start_rate: str = "auto"
    runner_timeout_seconds: str = "auto"
    runner_failure_policy: str = "auto"
    daemon_planner: bool = True
    daemon_apply: bool = False
    daemon_execute_runners: bool = False
    daemon_interval: int = 30
    daemon_max_runners: str = "auto"
    daemon_limit: int = 0
    daemon_max_cycles: int = 0
    daemon_max_cards: int = 0
    daemon_probe: bool = True
    daemon_reviewer: str = "parent-daemon"
    daemon_runner_instruction: str = ""
    log_level: str = "info"
    extensions_dir: str = "extensions"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "AGENT_API_KEY"
    model_name: str = "gpt-4o-mini"
    request_timeout: int = 60
    max_tokens: int = 1024
    temperature: str = "0.2"
    anthropic_version: str = "2023-06-01"
    enable_tools: bool = True
    max_tool_rounds: int = 5
    tool_read_max_chars: int = 6000
    tool_list_max_entries: int = 200
    tool_search_max_matches: int = 50
    tool_web_max_chars: int = 12000
    tool_http_timeout: int = 30
    tool_catalog_limit: int = 20
    tool_retrieval_limit: int = 3
    tool_vector_search_enabled: bool = False


def parse_scalar(value: str) -> Any:
    """解析一个简单标量值。

    这里支持的类型很克制：
    - `true/false`
    - 整数
    - 其他内容按普通字符串处理

    这样做的好处是规则简单，坏处是 YAML 能力有限。
    对这个小项目来说，够用比花哨更重要。
    """

    value = value.strip().strip('"').strip("'")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def load_simple_yaml(path: Path) -> dict[str, Any]:
    """读取一个极简 YAML 子集。

    这不是完整 YAML 解析器，只支持当前项目要用到的结构：
    - `key: value`
    - `key:` 后面接列表项

    大白话说，就是“够项目自己吃，不追求兼容所有 YAML 花样”。
    """

    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, []).append(parse_scalar(line[4:]))
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value == "":
                data[key] = []
                current_key = key
            else:
                data[key] = parse_scalar(value)
                current_key = None
    return data


def load_config(config_path: str | Path) -> AgentConfig:
    """从磁盘加载配置，并过滤未知字段。

    这样用户即使先写了某些未来配置项，旧版本程序也不会立刻炸掉。
    """

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = load_simple_yaml(path)
    allowed = set(AgentConfig.__dataclass_fields__.keys())
    clean = {key: value for key, value in raw.items() if key in allowed}
    config = AgentConfig(**clean)

    # 优先从环境变量读取密钥。
    # 大白话解释：仓库里只留“去哪里拿 key”的说明，不再把真 key 写进代码仓库。
    env_name = str(config.api_key_env).strip()
    if env_name:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            config.api_key = env_value

    return config

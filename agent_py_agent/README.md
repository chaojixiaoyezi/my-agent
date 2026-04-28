# Simple Python Agent

一个只依赖 Python3 标准库的 CLI 智能体骨架。

## 快速开始

```bash
python3 -m agent_py_agent --help
python3 -m agent_py_agent run "记住：我喜欢清晰的表格" --save
python3 -m agent_py_agent memory-list
python3 -m agent_py_agent run "请根据我的偏好回答" --inject "这次回答要短"
python3 -m agent_py_agent spawn-subagents "开发一个带记忆的CLI智能体" --count 3
```

## 特性
- 外置配置：`config/agent_config.yaml`，每个参数有中文说明。
- JSONL 记忆：`data/memory.jsonl`。
- 动态 prompt 注入：配置 prompt、`--prompt-file`、`--inject`。
- subagent：拆任务并记录 thought/plan/result。
- 后端可扩展：默认 `echo`，未来可接 OpenAI/本地模型/HTTP。

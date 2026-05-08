# LLM: Recovery output writers keep persistence.save focused on orchestration, not file-format details.
# 模块用途: 集中写入 checkpoint artifacts 和 takeover readiness 文件，避免 persistence 主流程继续膨胀。

from __future__ import annotations

"""Recovery output file writers for subagent persistence."""

import json
from pathlib import Path

from ..models import SubAgentTask
from .takeover_readiness import write_takeover_readiness_files


# LLM: write_recovery_output_files writes compact recovery refs and packet files after workspace sync.
# 函数用途: 保存恢复链路需要的 JSON/Markdown 文件；只写结构化恢复信息，不展开 artifact 正文。
def write_recovery_output_files(task: SubAgentTask, checkpoint_artifacts: dict[str, object]) -> None:
    for field_name, artifact_payload in checkpoint_artifacts.items():
        _write_checkpoint_artifact(getattr(task, field_name, ""), artifact_payload)
    write_takeover_readiness_files(task)


# LLM: _write_checkpoint_artifact writes one recovery artifact in either text or JSON form.
# 函数用途: 根据 payload 类型保存 checkpoint 产物，空路径时安全跳过。
def _write_checkpoint_artifact(path_text: str, artifact_payload: object) -> None:
    if not path_text:
        return
    path = Path(path_text)
    if isinstance(artifact_payload, str):
        path.write_text(artifact_payload, encoding="utf-8")
        return
    path.write_text(json.dumps(artifact_payload, ensure_ascii=False, indent=2), encoding="utf-8")

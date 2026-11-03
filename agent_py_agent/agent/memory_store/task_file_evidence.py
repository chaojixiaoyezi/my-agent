# LLM: 仅关联用户明确指针与同请求成功read_file的冻结归档；不重读源文件、不把输出或模型文字当授权。
# 模块用途: 为策展消息补充有界的、可核验的用户任务文件证据。
"""读取的是当前owner已有工具归档，不是用户指针当前指向的文件。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath


# LLM: 只做无IO的绝对路径归一，不以消费时符号链接证明读取时的身份；相对路径无宿主cwd保持未知。
# 函数用途: 从用户原话提取绝对文件指针，不访问这些文件。
def explicit_local_paths(text: str) -> set[str]:
    pattern = r'''`(/[^`\n]+)`|"(/[^"\n]+)"|'(/[^'\n]+)'|(?<![\w:/])(/[^\s`"'<>，。；、）)]+)'''
    result = set()
    for groups in re.findall(pattern, text):
        path = next((part for part in groups if part), "").rstrip(".,;:")
        try:
            result.add(os.path.normpath(path))
        except (OSError, ValueError, RuntimeError):
            continue
    return result


# LLM: 身份逐项核audit/索引；路径只比较冻结请求和用户的词法规范值，不借当前链接绑定另一个历史读取。
# 函数用途: 返回一次匹配读取的有界证据，缺失/越界/损坏或不完整归档一律无证据。
def _read_evidence(event, owner_root: Path, paths: set[str], *, max_chars: int, max_archive_bytes: int = 1_048_576):
    try:
        root = Path(owner_root).absolute()
        artifact = Path(event.artifact_ref).absolute()
        parts = artifact.relative_to(root).parts
        raw = read_bytes_beneath(root, parts, max_bytes=max_archive_bytes, require_dir_fd=True)
        payload = json.loads(raw) if raw else None
        if not isinstance(payload, dict) or not _same_read(payload, event):
            return None
        params = payload.get("parameters")
        path = params.get("path") if isinstance(params, dict) else None
        if not isinstance(path, str) or not Path(path).is_absolute():
            return None
        resolved = os.path.normpath(path)
        if resolved not in paths:
            return None
        content = payload.get("content")
        if not isinstance(content, str):
            return None
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if (digest != str(payload.get("sha256") or "").removeprefix("sha256:")
                or f"sha256:{digest}" != event.artifact_hash
                or len(encoded) != payload.get("size_bytes") or len(encoded) != event.artifact_size_bytes):
            return None
        return {"path": resolved, "sha256": digest, "excerpt": content[:max_chars],
                "tool_call_id": event.tool_call_id, "origin": "user_referenced_file",
                # read_file可以是局部窗口：这是冻结工具输出的摘要，绝不冒称整份文件hash。
                "hash_scope": "verified_read_output", "artifact_ref": event.artifact_ref}
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError):
        return None


# LLM: 不读取工具输出中的指令来判断资格；签名式身份字段与audit严格一致，失败调用不能由索引ok洗白。
# 函数用途: 确认归档属于本次成功read_file调用。
def _same_read(payload: dict, event) -> bool:
    request = str(payload.get("request_id") or payload.get("conversation_request_id") or "")
    return (event.is_verified_success() and event.tool_name == "read_file"
            and payload.get("kind") == "tool_output" and payload.get("tool") == "read_file"
            and payload.get("ok") is True and payload.get("call_id") == event.tool_call_id
            and payload.get("run_id") == event.run_id and bool(event.request_id) and request == event.request_id)


# LLM: 精确请求与线程均匹配且处于用户输入之后、下条输入之前；保留原消息顺序及游标，不从后续轮借证据。
# 函数用途: 在单文件和整批JSON预算内附加用户文件证据。
def attach_task_file_evidence(batch, owner_root: Path, *, max_chars: int = 4000, max_input_chars: int = 40_000):
    if max_chars <= 0:
        return batch
    messages = list(batch.messages)
    for index, message in enumerate(messages):
        if message.role != "user" or not (paths := explicit_local_paths(message.full_content)):
            continue
        request = str(message.metadata.get("request_id") or message.metadata.get("gateway_request_id")
                      or message.metadata.get("conversation_request_id") or "")
        next_input = min((item.created_at for item in messages if item.role == "user"
            and item.thread_id == message.thread_id and item.created_at > message.created_at), default=float("inf"))
        evidence = []
        for event in batch.audit_events:
            if event.thread_id != message.thread_id or not request or event.request_id != request:
                continue
            try:
                when = datetime.fromisoformat(event.created_at.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
            if not message.created_at <= when < next_input or not event.artifact_ref:
                continue
            item = _read_evidence(event, owner_root, paths, max_chars=max_chars)
            if item is None or any(prior["path"] == item["path"] for prior in evidence):
                continue
            candidate = replace(message, referenced_files=(*evidence, item))
            trial = replace(batch, messages=(*messages[:index], candidate, *messages[index + 1:]))
            if len(json.dumps(trial.to_model_payload(), ensure_ascii=False)) <= max_input_chars:
                evidence.append(item)
                messages[index] = candidate
    return replace(batch, messages=tuple(messages))

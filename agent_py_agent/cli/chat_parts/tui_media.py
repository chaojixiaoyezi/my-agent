# LLM: 此模块只把明确的 /attach、文件拖入或剪贴板动作转换为草稿 refs；不得从普通消息中扫描路径并私自上传。
# 模块用途: 在后台导入图片/视频，让输入框保留可删除的附件占位符；原始字节只存 owner 私有目录。
from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from ...agent.conversation.input_media import (
    import_input_media,
    input_media_root,
    media_type_for_path,
    validate_input_media,
)


# LLM: marker 只关联本草稿中已登记的 ref；用户手打相同格式不创建附件或读取权限。
# 类用途: 保存可随草稿暂存、删除和恢复的附件引用。
@dataclass(frozen=True)
class TuiMediaRef:
    placeholder: str
    ref: dict


# LLM: 只有整个粘贴块均为实际图片/视频文件路径才解释为拖入；混合普通句子仍是原文。
# 函数用途: 识别终端拖入的引号路径、反斜杠空格或 file URL。
def pasted_media_paths(text: str) -> tuple[Path, ...]:
    try:
        tokens = shlex.split(text.strip())
        paths = tuple(Path(unquote(urlsplit(token).path) if token.startswith("file://") else token).expanduser()
                      for token in tokens)
        if not paths or not all(path.is_file() for path in paths):
            return ()
        for path in paths:
            media_type_for_path(path)
        return paths
    except (ValueError, OSError):
        return ()


# LLM: 只有草稿携带的 refs 可提交；删除 marker 会移除附件，不扫描自然语言或生成额外模型指令。
# 函数用途: 提取当前仍可见的附件，并从模型正文移除展示占位符。
def draft_media(draft: object, text: str) -> tuple[str, tuple[dict, ...]]:
    refs = []
    for item in draft.media_refs:
        if item.placeholder in text:
            refs.append({**item.ref, "placeholder": item.placeholder})
            text = text.replace(item.placeholder, "")
    return text.strip(), tuple(refs)


# LLM: 导入走后台线程且有总量上限；成功后才修改草稿，不自动发消息。失败保留现有输入，并明确提示。
# 函数用途: 添加本机媒体文件，在磁盘慢时保持 TUI 可滚动和退出。
async def import_tui_media(event, params, paths: tuple[Path, ...], *, media_type: str = "") -> None:
    params.media_importing = True
    runtime = params.tui_runtime
    runtime.set_notice("正在添加附件…", duration_seconds=30)
    try:
        root = input_media_root(params.agent)
        limit = params.agent.config.input_media_max_bytes
        count = params.agent.config.input_media_max_files
        draft = params.interaction_state.capture_draft(params.input_area.text, params.input_area.buffer.cursor_position)
        _, current = draft_media(draft, draft.text)
        if len(paths) + len(current) > count or sum(path.stat().st_size for path in paths) + sum(ref["size_bytes"] for ref in current) > limit:
            raise ValueError(f"单条消息附件最多 {count} 个、合计 {limit // 1024 // 1024} MiB。")
        refs = []
        for path in paths:
            refs.append(await asyncio.to_thread(import_input_media, path, root, max_bytes=limit, media_type=media_type))
        validate_input_media((*current, *refs), root=root, max_bytes=limit, max_files=count)
        for ref in refs:
            label = f"[附件 {uuid4().hex[:8]}: {ref['name']}]"
            params.interaction_state.register_media(TuiMediaRef(label, ref))
            params.input_area.buffer.insert_text(label + " ")
        runtime.set_notice("附件已添加；输入问题后发送。删除占位符可移除附件。", duration_seconds=4)
    except (OSError, ValueError) as exc:
        runtime.set_notice(f"附件未添加：{exc}", duration_seconds=5)
    finally:
        params.media_importing = False
        event.app.invalidate()


# LLM: /attach 是明确的本地输入动作；此处只修改草稿，不直接向 Gateway 发起模型任务。
# 函数用途: 处理附件命令，支持带空格路径和显式 MIME。
def handle_attach_command(event, params, text: str) -> bool:
    if text != "/attach" and not text.startswith("/attach "):
        return False
    try:
        args = shlex.split(text)[1:]
        media_type = ""
        if args[:1] == ["--type"] and len(args) >= 3:
            media_type, args = args[1], args[2:]
        if not args:
            raise ValueError('用法：/attach "/图片或视频路径"；也可直接拖入文件。')
        paths = tuple(Path(arg).expanduser() for arg in args)
        for path in paths:
            media_type_for_path(path, media_type)
            if not path.is_file():
                raise ValueError(f"找不到文件：{path.name}")
        params.input_area.buffer.reset()
        event.app.create_background_task(import_tui_media(event, params, paths, media_type=media_type))
    except ValueError as exc:
        params.tui_runtime.set_notice(str(exc), duration_seconds=5)
    event.app.invalidate()
    return True

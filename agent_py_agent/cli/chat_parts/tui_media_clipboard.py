# LLM: 只有用户显式 Ctrl-V 才读取系统图片剪贴板；不轮询、不监听，临时文件仅用于入站媒体导入。
# 模块用途: 用平台自带剪贴板工具读取截图；无图片时返回空，让文本粘贴保持原语义。
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# LLM: 平台命令参数固定且无 shell；读取有超时，失败清理临时文件，不回显剪贴板内容。
# 函数用途: 将显式选择粘贴的系统 PNG 图片写入私有临时文件，调用方负责导入后删除。
def clipboard_image() -> Path | None:
    with tempfile.NamedTemporaryFile(prefix="my-agent-clipboard-", suffix=".png", delete=False) as temporary:
        path = Path(temporary.name)
    try:
        if sys.platform == "darwin":
            script = ('on run argv\nset imageData to the clipboard as «class PNGf»\n'
                      'set targetFile to open for access POSIX file (item 1 of argv) with write permission\n'
                      'try\nset eof targetFile to 0\nwrite imageData to targetFile\n'
                      'close access targetFile\non error\nclose access targetFile\nend try\nend run')
            subprocess.run(["osascript", "-e", script, str(path)], check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            command = (["wl-paste", "--no-newline", "--type", "image/png"] if shutil.which("wl-paste")
                       else ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"] if shutil.which("xclip") else [])
            if not command:
                return None
            with path.open("wb") as target:
                subprocess.run(command, stdout=target, stderr=subprocess.DEVNULL, check=True, timeout=5)
        with path.open("rb") as reader:
            if reader.read(8) == b"\x89PNG\r\n\x1a\n":
                return path
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        if not path.exists() or not path.stat().st_size:
            path.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    return None

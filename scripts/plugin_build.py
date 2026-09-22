# LLM: 本模块只用于开发者显式构建受信源码，不由安装或启用调用；标准 pip/setuptools 负责 wheel，不自制分发格式。
# 模块用途: 在临时目录构建 Python wheel，读回元数据并将最终产物独占写入指定位置。

from __future__ import annotations

import os
import subprocess
import sys
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


# LLM: source 必须是调用方准备的临时受信工程；关闭索引、依赖下载及配置继承，不在产品安装期运行。
# 函数用途: 使用当前开发解释器的标准构建后端生成唯一 wheel，失败不发布产物。
def build_wheel(source: Path, wheel_dir: Path) -> Path:
    wheel_dir.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1", SOURCE_DATE_EPOCH="315532800")
    command = [sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
               "--no-cache-dir", "--wheel-dir", str(wheel_dir), str(source)]
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError("标准 wheel 构建失败：\n" + result.stdout + result.stderr)
    wheels = tuple(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("构建必须产生一个 wheel")
    return wheels[0]


# LLM: 这里只读标准元数据，不导入发行包或重建版本规则；安装前完整 RECORD/闭包仍交原插件校验器。
# 函数用途: 取得 wheel 的实际发行名、版本和依赖声明。
def wheel_metadata(path: Path):
    with ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("wheel 元数据不唯一")
        value = BytesParser().parsebytes(archive.read(names[0]))
    if not value.get("Name") or not value.get("Version"):
        raise ValueError("wheel 缺少发行身份")
    return value


# LLM: 输出必须由开发者显式选择；独占创建避免静默替换同版本文件，失败清理只限本次创建的目标。
# 函数用途: 写入已验证的最终包，不覆盖已有产物。
def publish_artifact(target: Path, content: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    stream = target.open("xb")
    try:
        with stream:
            stream.write(content)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target

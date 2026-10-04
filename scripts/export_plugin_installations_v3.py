#!/usr/bin/env python3
# LLM: 用新运行时做显式回滚导出；不覆盖安装权威、不启停 Gateway，不读取环境/凭据；确认后仅排他创建指定输出。
# 模块用途: 切回17i前将有效v4导出为v3，完整报告丢失授权，原v4必须保留备份。
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_py_agent.agent.common.strict_json import load_strict_json
from agent_py_agent.agent.plugin_installation_state import INSTALLATION_STATE_LIMIT_BYTES
from agent_py_agent.agent.plugin_permissions.rollback import export_installations_v3
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


# LLM: 参数是管理员显式文件入口，不猜真实 owner 路径；未确认只读预览，输出必须是未存在的独立文件。
# 函数用途: 解析回滚来源、独立输出和完整预览给出的确认码。
def arguments():
    parser = argparse.ArgumentParser(description="新运行时导出插件v3安装表；不会覆盖原v4，不会启停服务。")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirm", default="")
    return parser.parse_args()


# LLM: 源文件必须为无链接规范普通文件；有界单次读取，不访问配置正文之外的包/凭据，不输出私有设置。
# 函数用途: 读取这次明确选择的安装表字节，拒绝链接和超预算输入。
def read_source(path: Path) -> bytes:
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("来源必须是无链接的规范普通文件")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("来源不是普通文件")
        content = stream.read(INSTALLATION_STATE_LIMIT_BYTES + 1)
    if len(content) > INSTALLATION_STATE_LIMIT_BYTES:
        raise ValueError("来源超过安装表预算")
    return content


# LLM: 确认绑定源/输出地址及全部损失事实；仅创建新0600文件，不覆盖原表或已有导出，坏输入零输出。
# 函数用途: 显示完整回滚损失，管理员确认后导出独立v3文件；回滚部署及原表替换由管理员另行执行。
def main() -> int:
    args = arguments()
    try:
        source, output = Path(args.source).absolute(), Path(args.output).absolute()
        if source == output or output.exists() or output.is_symlink() or output.parent.resolve(strict=True) != output.parent:
            raise ValueError("输出必须是规范目录中未存在的独立文件，不能覆盖来源")
        content = read_source(source)
        payload = load_strict_json(content)
        exported, report = export_installations_v3(content, OwnerIdentity(**payload["owner"]))
        binding = {"source": str(source), "output": str(output), **report}
        code = hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        if args.confirm != code:
            print(json.dumps({"state": "confirmation_required", "confirm_code": code, **report}, ensure_ascii=False))
            return 0
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(exported)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"state": "exported", "source_unchanged": True, **report}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"state": "rejected", "reason": "rollback_export_invalid", "source_unchanged": True,
                          "message": "回滚导出未成功；未覆盖原表，请核对完整v4、规范路径及独立输出。"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

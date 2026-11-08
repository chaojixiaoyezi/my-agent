#!/usr/bin/env python3
"""离线重建 runtime.db 并输出 JSON 回执；服务停止和最终换库由操作者负责。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        self.print_usage(sys.stderr)
        self.exit(1, f"参数错误：{message}\n")


def main(argv=None) -> int:
    """返回 0 表示核心表完整，2 表示回执未通过，1 表示参数或执行入口错误。"""
    parser = _Parser(description="离线恢复运行数据库。请先停止 Gateway 及所有源库使用者；本命令不会删除文件或换库。"
        "ok 只表示核心表和目标完整性通过，事件截断请核对 events_error。"
        "mode=ro 保护源数据库正文；SQLite 仍可能创建源目录的 WAL/SHM 旁文件，请使用离线备份。")
    parser.add_argument("--source", type=Path, required=True, help="只读源 runtime.db 路径")
    parser.add_argument("--target", type=Path, required=True, help="全新目标路径，其数据库和附属文件均须不存在")
    parser.add_argument("--batch-rows", type=int, help="每批复制事件的行数，默认沿用保留扫描条目预算")
    wal_bytes = 0
    try:
        args = parser.parse_args(argv)
        try:
            wal_bytes = Path(str(args.source) + "-wal").stat().st_size
        except FileNotFoundError:
            pass
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from agent_py_agent.agent.runtime_db.salvage import salvage_runtime_db

        receipt = salvage_runtime_db(args.source, args.target, batch_rows=args.batch_rows)
    except (OSError, ValueError, sqlite3.DatabaseError) as error:
        print(json.dumps({"ok": False, "error": str(error), "source_wal_bytes": wal_bytes}, ensure_ascii=False))
        return 1
    print(json.dumps({**receipt.to_dict(), "source_wal_bytes": wal_bytes}, ensure_ascii=False))
    return 0 if receipt.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

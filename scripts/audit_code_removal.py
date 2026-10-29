# LLM: 兼容原审计脚本的--repo/--output入口，所有核验唯一委托完整删除门，不保留第二套宽松规则。
# 模块用途: 为现有验收脚本保留旧文件名，输出与退出码沿统一删除门。
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_optimization_removal import main as verify_main  # noqa: E402


# LLM: 只转交原argv，不把失败或待复核改成通过；报告输出仍由同一边界验证。
# 函数用途: 运行旧名称对应的统一核验入口。
def main(argv=None):
    return verify_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

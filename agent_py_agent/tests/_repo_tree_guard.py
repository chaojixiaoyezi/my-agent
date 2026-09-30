"""测试会话的仓库树防线：测试不能往仓库树里写东西。

守两件事：起跑目录（pytest 启动时的 cwd，一般就是仓库根）下的 MagicMock/，以及仓库根被跟踪的 CODE_SIZE_REPORT.md。
MagicMock/：测试把 MagicMock 当路径用时（典型是 agent.home_paths.owner_home_dir），Path(mock) 是相对路径 "MagicMock/..."，
产品代码照常写盘，就会写进仓库根。
git 检出里它被 .gitignore 挡住看不见；导出目录里没有 .git，架构守卫 test_runtime_artifacts_are_not_present_in_tracked_files
会把它当运行产物报出来。

CODE_SIZE_REPORT.md：scripts/check_code_size.py 默认把报告写在仓库根，测试在仓库根跑它就会改写这个被跟踪文件；
只查这一个文件，不扩成全仓扫描。
conftest 的 autouse 夹具在每条测试前后各取一次指纹：新出现或有变化就让这条测试报错；这条测试新建的整个目录顺手删掉，
不连累后面的用例；会话开始前就有的残留不删，只比对变化。
防线只按文件系统事实判断，不读内容；取指纹只用导入时抓好的 os 底层函数：测试常替换 pathlib 或 os 上的函数当 IO 哨兵，
防线若走这些接口，会被哨兵当成违规或读到伪造的结果。
"""
from __future__ import annotations

import os
import shutil

# 导入时抓好的底层函数；测试把 os 模块上的同名属性换掉也影响不到这些引用。
_OS_LSTAT, _OS_SCANDIR = os.lstat, os.scandir

MAGICMOCK_DIR_NAME = "MagicMock"
TRACKED_REPORT_NAME = "CODE_SIZE_REPORT.md"
# 仓库根按本文件位置算（agent_py_agent/tests/ 的上两级），与 scripts/check_code_size.py 的 ROOT 同口径，不随 cwd 变。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# LLM: 只做 lstat 与一层 scandir，不递归、不读内容；不存在返回 None，存在时返回它自己和各直接子项的 (名字, mtime_ns)。
#   新增或删除直接子项会改变子项列表，往已有子目录里再建目录会改变那个子目录的 mtime，两者都会让指纹变化。
# 函数用途: 取起跑目录下 MagicMock/ 的指纹，供测试前后比对。
def magicmock_fingerprint(root: str) -> tuple[int, tuple[tuple[str, int], ...]] | None:
    path = os.path.join(root, MAGICMOCK_DIR_NAME)
    try:
        top = _OS_LSTAT(path)
    except FileNotFoundError:
        return None
    return top.st_mtime_ns, _child_mtimes(path)


# LLM: 只列一层，按名字排序；目录在两次调用之间被删掉或不是目录时返回空元组，不抛异常。
# 函数用途: 取一个目录下各直接子项的 (名字, mtime_ns)。
def _child_mtimes(path: str) -> tuple[tuple[str, int], ...]:
    try:
        with _OS_SCANDIR(path) as entries:
            return tuple(sorted((entry.name, entry.stat(follow_symlinks=False).st_mtime_ns) for entry in entries))
    except (FileNotFoundError, NotADirectoryError):
        return ()


# LLM: 测试后存在且与测试前不同即违规；测试前后都不存在、或残留原样未动都不算。只比较指纹，不看是谁写的。
# 函数用途: 判断一条测试期间 MagicMock/ 有没有被新建或写入。
def magicmock_violation(
    before: tuple[int, tuple[tuple[str, int], ...]] | None,
    after: tuple[int, tuple[tuple[str, int], ...]] | None,
) -> bool:
    return after is not None and after != before


# LLM: 只在这条测试之前不存在时调用，删掉的是这条测试自己新建的目录；会话开始前的残留由调用方保证不删。
# 函数用途: 删掉一条测试新建的 MagicMock/，不连累后面的用例；会删文件。
def remove_magicmock_dir(root: str) -> None:
    shutil.rmtree(os.path.join(root, MAGICMOCK_DIR_NAME), ignore_errors=True)


# 函数用途: 生成违规测试的报错文字，写明位置、是否已清理和改法。
def magicmock_failure_message(nodeid: str, root: str, *, created: bool) -> str:
    where = os.path.join(root, MAGICMOCK_DIR_NAME)
    cleanup = "已删除本条测试新建的目录。" if created else "目录在本条测试之前就有，只报变化、不删除。"
    return (f"测试 {nodeid} 往起跑目录写了 {where}：把 MagicMock 当成路径用了（典型是 agent.home_paths.owner_home_dir）。"
            f"给替身配真实路径（如 tmp_path），或在测试里 monkeypatch.chdir(tmp_path)。{cleanup}")


# LLM: 只做一次 lstat，取 (大小, mtime_ns)；不读内容。不存在返回 None（导出目录可能没有这个文件）。
# 函数用途: 取仓库根被跟踪报告文件的指纹，供测试前后比对。
def tracked_report_fingerprint(repo_root: str) -> tuple[int, int] | None:
    try:
        stat = _OS_LSTAT(os.path.join(repo_root, TRACKED_REPORT_NAME))
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns


# 函数用途: 生成改写了被跟踪报告的测试的报错文字，写明位置和改法；不自动恢复内容。
def tracked_report_failure_message(nodeid: str, repo_root: str) -> str:
    where = os.path.join(repo_root, TRACKED_REPORT_NAME)
    return (f"测试 {nodeid} 改写了被跟踪的 {where}（多半是在仓库根跑了 scripts/check_code_size.py）。"
            f"用 --report 把报告写到 tmp_path；本地可用 git checkout -- {TRACKED_REPORT_NAME} 恢复。")

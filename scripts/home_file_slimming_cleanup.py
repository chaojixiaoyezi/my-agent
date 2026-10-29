# LLM: 独立一次性清理器；只认五类白名单，默认预演；删除沿原目录FD且不跟随链接，保护规则须同步回归测试。
# 模块用途: 生成可审计的历史垃圾清单，核验后删除明确获准的中间件，不处理在线数据库或正式数据。
"""保守清理：默认dry-run；apply可用--approved-manifest限制为已审阅集合。

清单记录逻辑字节和路径累计分配块，不承诺APFS快照/克隆后的物理释放量。
归档只认--home-dir/archive-before-20261019，不自动寻找或扩大到其他位置。
发布备份只移除成套的明确备份文件，保留同目录的日志、报告、安装包和环境。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DAY = 86400
OWNER = Path("owners/local/main")
CATEGORIES = (
    "dangling_orphan_agent_projections",
    "task_idle_tmp_and_empty_dirs",
    "stale_test_workspaces",
    "old_release_switch_backups",
    "archive_before_20261019_scratch_only",
)
PROTECTED_DIRS = {"output", "inputs", "src", "messages", "memory", "long_term", "skills", ".git"}
PROTECTED_FILES = {
    "memories.jsonl",
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "RESULT.md",
    "FINAL_REPORT.md",
}
BACKUP = re.compile(
    r"^(?:pre-switch-conversations(?P<tar>[-_.0-9TZa-z]*)\.tar\.gz|"
    r"runtime-before(?P<db>[-_.0-9TZa-z]*)\.db(?:-(?:wal|shm))?)$"
)


# LLM: 元数据指纹只读取文件身份，不读取正文；目录删除后mtime会改变，执行时须区分目录身份与文件版本。
# 函数用途: 保存设备、inode、类型、链接数、大小及纳秒时间，供预演和删除前核对。
def _identity(value):
    return [
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    ]


# LLM: 从已解析的home逐级O_NOFOLLOW打开；不把路径resolve之后的外部目标当白名单，异常交调用方失败关闭。
# 函数用途: 固定一个目录FD，避免扫描和删除跟随中途的符号链接。
@contextmanager
def _directory(home, relative=Path()):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(home, flags)
    try:
        for part in Path(relative).parts:
            if part in {"..", "."} or part.startswith("/"):
                raise ValueError("非规范相对目录")
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


# LLM: 小JSON读取沿父FD且拒绝链接、多链接、变化及过大正文；错误不能被解释为可删除资格。
# 函数用途: 安全读取孤儿定位器，最多读取一兆字节。
def _read_locator(home, relative):
    with _directory(home, relative.parent) as parent:
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 1024**2:
                raise ValueError("定位器类型或大小非法")
            data = os.read(fd, 1024**2 + 1)
            if _identity(before) != _identity(os.fstat(fd)):
                raise ValueError("定位器并发变化")
            return json.loads(data)
        finally:
            os.close(fd)


# LLM: 所有保护名在整棵候选内生效；报告不因位于tmp而失去交付身份，同步检查保护参数化回归。
# 函数用途: 判断目录或文件是否必须保留。
def _protected(name):
    return name in PROTECTED_DIRS or name in PROTECTED_FILES or name.lower().endswith(".md")


# LLM: 扫描不跟随链接；保护子树仍只读元数据以核年龄，删除表不含保护项；任何读取错误使整个单元退出。
# 函数用途: 收集候选的版本摘要、实际文件数和删除用元数据，不保存文件正文。
def _scan(home, relative):
    digest = hashlib.sha256()
    nodes, protected = [], []
    newest = 0.0

    # LLM: 递归使用父FD，保护标志只会收紧；遍历目录顺序固定，以便前后摘要一致。
    # 函数用途: 逐层读取元数据，并保留可删除文件与目录的原始身份。
    def visit(parent, name, prefix, keep=False):
        nonlocal newest
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        identity = _identity(info)
        newest = max(newest, info.st_mtime)
        rel = prefix / name
        digest.update(json.dumps([str(rel), identity], ensure_ascii=False).encode())
        keep = keep or _protected(name) or stat.S_ISLNK(info.st_mode)
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            keep = True
        if keep:
            protected.append(str(rel))
        if stat.S_ISDIR(info.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if _identity(os.fstat(fd)) != identity:
                    raise ValueError("目录在扫描中被替换")
                for child in sorted(os.listdir(fd)):
                    visit(fd, child, rel, keep)
                if _identity(os.fstat(fd)) != identity:
                    raise ValueError("目录在扫描中变化")
            finally:
                os.close(fd)
        elif not stat.S_ISREG(info.st_mode):
            keep = True
        if not keep:
            nodes.append((str(rel), identity, getattr(info, "st_blocks", 0) * 512))

    with _directory(home, relative.parent) as parent:
        visit(parent, relative.name, relative.parent)
    return {
        "fingerprint": digest.hexdigest(),
        "newest_mtime": newest,
        "files": sum(stat.S_ISREG(row[1][2]) for row in nodes),
        "bytes": sum(row[1][4] for row in nodes if stat.S_ISREG(row[1][2])),
        "allocated_bytes": sum(row[2] for row in nodes if stat.S_ISREG(row[1][2])),
        "directories": sum(stat.S_ISDIR(row[1][2]) for row in nodes),
        "protected_count": len(protected),
        "protected_examples": protected[:12],
        "nodes": nodes,
    }


# LLM: 备份套组只聚合明确成员，不递归扫描/删除承载目录；成员有链接、类型异常或不完整时整组保留。
# 函数用途: 为整套备份或普通树生成相同的版本/计数视图，避免按文件删到半套后丢失集合身份。
def _scan_unit(home, candidate):
    members = candidate.get("members")
    if members is None:
        return _scan(home, Path(candidate["path"]))
    trees = [(member, _scan(home, Path(member))) for member in members]
    if any(t["files"] != 1 or t["protected_count"] or t["directories"] for _, t in trees):
        raise ValueError("备份成员不是单链接普通文件")
    result = {
        key: sum(t[key] for _, t in trees)
        for key in ("files", "bytes", "allocated_bytes", "directories", "protected_count")
    }
    result.update(
        fingerprint=hashlib.sha256(
            json.dumps([(member, t["fingerprint"]) for member, t in trees]).encode()
        ).hexdigest(),
        newest_mtime=max(t["newest_mtime"] for _, t in trees),
        protected_examples=[],
        nodes=[node for _, t in trees for node in t["nodes"]],
    )
    return result


# LLM: 只规划显式命名空间；无定位器、坏配置、未知工作区等均保留，不从报告里的估计数量推导删除。
# 类用途: 建立五类候选、保留理由及当前配置引用，不产生删除副作用。
class Planner:
    # LLM: 参数须先验证，home固定后所有相对路径沿FD访问；不导入Agent或触发迁移。
    # 函数用途: 初始化本次清理策略和只读审计容器。
    def __init__(self, home, now, age, keep):
        self.home, self.now, self.age, self.keep = home, now, age, keep
        self.today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        self.candidates = []
        self.skips = Counter()
        self.examples = {}
        self.references, self.config_text, self.config_ok = self._references()

    # LLM: 跳过原因只含元数据路径，不复制配置正文；大量投影缺定位器时只聚合计数。
    # 函数用途: 记录保留原因和有界样本。
    def skip(self, reason, path):
        self.skips[reason] += 1
        examples = self.examples.setdefault(reason, [])
        if len(examples) < 8:
            examples.append(str(path))

    # LLM: 固定深度列名通过nofollow目录链；缺目录是空范围，其它错误可审计且不降级为删除。
    # 函数用途: 列出一个已授权目录的直接子项。
    def children(self, relative):
        try:
            with _directory(self.home, relative) as fd:
                return sorted(os.listdir(fd))
        except FileNotFoundError:
            self.skip("missing_scope", relative)
        except (OSError, ValueError) as exc:
            self.skip("unreadable_or_symlink_scope:" + type(exc).__name__, relative)
        return []

    # LLM: 配置仅作额外保留依据，绝不输出正文；任一现存配置读错/符号链接时全部测试workspace清理失败关闭。
    # 函数用途: 从固定配置位置及浅层当前链接收集保护引用，不读取凭据或其他owner。
    def _references(self):
        references, texts, okay = [], [], True
        for base in (Path("config"), OWNER / "config", OWNER / "workspace/runtime"):
            try:
                with _directory(self.home, base):
                    pass
            except FileNotFoundError:
                continue
            except OSError:
                okay = False
                self.skip("configuration_parent_unreadable", base)
                continue
            for name in self.children(base):
                if name in {"admin-password.json", "shared-model-profiles.json"}:
                    continue
                path = self.home / base / name
                if path.suffix in {".yaml", ".yml", ".toml", ".json"}:
                    try:
                        with _directory(self.home, base) as parent:
                            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                            try:
                                info = os.fstat(fd)
                                if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024**2:
                                    raise ValueError("配置过大或非普通文件")
                                texts.append(os.read(fd, 4 * 1024**2 + 1).decode("utf-8"))
                            finally:
                                os.close(fd)
                    except (OSError, ValueError):
                        okay = False
                        self.skip("configuration_unreadable", base / name)
        for base in (
            Path(),
            Path("releases"),
            OWNER / "workspace",
            OWNER / "workspace/runtime",
            OWNER / "workspace/runtime/workspaces",
        ):
            for name in self.children(base):
                path = self.home / base / name
                if path.is_symlink():
                    references.append(path.resolve())
        return references, "\n".join(texts), okay

    # LLM: 显式配置文本只用于保守保护，误命中宁可少删；目标或其父/子被当前链接引用则保留。
    # 函数用途: 检查候选是否可能仍由配置或当前链接使用。
    def referenced(self, relative):
        path = self.home / relative
        return (
            str(path) in self.config_text
            or path.name in self.config_text
            or any(
                ref == path or ref.is_relative_to(path) or path.is_relative_to(ref)
                for ref in self.references
            )
        )

    # LLM: 只认workspace_locator的绝对临时路径；悬空符号链接也算尚有对象，不转用state/refs推断。
    # 函数用途: 核验一个超过一天的投影是否确属临时目标已消失的孤儿。
    def orphan(self, relative):
        try:
            obj = _read_locator(self.home, relative / "workspace_locator.json")
            root = obj.get("workspace_root") if isinstance(obj, dict) else None
            if (
                not isinstance(root, str)
                or not Path(root).is_absolute()
                or ".." in Path(root).parts
            ):
                return False
            target = Path(root)
            try:
                target.lstat()
                return False
            except FileNotFoundError:
                pass
            target = target.resolve()
            if target.is_relative_to(self.home):
                return False
            if not any(
                target.is_relative_to(prefix)
                for prefix in (
                    Path("/tmp"),
                    Path("/private/tmp"),
                    Path("/var/folders"),
                    Path("/private/var/folders"),
                )
            ):
                return False
            with _directory(self.home, relative) as fd:
                if os.fstat(fd).st_mtime >= self.now - DAY:
                    return False
                if any(
                    os.path.lexists(self.home / relative / n)
                    for n in ("RESULT.md", "FINAL_REPORT.md", "output")
                ):
                    return False
            return True
        except (OSError, ValueError, RuntimeError):
            return False

    # LLM: 单元扫描后仅收录达到年龄的可删除节点；tmp根不删，嵌套报告、输出和正式数据逐项保留。
    # 函数用途: 为一个白名单候选建立元数据清单。
    def add(self, relative, category, days=0, keep_root=False):
        try:
            tree = _scan(self.home, relative)
            if tree["newest_mtime"] >= self.now - days * DAY:
                self.skip("recent_subtree", relative)
                return
            if not tree["nodes"]:
                self.skip("no_deletable_nodes", relative)
                return
            row = {key: value for key, value in tree.items() if key != "nodes"}
            row.update(
                path=str(relative), category=category, keep_root=keep_root, min_age_days=days
            )
            self.candidates.append(row)
        except (OSError, ValueError, RecursionError):
            self.skip("unreadable_or_changing_candidate", relative)

    # LLM: tasks只走日期/slug/tmp固定深度，今天及任务根的其他兄弟目录永不作为候选。
    # 函数用途: 找出老任务的临时目录或旧归档中明确的tmp/work。
    def tasks(self, root, category, archive=False):
        for date in self.children(root):
            if date in {self.today, "2026-10-29"}:
                self.skip("today_task", root / date)
                continue
            for slug in self.children(root / date):
                for kind in ("tmp", "work") if archive else ("tmp",):
                    rel = root / date / slug / kind
                    if os.path.lexists(self.home / rel):
                        self.add(rel, category, self.age, keep_root=True)

    # LLM: 一个备份集合必须同时含会话tar和runtime数据库；只选确切成员，目录中的报告和程序不删除。
    # 函数用途: 按集合最新mtime留至少两套完整备份，规划更老的四类备份文件。
    def backups(self):
        groups = []
        release = Path("releases")
        for name in self.children(release):
            if name in {"claude-tools", "active", "current"}:
                continue
            base = release / name
            names = (
                self.children(base)
                if (self.home / base).is_dir() and not (self.home / base).is_symlink()
                else []
            )
            if not names:
                continue
            members = [base / n for n in names if BACKUP.fullmatch(n)]
            if not any(n.name.startswith("pre-switch-conversations") for n in members) or not any(
                n.name.startswith("runtime-before") and n.name.endswith(".db") for n in members
            ):
                continue
            try:
                if any(not stat.S_ISREG((self.home / p).lstat().st_mode) for p in members):
                    raise ValueError("非普通备份")
                groups.append(
                    (max((self.home / p).stat().st_mtime_ns for p in members), base, members)
                )
            except (OSError, ValueError):
                self.skip("unsafe_backup_set", base)
        # 顶层带同后缀的备份同样作为一套；不把两个文件当两套计数。
        direct = {}
        for name in self.children(release):
            match = BACKUP.fullmatch(name)
            if match:
                direct.setdefault(match.group("tar") or match.group("db") or "", []).append(
                    release / name
                )
        for suffix, members in direct.items():
            if any(p.name.startswith("pre-switch-conversations") for p in members) and any(
                p.name.endswith(".db") for p in members
            ):
                try:
                    if all(stat.S_ISREG((self.home / p).lstat().st_mode) for p in members):
                        groups.append(
                            (
                                max((self.home / p).stat().st_mtime_ns for p in members),
                                release / ("direct-set" + suffix),
                                members,
                            )
                        )
                except OSError:
                    self.skip("unsafe_backup_set", suffix)
        for index, (_mtime, base, members) in enumerate(
            sorted(groups, key=lambda row: (row[0], str(row[1])), reverse=True)
        ):
            if index < self.keep or any(self.referenced(p) for p in members):
                self.skip("newest_or_referenced_backup_set", base)
                continue
            row = dict(
                path=str(base),
                category=CATEGORIES[3],
                keep_root=True,
                min_age_days=0,
                members=[str(p) for p in sorted(members)],
            )
            try:
                tree = _scan_unit(self.home, row)
                row.update({key: value for key, value in tree.items() if key != "nodes"})
                self.candidates.append(row)
            except (OSError, ValueError):
                self.skip("unsafe_backup_set", base)

    # LLM: 归档不扩大到owner同名目录；缓存搜索不进入保护目录，也不把任意work目录当临时区。
    # 函数用途: 仅查旧归档中的显式缓存目录和孤儿投影。
    def archive(self):
        root = Path("archive-before-20261019")
        self.tasks(root / "tasks", CATEGORIES[4], archive=True)
        agents = root / "agents"
        for name in self.children(agents):
            rel = agents / name
            if self.orphan(rel):
                self.add(rel, CATEGORIES[4], 1)
            else:
                self.skip("archive_agent_not_proven_orphan", rel)
        pending = [root]
        while pending:
            base = pending.pop()
            for name in self.children(base):
                rel = base / name
                path = self.home / rel
                if _protected(name) or path.is_symlink() or not path.is_dir():
                    continue
                # tmp/work已由固定层级覆盖，不重复纳入其内部缓存。
                if name in {"tmp", "work", "agents"}:
                    continue
                if name in {"__pycache__", ".pytest_cache"}:
                    self.add(rel, CATEGORIES[4], self.age)
                else:
                    pending.append(rel)

    # LLM: 所有候选都由本轮事实规划，不接受外部清单直接授予删除权；工作区只认显式测试前缀。
    # 函数用途: 汇总五类候选及保留原因。
    def plan(self):
        for name in self.children(OWNER / "agents"):
            relative = OWNER / "agents" / name
            if self.orphan(relative):
                self.add(relative, CATEGORIES[0], 1)
            else:
                self.skip("agent_not_proven_orphan", relative)
        self.tasks(OWNER / "tasks", CATEGORIES[1])
        base = OWNER / "workspace/runtime/workspaces"
        for name in self.children(base):
            rel = base / name
            if (
                name == "main-a4e68bb5ead1"
                or not name.startswith(("test-ws-", "ws-a-"))
                or not self.config_ok
                or self.referenced(rel)
            ):
                self.skip("canonical_unknown_or_referenced_workspace", rel)
            else:
                self.add(rel, CATEGORIES[2], 1)
        self.backups()
        self.archive()
        return self.candidates


# LLM: 删除表来自同次重新扫描，路径逐层nofollow；目录只rmdir空目录，绝不递归删除未知新文件；错误回传实际已删计数。
# 函数用途: 删除一个版本未变的批准单元，逐文件记录准确逻辑字节。
def _delete_unit(home, candidate, tree, counters):
    for relative, expected, blocks in tree["nodes"]:
        path = Path(relative)
        if candidate["keep_root"] and relative == candidate["path"]:
            continue
        with _directory(home, path.parent) as fd:
            current = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(expected[2]):
                # 子项删除会改变目录mtime/ctime/大小和nlink，仅身份及目录类型须保持。
                if _identity(current)[:3] != expected[:3]:
                    raise ValueError("目录身份变化，停止该单元")
                try:
                    os.rmdir(path.name, dir_fd=fd)
                    counters["deleted_directories"] += 1
                except OSError as exc:
                    if exc.errno not in {39, 66}:  # ENOTEMPTY：保护项或新文件使目录保留
                        raise
            else:
                if _identity(current) != expected or _protected(path.name):
                    raise ValueError("文件版本变化，停止该单元")
                os.unlink(path.name, dir_fd=fd)
                counters["deleted_files"] += 1
                counters["deleted_bytes"] += expected[4]
                counters["deleted_allocated_bytes"] += blocks


# LLM: 执行前重建资格并交集已审清单；每单元再核摘要，保护/配置有变化就跳过；apply不会纳入预演后新出现的垃圾。
# 函数用途: 以库接口预演或执行一次受限清理，返回完整审计回执。
def run_cleanup(
    home_dir,
    *,
    apply=False,
    now=None,
    min_task_tmp_age_days=3.0,
    keep_release_backups=2,
    approved_manifest=None,
    checkpoint=None,
):
    if (
        not math.isfinite(min_task_tmp_age_days)
        or min_task_tmp_age_days < 3
        or type(keep_release_backups) is not int
        or keep_release_backups < 2
    ):
        raise ValueError("临时目录至少保留3天，发布备份至少保留2套")
    source = Path(home_dir).expanduser().absolute()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("home必须是现存普通目录，不能为符号链接")
    home = source.resolve(strict=True)
    current = time.time() if now is None else now
    if not math.isfinite(current):
        raise ValueError("时间参数非法")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("平台缺少nofollow目录FD，拒绝清理")
    planner = Planner(home, current, min_task_tmp_age_days, keep_release_backups)
    candidates = planner.plan()
    policy = {
        "min_task_tmp_age_days": min_task_tmp_age_days,
        "keep_release_backups": keep_release_backups,
    }
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if approved_manifest is not None:
        if (
            approved_manifest.get("home") != str(home)
            or approved_manifest.get("policy") != policy
            or approved_manifest.get("script_sha256") != script_hash
            or approved_manifest.get("mode") != "dry-run"
        ):
            raise ValueError("预演的根、策略、脚本版本或模式不匹配")
        approved = {
            (r["path"], r["category"], r["fingerprint"]) for r in approved_manifest["candidates"]
        }
        candidates = [
            r for r in candidates if (r["path"], r["category"], r["fingerprint"]) in approved
        ]
    categories = {
        key: dict(
            planned_files=0,
            planned_bytes=0,
            planned_allocated_bytes=0,
            deleted_files=0,
            deleted_bytes=0,
            deleted_allocated_bytes=0,
            deleted_directories=0,
        )
        for key in CATEGORIES
    }
    manifest = {
        "schema": "home-file-slimming.v1",
        "home": str(home),
        "policy": policy,
        "mode": "apply" if apply else "dry-run",
        "script_sha256": script_hash,
        "started_at": datetime.fromtimestamp(current).astimezone().isoformat(),
        "categories": categories,
        "candidates": candidates,
        "exclusions": dict(planner.skips),
        "exclusion_examples": planner.examples,
        "errors": [],
        "complete": False,
    }
    for row in candidates:
        for field in ("files", "bytes", "allocated_bytes"):
            categories[row["category"]]["planned_" + field] += row[field]
        row["status"] = "planned"
    if checkpoint is not None:
        checkpoint(manifest)
    if apply:
        for row in candidates:
            try:
                # 在规划或此前单元耗时期间，配置可能切换、临时目标也可能重新出现。
                fresh = Planner(home, current, min_task_tmp_age_days, keep_release_backups)
                relative = Path(row["path"])
                if row["category"] == CATEGORIES[2] and (
                    not fresh.config_ok or fresh.referenced(relative)
                ):
                    row["status"] = "skipped_new_reference"
                    continue
                if row["category"] == CATEGORIES[0] and not fresh.orphan(relative):
                    row["status"] = "skipped_target_reappeared"
                    continue
                if row["category"] == CATEGORIES[3]:
                    fresh.backups()
                    if not any(
                        r["path"] == row["path"] and r["fingerprint"] == row["fingerprint"]
                        for r in fresh.candidates
                    ):
                        row["status"] = "skipped_backup_ranking_or_members_changed"
                        continue
                tree = _scan_unit(home, row)
                if tree["fingerprint"] != row["fingerprint"]:
                    row["status"] = "skipped_changed"
                    continue
                if row["newest_mtime"] >= time.time() - row["min_age_days"] * DAY and now is None:
                    row["status"] = "skipped_recent"
                    continue
                _delete_unit(home, row, tree, categories[row["category"]])
                row["status"] = "applied"
            except (OSError, ValueError, RecursionError) as exc:
                row["status"] = "stopped_on_error"
                manifest["errors"].append({"path": row["path"], "type": type(exc).__name__})
            finally:
                if checkpoint is not None:
                    checkpoint(manifest)
    manifest["complete"] = True
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    return manifest


# LLM: 清单是唯一外部写入；拒绝home内输出/现存文件，临时文件同父原子替换并fsync，不覆盖用户已有资料。
# 函数用途: 将预演或执行进展持久化，删除中途异常也保留最近完整单元计数。
def _save_manifest(path, payload):
    fd, temporary = tempfile.mkstemp(prefix=".slimming-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# LLM: CLI默认dry-run；显式apply才删除，已审预演仅收紧集合；返回非零表示未完整应用或参数不安全。
# 函数用途: 解析参数并输出摘要，完整清单只写指定路径。
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home-dir", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest-out", type=Path, required=True)
    parser.add_argument("--approved-manifest", type=Path)
    parser.add_argument("--min-task-tmp-age-days", type=float, default=3.0)
    parser.add_argument("--keep-release-backups", type=int, default=2)
    args = parser.parse_args(argv)
    output = args.manifest_out.absolute()
    try:
        if (
            os.path.lexists(output)
            or not output.parent.is_dir()
            or output.resolve().is_relative_to(args.home_dir.resolve())
        ):
            raise ValueError("清单必须写在home以外的全新文件，父目录须已存在")
        approved = (
            json.loads(args.approved_manifest.read_text()) if args.approved_manifest else None
        )
        result = run_cleanup(
            args.home_dir,
            apply=args.apply,
            min_task_tmp_age_days=args.min_task_tmp_age_days,
            keep_release_backups=args.keep_release_backups,
            approved_manifest=approved,
            checkpoint=lambda payload: _save_manifest(output, payload),
        )
        _save_manifest(output, result)
        print(
            json.dumps(
                {
                    "manifest": str(output),
                    "mode": result["mode"],
                    "categories": result["categories"],
                    "errors": result["errors"],
                },
                ensure_ascii=False,
            )
        )
        return 1 if result["errors"] else 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print("清理拒绝：" + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

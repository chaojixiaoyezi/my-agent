# LLM: 独立一次性清理器；只认五类白名单，默认预演；删除沿原目录FD且不跟随链接，保护规则须同步回归测试。
# 模块用途: 生成可审计的历史垃圾清单，核验后删除明确获准的中间件，不处理在线数据库或正式数据。
"""保守清理：默认dry-run；apply可用--approved-manifest限制为已审阅集合。

清单记录逻辑字节和路径累计分配块，不承诺APFS快照/克隆后的物理释放量。
归档只认--home-dir/archive-before-20261019，不自动寻找或扩大到其他位置。
发布备份只移除成套的明确备份文件，保留同目录的日志、报告、安装包和环境。
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

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


# LLM: 只加载同源码树的纯标准库YAML解析模块，不导入Agent/settings包或重读活动配置；模块摘要绑定审批策略。
# 函数用途: 复用产品唯一简化YAML语法，避免为活动路径另造易漏判的字符串解析器。
@lru_cache(maxsize=1)
def _config_parser():
    source = Path(__file__).resolve().parents[1] / "agent_py_agent/agent/settings/config_io.py"
    spec = importlib.util.spec_from_file_location("_slimming_config_io", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_simple_yaml, hashlib.sha256(source.read_bytes()).hexdigest()


# LLM: 产品解析器的read_text适配器只消费已nofollow校验的内存文本，不再次按活动路径打开文件。
# 类用途: 为原YAML解析器提供冻结配置快照。
class ConfigSnapshot:
    # LLM: 只保存本次已读文本，清单不得包含此对象或配置正文。
    # 函数用途: 固定一次配置读取结果。
    def __init__(self, text):
        self.text = text

    # LLM: 输入已经UTF-8解码，按原utf-8-sig合同移除BOM，无文件写入。
    # 函数用途: 将相同快照交给产品解析器。
    def read_text(self, encoding="utf-8-sig"):
        return self.text.removeprefix("\ufeff")


# LLM: 路径后缀采用保守超集；相对值缺运行根不能猜，类型/结构不明亦失败关闭，绝对值按父子关系保留。
# 函数用途: 从原配置语法提取保护路径，并报告是否存在未知运行根。
def _config_references(text):
    loader, _digest = _config_parser()
    raw = loader(ConfigSnapshot(text))
    refs, known = [], True
    if text.strip() and not raw and not all(not line.strip() or line.lstrip().startswith("#") for line in text.splitlines()):
        known = False
    for key, value in raw.items():
        if not key.endswith(("_path", "_dir", "_workspace")) or value == "":
            continue
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            known = False
        else:
            refs.append(Path(value).expanduser().resolve())
    return refs, known


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


# LLM: 从文件系统根逐段nofollow并固定设备/inode/模式；expected来自批准摘要，目录mtime不充当身份。
# 函数用途: 打开并核对完整祖先链，返回末目录FD及各段身份，所有出口释放FD。
@contextmanager
def _open_chain(path, expected=None):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("目录必须是规范绝对路径")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        chain = {path.anchor: _identity(os.fstat(fd))[:3]}
        prefix = Path(path.anchor)
        if expected is not None and chain[path.anchor] != expected.get(path.anchor):
            raise ValueError("祖先根身份变化")
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            prefix /= part
            identity = _identity(os.fstat(fd))[:3]
            chain[str(prefix)] = identity
            if expected is not None and identity != expected.get(str(prefix)):
                raise ValueError("祖先目录身份变化")
        yield fd, chain
    finally:
        os.close(fd)


# LLM: 所有home相对路径仍经统一完整nofollow链，不允许..或绝对relative旁路。
# 函数用途: 为只需末目录FD的扫描和读取保留简洁上下文接口。
@contextmanager
def _directory(home, relative=Path()):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("非规范相对目录")
    with _open_chain(Path(home) / relative) as (fd, _chain):
        yield fd


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
    ancestors = {}
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
            ancestors[str(home / rel)] = identity[:3]
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

    with _open_chain(home / relative.parent) as (parent, chain):
        ancestors.update(chain)
        digest.update(json.dumps(sorted(chain.items())).encode())
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
        "ancestors": ancestors,
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
        ancestors={p: identity for _, t in trees for p, identity in t["ancestors"].items()},
    )
    return result


# LLM: 只规划显式命名空间；无定位器、坏配置、未知工作区等均保留，不从报告里的估计数量推导删除。
# 类用途: 建立五类候选、保留理由及当前配置引用，不产生删除副作用。
class Planner:
    # LLM: 参数须先验证，home固定后所有相对路径沿FD访问；不导入Agent或触发迁移。
    # 函数用途: 初始化本次清理策略和只读审计容器。
    def __init__(self, home, now, age, keep, active_configs=()):
        self.home, self.now, self.age, self.keep = home, now, age, keep
        self.today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        self.today_dates = {self.today, self.today.replace("-", ""), "2026-10-29", "20261029"}
        self.active_configs = active_configs
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

    # LLM: 选中配置遵循MY_AGENT_CONFIG/显式active-config来源；只用于收紧保护；读取前后版本不同则workspace失败关闭。
    # 函数用途: 安全读取活动配置文本，不解析为权限或输出配置正文。
    def config_text_at(self, path):
        path = Path(path).expanduser().absolute()
        # /tmp等系统别名只在首次选择时规范化；配置叶仍nofollow，父链完整核对。
        parent_path = path.parent.resolve(strict=True)
        with _open_chain(parent_path) as (parent, chain):
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                before = _identity(os.fstat(fd))
                if not stat.S_ISREG(before[2]) or before[3] != 1 or before[4] > 4 * 1024**2:
                    raise ValueError("活动配置形状非法")
                data = os.read(fd, 4 * 1024**2 + 1)
                if _identity(os.fstat(fd)) != before or len(data) != before[4]:
                    raise ValueError("活动配置并发变化")
                if _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != before:
                    raise ValueError("活动配置被替换")
                with _open_chain(parent_path, chain):
                    pass
                return data.decode("utf-8")
            finally:
                os.close(fd)

    # LLM: 固定目录仅作额外保留；真正选中配置不能漏掉；缺失/坏配置使全部测试workspace保留。
    # 函数用途: 从活动配置、固定位置和浅层当前链接收集保护引用，不输出正文。
    def _references(self):
        references, texts, okay = [], [], True
        selected = str(os.environ.get("MY_AGENT_CONFIG") or "").strip()
        config_paths = [*self.active_configs, selected or Path(__file__).resolve().parents[1] / "agent_py_agent/config/agent_config.yaml"]
        for path in config_paths:
            try:
                text = self.config_text_at(path)
                refs, known = _config_references(text)
                texts.append(text)
                references.extend(refs)
                okay = okay and known
                if not known:
                    self.skip("active_configuration_unresolved_root", path)
            except (OSError, ValueError, RuntimeError):
                okay = False
                self.skip("active_configuration_unavailable", path)
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
                        text = self.config_text_at(path)
                        refs, known = _config_references(text)
                        texts.append(text)
                        references.extend(refs)
                        okay = okay and known
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
    def add(self, relative, category, days=0, keep_root=False, kind="scratch"):
        try:
            tree = _scan(self.home, relative)
            if tree["newest_mtime"] >= self.now - days * DAY:
                self.skip("recent_subtree", relative)
                return
            if not tree["nodes"]:
                self.skip("no_deletable_nodes", relative)
                return
            row = {key: value for key, value in tree.items() if key not in {"nodes", "ancestors"}}
            row.update(
                path=str(relative), category=category, keep_root=keep_root, min_age_days=days, kind=kind
            )
            self.candidates.append(row)
        except (OSError, ValueError, RecursionError):
            self.skip("unreadable_or_changing_candidate", relative)

    # LLM: tasks只走日期/slug/tmp固定深度，今天及任务根的其他兄弟目录永不作为候选。
    # 函数用途: 找出老任务的临时目录或旧归档中明确的tmp/work。
    def tasks(self, root, category, archive=False):
        for date in self.children(root):
            if date in self.today_dates:
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
                row.update({key: value for key, value in tree.items() if key not in {"nodes", "ancestors"}})
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
                self.add(rel, CATEGORIES[4], 1, kind="orphan")
            else:
                self.skip("archive_agent_not_proven_orphan", rel)
        pending = [root]
        while pending:
            base = pending.pop()
            for name in self.children(base):
                rel = base / name
                path = self.home / rel
                if base == root / "tasks" and name in self.today_dates:
                    self.skip("today_task", rel)
                    continue
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
                self.add(relative, CATEGORIES[0], 1, kind="orphan")
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


# LLM: 仅承载本清理器结构化安全拒绝原因，不包含文件正文；恢复位置放候选回执。
# 类用途: 区分身份漂移、恢复冲突与普通文件系统故障。
class SafetyError(ValueError):
    # LLM: code只来自固定内部枚举，用于清单诊断而非授权。
    # 函数用途: 保存此次拒绝的机器可读原因。
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# LLM: 不用lexists+rename模拟排他恢复；内核原生no-replace避免覆盖恢复时晚出现的文件，缺能力须先拒绝。
# 函数用途: 选择macOS/Linux原子不覆盖rename接口，其他平台不进行删除。
def _rename_api():
    library = ctypes.CDLL(None, use_errno=True)
    name, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    function = getattr(library, name, None)
    if function is None:
        raise SafetyError("exclusive_restore_unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function, flag


# LLM: 所有参数来自固定的源/隔离目录FD；失败保持隔离对象，不尝试覆盖回退。
# 函数用途: 将隔离条目原子恢复到未被重新占用的原名。
def _restore_entry(source_fd, source, destination_fd, destination):
    function, flag = _rename_api()
    if function(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


# LLM: 隔离目录出生0700且名字排他，位于home的同级；只移除本次身份未变的空隔离目录，不递归清理残留。
# 函数用途: 为一个单元创建专属隔离FD，恢复冲突时保留内容供人工处理。
@contextmanager
def _quarantine(home):
    _rename_api()  # 先确认恢复能力，再允许发生任何移出。
    with _open_chain(home.parent) as (parent, ancestors):
        name = ".slimming-quarantine-" + uuid4().hex
        os.mkdir(name, 0o700, dir_fd=parent)
        path = home.parent / name
        with _open_chain(path) as (fd, chain):
            identity = _identity(os.fstat(fd))[:3]
            try:
                yield fd, path, chain
            finally:
                if not os.listdir(fd):
                    with _open_chain(home.parent, ancestors):
                        if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))[:3] == identity:
                            os.rmdir(name, dir_fd=parent)


# LLM: 叶子rename只容ctime自然变化；目录因先删子项只核身份且必须为空；异常绝不当成已删除。
# 函数用途: 在私有隔离区检查真实取得的条目是否就是批准对象。
def _verify_quarantined(fd, name, expected):
    current = _identity(os.stat(name, dir_fd=fd, follow_symlinks=False))
    if stat.S_ISDIR(expected[2]):
        if current[:3] != expected[:3]:
            raise SafetyError("quarantined_directory_replaced")
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if _identity(os.fstat(directory))[:3] != expected[:3] or os.listdir(directory):
                raise SafetyError("quarantined_directory_changed")
        finally:
            os.close(directory)
    elif current[:6] != expected[:6]:
        raise SafetyError("quarantined_leaf_replaced")


# LLM: 活目录只rename到专属隔离区；移出后核对象及原祖先身份，失败原子无覆盖恢复；不合作同权限写者不能获全局事务承诺。
# 函数用途: 删除一个批准单元，只有核验后的隔离条目才能unlink/rmdir并累计成功数。
def _delete_unit(home, candidate, tree, counters):
    with _quarantine(home) as (q_fd, q_path, q_chain):
        for relative, expected, blocks in tree["nodes"]:
            path = Path(relative)
            if candidate["keep_root"] and relative == candidate["path"]:
                continue
            with _open_chain(home / path.parent, tree["ancestors"]) as (fd, _chain):
                current = _identity(os.stat(path.name, dir_fd=fd, follow_symlinks=False))
                directory = stat.S_ISDIR(expected[2])
                if (current[:3] != expected[:3] if directory else current != expected) or _protected(path.name):
                    raise SafetyError("source_version_changed")
                if directory:
                    with _open_chain(home / path, tree["ancestors"]) as (child, _):
                        if os.listdir(child):
                            continue  # 不移动含保护文件或新增条目的目录。
                # 最后stat可能已触发并发迁移，先重新确认全链再允许移出。
                with _open_chain(home / path.parent, tree["ancestors"]):
                    pass
                token = uuid4().hex
                os.rename(path.name, token, src_dir_fd=fd, dst_dir_fd=q_fd)
                try:
                    _verify_quarantined(q_fd, token, expected)
                    with _open_chain(home / path.parent, tree["ancestors"]):
                        pass
                    with _open_chain(q_path, q_chain):
                        pass
                    if directory:
                        os.rmdir(token, dir_fd=q_fd)
                        counters["deleted_directories"] += 1
                    else:
                        os.unlink(token, dir_fd=q_fd)
                        counters["deleted_files"] += 1
                        counters["deleted_bytes"] += expected[4]
                        counters["deleted_allocated_bytes"] += blocks
                except BaseException:
                    try:
                        _restore_entry(q_fd, token, fd, path.name)
                    except OSError as recovery_error:
                        candidate.setdefault("recovery_required", []).append(
                            {"source": relative, "quarantine": str(q_path / token), "errno": recovery_error.errno}
                        )
                        raise SafetyError("restore_conflict_preserved_in_quarantine") from recovery_error
                    raise


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
    active_configs=(),
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
    active_configs = tuple(str(Path(path).expanduser().absolute()) for path in active_configs)
    planner = Planner(home, current, min_task_tmp_age_days, keep_release_backups, active_configs)
    candidates = planner.plan()
    policy = {
        "min_task_tmp_age_days": min_task_tmp_age_days,
        "keep_release_backups": keep_release_backups,
        "active_configs": list(active_configs),
        "config_parser_sha256": _config_parser()[1],
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
                fresh = Planner(home, current, min_task_tmp_age_days, keep_release_backups, active_configs)
                relative = Path(row["path"])
                if row["category"] == CATEGORIES[2] and (
                    not fresh.config_ok or fresh.referenced(relative)
                ):
                    row["status"] = "skipped_new_reference"
                    continue
                if row.get("kind") == "orphan" and not fresh.orphan(relative):
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
                manifest["errors"].append({
                    "path": row["path"], "type": type(exc).__name__,
                    "code": exc.code if isinstance(exc, SafetyError) else "filesystem_or_validation_error",
                })
            finally:
                if checkpoint is not None:
                    checkpoint(manifest)
    manifest["complete"] = True
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    return manifest


# LLM: 不按路径replace发表；对象只能来自本次O_EXCL创建，检查完整父链与单链接身份，后续写仅落到原FD。
# 类用途: 持有清单文件与父目录的独占出生身份，为checkpoint提供受限写口。
class ManifestOutput:
    # LLM: FD由外层上下文拥有；此构造只冻结身份，不创建/关闭文件。
    # 函数用途: 绑定安全父链、Home身份与本次清单inode。
    def __init__(self, path, parent, chain, fd, home, home_chain):
        self.path, self.parent, self.chain, self.fd = path, parent, chain, fd
        self.home, self.home_chain = home, home_chain
        self.identity = _identity(os.fstat(fd))[:3]

    # LLM: 不以字符串resolve后继续写；重新nofollow逐段核身份，目录被搬动/换链接即拒绝。
    # 函数用途: 确认输出仍指向本次唯一文件且父目录仍在Home之外。
    def verify(self):
        with _open_chain(self.home, self.home_chain):
            pass
        with _open_chain(self.path.parent, self.chain) as (_fd, chain):
            home_identity = self.home_chain[str(self.home)][:2]
            if any(value[:2] == home_identity for value in chain.values()):
                raise SafetyError("manifest_parent_inside_home")
        opened = _identity(os.fstat(self.fd))
        named = _identity(os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False))
        if (opened[:3] != self.identity or named != opened or opened[3] != 1
                or not stat.S_ISREG(opened[2])):
            raise SafetyError("manifest_identity_changed")

    # LLM: 序列化完成才核身份并写原FD，拒绝叶替换；fsync持久化，但不承诺原地checkpoint的断电原子性。
    # 函数用途: 更新本次清单，不重新解析输出路径或覆盖别人的文件。
    def save(self, payload):
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.verify()
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        view = memoryview(data)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "清单写入无进展")
            view = view[written:]
        os.fsync(self.fd)
        self.verify()


# LLM: 初次父目录规范化后立即nofollow固定全链，再O_EXCL预留；异常只关闭FD，不删除可能被他人替换的输出名。
# 函数用途: 在开始扫描前安全取得全新Home外清单文件。
@contextmanager
def _manifest_output(home, output):
    home = home.resolve(strict=True)
    output = output.parent.resolve(strict=True) / output.name
    if output.is_relative_to(home):
        raise SafetyError("manifest_destination_inside_home")
    with _open_chain(home) as (_home_fd, home_chain), _open_chain(output.parent) as (parent, chain):
        if any(value[:2] == home_chain[str(home)][:2] for value in chain.values()):
            raise SafetyError("manifest_parent_inside_home")
        fd = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            destination = ManifestOutput(output, parent, chain, fd, home, home_chain)
            destination.verify()
            yield destination
        finally:
            os.close(fd)


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
    parser.add_argument("--active-config", type=Path, action="append", default=[], help="额外活动配置路径；不可读则保留全部测试workspace")
    parser.add_argument("--min-task-tmp-age-days", type=float, default=3.0)
    parser.add_argument("--keep-release-backups", type=int, default=2)
    args = parser.parse_args(argv)
    output = args.manifest_out.absolute()
    try:
        approved = (
            json.loads(args.approved_manifest.read_text()) if args.approved_manifest else None
        )
        with _manifest_output(args.home_dir, output) as destination:
            result = run_cleanup(
                args.home_dir,
                apply=args.apply,
                min_task_tmp_age_days=args.min_task_tmp_age_days,
                keep_release_backups=args.keep_release_backups,
                approved_manifest=approved,
                active_configs=args.active_config,
                checkpoint=destination.save,
            )
            destination.save(result)
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

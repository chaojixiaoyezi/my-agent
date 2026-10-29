# LLM: 所有清理与竞态回归仅操作pytest临时Home，不调用真实家目录；保留原安全断言与真实系统调用。
# 模块用途: 验证白名单、隔离恢复、活动资格及清单独占发布，禁止为通过而弱化保护。
"""一次性清理必须先证明保护边界，所有数据均在pytest临时目录。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/home_file_slimming_cleanup.py"
SPEC = importlib.util.spec_from_file_location("home_slimming", SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)
DAY = 86400
NOW = time.time()
TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW))
OLD_DATE = "2020-01-01"
OWNER = Path("owners/local/main")
CATEGORIES = (
    "dangling_orphan_agent_projections",
    "task_idle_tmp_and_empty_dirs",
    "stale_test_workspaces",
    "old_release_switch_backups",
    "archive_before_20261019_scratch_only",
)


# LLM: 测试helper只向传入的隔离根创建父目录和样本，不选择真实Home。
# 函数用途: 写入指定临时文件并返回路径。
def write(root, relative, text="scratch"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# LLM: 只修改测试树mtime，不跟随链接；NOW与产品调用使用同一时钟。
# 函数用途: 将样本文件及目录变成满足年龄门的历史对象。
def age_tree(path, days=10):
    paths = list(path.rglob("*")) if path.is_dir() else []
    for p in [*reversed(paths), path]:
        if not p.is_symlink():
            os.utime(p, (NOW - days * DAY, NOW - days * DAY))


# LLM: 快照只读普通样本文件的真实字节，不读取链接目标。
# 函数用途: 比较预演或应用前后的保留数据内容。
def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


# LLM: fixture专属临时根，所有apply必须显式传入它。
# 函数用途: 创建空白样本Home。
@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


# LLM: 五类资格由真实目录、locator和老mtime构成，备份同时保留报告控制。
# 函数用途: 构建已知数量的可删历史样本。
@pytest.fixture
def baggage(home):
    orphan = OWNER / "agents/orphan"
    write(
        home,
        orphan / "workspace_locator.json",
        json.dumps({"workspace_root": f"/var/folders/no-such-slimming-{os.getpid()}/gone"}),
    )
    write(home, orphan / "state.json", "{}")
    tmp = OWNER / "tasks" / OLD_DATE / "old/tmp"
    write(home, tmp / "pytest/a.pyc", "12345")
    (home / tmp / "empty/sub").mkdir(parents=True)
    workspace = OWNER / "workspace/runtime/workspaces/test-ws-old"
    write(home, workspace / "cache.bin", "1234567")
    archive = Path("archive-before-20261019/tasks") / OLD_DATE / "task/tmp"
    write(home, archive / "cache.bin", "123456789")
    for i in range(4):
        base = Path(f"releases/switch-{i}")
        write(home, base / "pre-switch-conversations.tar.gz", "tar")
        write(home, base / "runtime-before.db", "db")
        write(home, base / "runtime-before.db-wal", "wal")
        write(home, base / "RESULT.md", "保留报告")
        age_tree(home / base, 10 - i)
    for p in (orphan, tmp, workspace, archive):
        age_tree(home / p)
    return home


# LLM: 预演不能写样本；批准后成功计数必须与删除事实和保留对象一致。
# 函数用途: 验证五类别精确规划、执行、目录修剪和再次执行幂等。
def test_five_categories_plan_exact_files_then_apply(baggage):
    before = snapshot(baggage)
    manifest = cleanup.run_cleanup(baggage, now=NOW)
    assert snapshot(baggage) == before
    expected = [2, 1, 1, 6, 1]
    assert [manifest["categories"][k]["planned_files"] for k in CATEGORIES] == expected
    result = cleanup.run_cleanup(baggage, apply=True, now=NOW, approved_manifest=manifest)
    assert [result["categories"][k]["deleted_files"] for k in CATEGORIES] == expected
    for category in CATEGORIES:
        row = result["categories"][category]
        assert row["deleted_bytes"] == row["planned_bytes"]
    assert not (baggage / OWNER / "agents/orphan").exists()
    assert (baggage / OWNER / "tasks" / OLD_DATE / "old/tmp").is_dir()
    assert not (baggage / OWNER / "tasks" / OLD_DATE / "old/tmp/empty").exists()
    assert (baggage / "releases/switch-3/runtime-before.db").is_file()
    assert (baggage / "releases/switch-2/pre-switch-conversations.tar.gz").is_file()
    assert (baggage / "releases/switch-0/RESULT.md").read_text() == "保留报告"
    assert all(
        row["deleted_files"] == 0
        for row in cleanup.run_cleanup(baggage, apply=True, now=NOW)["categories"].values()
    )


# LLM: 保护名即便嵌入tmp也保留，预演与应用两入口均检查真实字节。
# 函数用途: 参数化覆盖人格、记忆、技能、交付物和源材料红线。
@pytest.mark.parametrize(
    "protected",
    [
        "output/video.mp4",
        "inputs/source.bin",
        "src/main.py",
        "RESULT.md",
        "FINAL_REPORT.md",
        "memories.jsonl",
        "AGENTS.md",
        "SOUL.md",
        "USER.md",
        "memory/long_term/a.jsonl",
        "skills/custom/SKILL.md",
        "conversations/messages/a.jsonl",
    ],
)
def test_protected_data_inside_scratch_is_untouched(home, protected):
    base = OWNER / "tasks" / OLD_DATE / "task/tmp"
    keep = write(home, base / protected, "唯一正式内容")
    junk = write(home, base / "junk.pyc")
    age_tree(home / base)
    before = keep.read_bytes()
    cleanup.run_cleanup(home, now=NOW)
    assert junk.exists() and keep.read_bytes() == before
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert not junk.exists() and keep.read_bytes() == before


# LLM: 年龄门包含空目录和文件，今天任务与非tmp根均不得删除。
# 函数用途: 验证当前活动范围及近期样本受到保护。
@pytest.mark.parametrize("kind", ["today", "recent-file", "recent-empty-dir", "outside-tmp"])
def test_active_or_non_tmp_tasks_are_preserved(home, kind):
    date = TODAY if kind == "today" else OLD_DATE
    base = OWNER / "tasks" / date / "task"
    path = write(home, base / ("output/result.bin" if kind == "outside-tmp" else "tmp/a.pyc"))
    age_tree(home / base)
    if kind == "recent-file":
        os.utime(path, (NOW, NOW))
    if kind == "recent-empty-dir":
        (home / base / "tmp/recent").mkdir()
    before = snapshot(home)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert snapshot(home) == before


# LLM: 孤儿条件为AND；任一未证成立都保留整个投影。
# 函数用途: 覆盖目标仍在、定位器坏、非临时路径、近期和交付物等拒绝分支。
@pytest.mark.parametrize(
    "reason",
    [
        "exists",
        "inside-home",
        "missing-locator",
        "malformed",
        "non-temp",
        "recent",
        "deliverable",
        "output-dir",
        "dangling-target-link",
    ],
)
def test_agent_requires_all_orphan_conditions(home, tmp_path, reason):
    base = OWNER / "agents/keep"
    target = tmp_path / "existing"
    target.mkdir()
    root = f"/tmp/no-such-slimming-{os.getpid()}-x"
    if reason == "exists":
        root = str(target)
    if reason == "inside-home":
        root = str(home / "missing")
    if reason == "non-temp":
        root = "/unknown/missing"
    if reason == "dangling-target-link":
        link = tmp_path / "target-link"
        link.symlink_to(tmp_path / "missing")
        root = str(link)
    write(home, base / "state.json", "{}")
    if reason != "missing-locator":
        write(
            home,
            base / "workspace_locator.json",
            "{" if reason == "malformed" else json.dumps({"workspace_root": root}),
        )
    if reason == "deliverable":
        write(home, base / "RESULT.md")
    if reason == "output-dir":
        (home / base / "output").mkdir()
    age_tree(home / base, 0 if reason == "recent" else 10)
    before = snapshot(home)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert (home / base).is_dir() and snapshot(home) == before


# LLM: workspace前缀仅是必要条件，当前配置和链接引用优先保留。
# 函数用途: 验证canonical、未知、近期及被引用的工作区不进入删除表。
@pytest.mark.parametrize("reason", ["canonical", "config", "symlink-ref", "recent", "unknown"])
def test_workspace_guards(home, reason):
    name = (
        "main-a4e68bb5ead1"
        if reason == "canonical"
        else "customer"
        if reason == "unknown"
        else "test-ws-keep"
    )
    base = OWNER / "workspace/runtime/workspaces" / name
    write(home, base / "cache.bin")
    age_tree(home / base, 0 if reason == "recent" else 10)
    if reason == "config":
        write(home, "config/desktop.yaml", f"workspace_id: {name}\n")
    if reason == "symlink-ref":
        (home / "current-workspace").symlink_to(home / base)
    before = snapshot(home)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert snapshot(home) == before


# LLM: 旧归档只授权显式scratch/cache，输出和消息不能被递归扫描带走。
# 函数用途: 验证旧归档垃圾删除与正式内容保留并存。
def test_archive_only_scratch_not_reports_or_messages(home):
    root = Path("archive-before-20261019")
    junk = write(home, root / "tasks/d/task/work/x.cache")
    cache = write(home, root / "misc/__pycache__/x.pyc")
    for path in (
        "tasks/d/task/output/film.mp4",
        "tasks/d/task/work/RESULT.md",
        "conversations/messages/thread.jsonl",
        "memories.jsonl",
        "AGENTS.md",
    ):
        write(home, root / path, "正式")
    age_tree(home / root)
    keep = {p: b for p, b in snapshot(home).items() if not p.endswith(("x.cache", "x.pyc"))}
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert not junk.exists() and not cache.exists()
    assert snapshot(home) == keep


# LLM: 真实符号链接不跟随，多链接分支仅合成元数据，不能宣称物理硬链接通过。
# 函数用途: 验证父链逃逸和多链接普通文件均被保留。
def test_symlink_parent_and_hardlink_metadata_are_never_followed(home, tmp_path, monkeypatch):
    external = tmp_path / "outside"
    write(external, "a.pyc", "外部")
    tmp = home / OWNER / "tasks" / OLD_DATE / "task/tmp"
    tmp.parent.mkdir(parents=True)
    tmp.symlink_to(external, target_is_directory=True)
    other = write(home, OWNER / "tasks" / OLD_DATE / "other/tmp/a.pyc")
    linked = write(home, other.with_name("linked").relative_to(home), "多链接内容")
    age_tree(other.parent)
    original = cleanup.os.stat

    # LLM: 仅修改指定叶子的nlink观察，其余stat和mtime门保持真实。
    # 函数用途: 在不能建硬链接的环境覆盖多链接保留分支。
    def multiple_links(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if str(path) == "linked":
            values = list(info)
            values[3] = 2
            return os.stat_result(values)
        return info

    monkeypatch.setattr(cleanup.os, "stat", multiple_links)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert (external / "a.pyc").read_text() == "外部"
    assert linked.exists() and not other.exists()


# LLM: 已审清单只收紧当前候选，不接受新增单元或变更内容。
# 函数用途: 验证预演后改写和新增的垃圾不被旧批准删除。
def test_apply_does_not_expand_approved_plan_or_accept_changed_files(baggage):
    approved = cleanup.run_cleanup(baggage, now=NOW)
    changed = baggage / OWNER / "tasks" / OLD_DATE / "old/tmp/pytest/a.pyc"
    changed.write_text("新内容")
    extra = write(baggage, OWNER / "tasks" / OLD_DATE / "extra/tmp/a.pyc")
    age_tree(extra.parent)
    cleanup.run_cleanup(baggage, apply=True, now=NOW, approved_manifest=approved)
    assert changed.read_text() == "新内容" and extra.exists()


# LLM: 非有限年龄、短保留期和不足两套备份都须先拒绝，不能通过输入放宽红线。
# 函数用途: 验证不安全策略参数不会执行。
@pytest.mark.parametrize(
    "kwargs",
    [
        {"keep_release_backups": 1},
        {"min_task_tmp_age_days": -1},
        {"min_task_tmp_age_days": float("nan")},
    ],
)
def test_unsafe_policy_is_rejected(home, kwargs):
    with pytest.raises(ValueError):
        cleanup.run_cleanup(home, **kwargs)


# LLM: 子进程使用真实CLI和当前脚本，清单只落外部临时路径。
# 函数用途: 验证默认预演/批准执行回执可解析且统计有真实删除。
def test_cli_manifest_and_dry_run_apply_are_auditable(baggage, tmp_path):
    dry = tmp_path / "dry.json"
    applied = tmp_path / "applied.json"
    before = snapshot(baggage)
    for flag, output in (("--dry-run", dry), ("--apply", applied)):
        args = [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--home-dir",
            str(baggage),
            flag,
            "--manifest-out",
            str(output),
        ]
        if flag == "--apply":
            args += ["--approved-manifest", str(dry)]
        result = subprocess.run(args, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        manifest = json.loads(output.read_text())
        assert manifest["mode"] == flag[2:]
        if flag == "--dry-run":
            assert snapshot(baggage) == before
    assert any(row["deleted_files"] for row in manifest["categories"].values())


# LLM: 规划快照不能永久授予资格，首checkpoint后的配置/目标变化必须复核。
# 函数用途: 验证晚引用及目标复活都撤销当前单元删除资格。
@pytest.mark.parametrize("change", ["workspace-config", "orphan-target"])
def test_eligibility_rechecked_after_plan_before_delete(home, change):
    if change == "workspace-config":
        base = OWNER / "workspace/runtime/workspaces/test-ws-racing"
        victim = write(home, base / "cache.bin")
    else:
        base = OWNER / "agents/racing"
        target = home.parent / "late-target"
        write(home, base / "workspace_locator.json", json.dumps({"workspace_root": str(target)}))
        victim = write(home, base / "state.json", "{}")
    age_tree(home / base)
    touched = []

    # LLM: 只在首checkpoint执行真实新增操作，后续checkpoint无副作用。
    # 函数用途: 模拟规划后另一方恢复工作目标或配置引用。
    def before_delete(_manifest):
        if not touched:
            touched.append(True)
            if change == "workspace-config":
                write(home, "config/desktop.yaml", "workspace_id: test-ws-racing\n")
            else:
                target.mkdir()

    cleanup.run_cleanup(home, apply=True, now=NOW, checkpoint=before_delete)
    assert victim.exists(), "预演后的配置引用或目标复活必须撤销删除资格"


# LLM: 已有配置链接不可读不能被解释为“无引用”。
# 函数用途: 验证坏配置使工作区清理失败关闭。
def test_unreadable_configuration_fails_closed_for_workspaces(home):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-old/cache.bin")
    age_tree(victim.parent)
    path = home / "config/desktop.yaml"
    path.parent.mkdir()
    path.symlink_to(home / "missing")
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()


# LLM: 配置父目录被链接替换也属覆盖未知，不能继续删workspace。
# 函数用途: 验证父配置目录链接不被当成空配置目录。
def test_configuration_parent_symlink_fails_closed(home, tmp_path):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-old/cache.bin")
    age_tree(victim.parent)
    config = tmp_path / "external-config"
    config.mkdir()
    (home / "config").symlink_to(config)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()


# LLM: 预演后最新两套移走会改变排名，不能继续删除新的保底集合。
# 函数用途: 验证执行前重新计算整套备份保留次序。
def test_backup_ranking_is_revalidated_after_plan(baggage, tmp_path):
    moved = []

    # LLM: 真实移动完整备份套组，单次动作避免checkpoint重复触发。
    # 函数用途: 模拟管理员在规划后搬走最新两套备份。
    def after_plan(_manifest):
        if not moved:
            moved.append(True)
            for name in ("switch-3", "switch-2"):
                (baggage / "releases" / name).rename(tmp_path / name)

    report = cleanup.run_cleanup(baggage, apply=True, now=NOW, checkpoint=after_plan)
    assert report["categories"]["old_release_switch_backups"]["deleted_files"] == 0
    for name in ("switch-1", "switch-0"):
        assert (baggage / "releases" / name / "runtime-before.db").exists()
        assert (baggage / "releases" / name / "pre-switch-conversations.tar.gz").exists()


# LLM: 预先存在和Home内输出均不允许，检查CLI非零及旧文件内容。
# 函数用途: 验证清单启动参数的基础写入边界。
def test_cli_rejects_existing_manifest_and_in_home_manifest(home, tmp_path):
    for destination in (write(tmp_path, "keep.json", "原件"), home / "new.json"):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--home-dir",
                str(home),
                "--manifest-out",
                str(destination),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
    assert (tmp_path / "keep.json").read_text() == "原件"


# LLM: 包装真实最后stat，仅在_delete_unit活栈内交错真实替换/迁移；不伪造stat或删除结果。
# 函数用途: 在删除器最后核验之后触发一次可重复的并发文件系统动作。
def race_after_delete_stat(monkeypatch, action):
    original_delete, original_stat = cleanup._delete_unit, cleanup.os.stat
    state = {"active": False, "fired": False}

    # LLM: 保留真实删除实现和异常，只标记其调用寿命。
    # 函数用途: 限定注入只能发生在删除阶段而非预演扫描。
    def deleting(*args, **kwargs):
        state["active"] = True
        try:
            return original_delete(*args, **kwargs)
        finally:
            state["active"] = False

    # LLM: 先取真实stat再交错，返回旧观察以覆盖最终check/use窗口。
    # 函数用途: 只对leaf.bin执行一次外部写者动作。
    def racing_stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if state["active"] and not state["fired"] and str(path) == "leaf.bin":
            state["fired"] = True
            action()
        return value

    monkeypatch.setattr(cleanup, "_delete_unit", deleting)
    monkeypatch.setattr(cleanup.os, "stat", racing_stat)
    return state


# LLM: 来源双审F1；批准清单始终由真实预演生成，不靠省略审批制造误删。
# 函数用途: 最后stat后换入新inode时必须保留新文件并明确报错。
def test_leaf_replacement_after_last_stat_is_not_deleted(home, tmp_path, monkeypatch):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "task/tmp/leaf.bin", "old")
    age_tree(victim.parent)
    approved = cleanup.run_cleanup(home, now=NOW)
    replacement = write(tmp_path, "replacement", "unapproved-new-file")
    assert replacement.stat().st_ino != victim.stat().st_ino
    state = race_after_delete_stat(monkeypatch, lambda: os.replace(replacement, victim))
    result = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert state["fired"]
    assert victim.exists(), "未批准新inode不能被删除"
    assert victim.read_text() == "unapproved-new-file"
    assert result["errors"] and result["categories"][CATEGORIES[1]]["deleted_files"] == 0


# LLM: 来源双审F1；真实rename整个已打开父目录，原tmp再建新目录以捕获只验证路径存在的伪修复。
# 函数用途: tmp迁到output后禁止删除交付区里的叶子。
def test_open_parent_moved_to_output_stops_deletion(home, monkeypatch):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "task/tmp/leaf.bin", "keep")
    saved = victim.parent.parent / "output/saved"
    saved.parent.mkdir()
    age_tree(victim.parent)
    approved = cleanup.run_cleanup(home, now=NOW)

    # LLM: 真实迁移目录并在原处建替代对象，不改产品观察。
    # 函数用途: 模拟另一个整理者把临时文件提升为交付内容。
    def move_parent():
        victim.parent.rename(saved)
        victim.parent.mkdir()

    state = race_after_delete_stat(monkeypatch, move_parent)
    result = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert state["fired"]
    assert (saved / victim.name).exists(), "output中的对象不能被删除"
    assert (saved / victim.name).read_text() == "keep"
    assert result["errors"] and result["categories"][CATEGORIES[1]]["deleted_files"] == 0


# LLM: 来源双审F2；在CLI进入run_cleanup时改输出路径，保留真实写入链；不接受错误回执掩盖文件损坏。
# 函数用途: 检查清单晚出现或父目录换链接均不覆盖其他文件。
@pytest.mark.parametrize("race", ["late-existing-output", "manifest-parent-symlink"])
def test_manifest_destination_remains_exclusive_and_outside_home(home, tmp_path, monkeypatch, race):
    protected = write(home, "persona/AGENTS.md", "formal-persona")
    reports = tmp_path / "reports"
    reports.mkdir()
    output = reports / "AGENTS.md"
    original = cleanup.run_cleanup

    # LLM: 新版先预留输出身份，外部写者先移走该对象再新建名字；不借共享FD原地写覆盖来伪造替换。
    # 函数用途: 在首checkpoint之前注入真实目录或文件替换。
    def after_validation(*args, **kwargs):
        if race == "late-existing-output":
            if output.exists():
                output.rename(reports / "reserved")
            output.write_text("other-writer-report")
        else:
            reports.rename(tmp_path / "reports-original")
            reports.symlink_to(protected.parent, target_is_directory=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(cleanup, "run_cleanup", after_validation)
    status = cleanup.main(["--home-dir", str(home), "--dry-run", "--manifest-out", str(output)])
    assert protected.read_text() == "formal-persona"
    if race == "late-existing-output":
        assert output.read_text() == "other-writer-report"
    assert status != 0


# LLM: 来源双审F3；两入口相同临时目标与相同checkpoint，对归档也必须撤销孤儿资格。
# 函数用途: 目标复活后普通和归档投影都保留，回执0删除。
@pytest.mark.parametrize("archived", [False, True])
def test_orphan_target_reappearing_at_checkpoint_revokes_eligibility(home, tmp_path, archived):
    base = (Path("archive-before-20261019") if archived else OWNER) / "agents/orphan"
    target = tmp_path / "workspace-returned"
    write(home, base / "workspace_locator.json", json.dumps({"workspace_root": str(target)}))
    victim = write(home, base / "state.json", "{}")
    age_tree(home / base)
    approved = cleanup.run_cleanup(home, now=NOW)
    assert len(approved["candidates"]) == 1

    # LLM: 只改变候选之外的目标存在性，不改变投影摘要。
    # 函数用途: 在第一次执行checkpoint恢复工作目录。
    def revive(_manifest):
        target.mkdir(exist_ok=True)

    result = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved, checkpoint=revive)
    assert victim.exists(), "恢复目标后不可继续按孤儿删除"
    assert result["candidates"][0]["status"] == "skipped_target_reappeared"
    assert sum(row["deleted_files"] for row in result["categories"].values()) == 0


# LLM: 归档通用缓存入口不能绕过固定tasks的当天红线，两种日期格式共同验证。
# 函数用途: 保留归档当日任务中的老缓存。
@pytest.mark.parametrize("compact", [False, True])
def test_archive_today_cache_is_excluded(home, compact):
    date = TODAY.replace("-", "") if compact else TODAY
    victim = write(home, Path("archive-before-20261019/tasks") / date / "task/__pycache__/x.pyc")
    age_tree(home / "archive-before-20261019")
    result = cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()
    assert not result["candidates"]


# LLM: Astra-2 F4来源；环境选中外部配置是产品正式入口，不可用固定目录缺引用推导不活跃。
# 函数用途: 外部配置引用和不可读的已选配置都必须保留workspace。
@pytest.mark.parametrize("readable", [False, True])
def test_selected_external_configuration_protects_workspace(home, tmp_path, monkeypatch, readable):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-live/local_store/local.db")
    age_tree(home / OWNER / "workspace/runtime/workspaces")
    config = tmp_path / "active.yaml"
    if readable:
        config.write_text("local_store_path: " + str(victim) + "\n")
    monkeypatch.setenv("MY_AGENT_CONFIG", str(config))
    result = cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()
    assert not result["candidates"]


# LLM: 覆盖隔离rename本身的间隙，不只在更早stat交错；恢复冲突必须保留隔离内容和原名的新对象。
# 函数用途: 验证移出后核验及内核no-replace恢复，包含output迁移与双写者冲突。
@pytest.mark.parametrize("race", ["replace", "parent-move", "restore-conflict"])
def test_quarantine_post_move_validation_and_nonclobber_recovery(home, tmp_path, monkeypatch, race):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "task/tmp/leaf.bin", "old")
    saved = victim.parent.parent / "output/saved"
    saved.parent.mkdir()
    replacement = write(tmp_path, "replacement", "unapproved")
    age_tree(victim.parent)
    approved = cleanup.run_cleanup(home, now=NOW)
    original_rename = cleanup.os.rename
    fired = []

    # LLM: 真实rename前替换/移动，rename后冲突只创建新原名，不直接改产品判断。
    # 函数用途: 将竞态固定在隔离转移系统调用的边界。
    def racing_rename(source, destination, *args, **kwargs):
        if str(source) != "leaf.bin" or fired:
            return original_rename(source, destination, *args, **kwargs)
        fired.append(True)
        if race == "parent-move":
            victim.parent.rename(saved)
            victim.parent.mkdir()
        else:
            os.replace(replacement, victim)
        result = original_rename(source, destination, *args, **kwargs)
        if race == "restore-conflict":
            victim.write_text("new-name-owner")
        return result

    monkeypatch.setattr(cleanup.os, "rename", racing_rename)
    report = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert fired and report["errors"]
    assert report["categories"][CATEGORIES[1]]["deleted_files"] == 0
    if race == "parent-move":
        assert (saved / victim.name).read_text() == "old"
    elif race == "replace":
        assert victim.read_text() == "unapproved"
    else:
        assert victim.read_text() == "new-name-owner"
        recovery = report["candidates"][0]["recovery_required"]
        assert len(recovery) == 1
        assert Path(recovery[0]["quarantine"]).read_text() == "unapproved"
        assert report["errors"][0]["code"] == "restore_conflict_preserved_in_quarantine"


# LLM: 目录隔离后也须复查为空，不能rmtree晚出现的正式数据；恢复原目录身份。
# 函数用途: 在空目录隔离后新增报告，验证它不被删除且恢复原处。
def test_quarantined_directory_gaining_report_is_restored(home, monkeypatch):
    base = home / OWNER / "tasks" / OLD_DATE / "task/tmp"
    empty = base / "empty"
    empty.mkdir(parents=True)
    age_tree(base)
    approved = cleanup.run_cleanup(home, now=NOW)
    original_rename = cleanup.os.rename
    fired = []

    # LLM: 先真实移出，再经隔离FD创建报告，验证后核而非伪造异常。
    # 函数用途: 模拟最后空目录检查之后出现新文件。
    def racing_rename(source, destination, *args, **kwargs):
        result = original_rename(source, destination, *args, **kwargs)
        if str(source) == "empty" and not fired:
            fired.append(True)
            directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY, dir_fd=kwargs["dst_dir_fd"])
            try:
                fd = os.open("RESULT.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                with os.fdopen(fd, "w") as stream:
                    stream.write("keep-result")
            finally:
                os.close(directory)
        return result

    monkeypatch.setattr(cleanup.os, "rename", racing_rename)
    report = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert fired and report["errors"]
    assert (empty / "RESULT.md").read_text() == "keep-result"
    assert report["categories"][CATEGORIES[1]]["deleted_directories"] == 0


# LLM: 不依赖环境继承，active_configs显式选择外部入口；坏配置禁止删除，正常配置按实际引用收紧。
# 函数用途: 验证库接口可接入调用者实际使用的配置路径。
def test_explicit_active_configuration_protects_workspace(home, tmp_path):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-explicit/local_store/local.db")
    age_tree(victim.parent.parent)
    config = write(tmp_path, "external.yaml", "local_store_path: " + str(victim))
    report = cleanup.run_cleanup(home, apply=True, now=NOW, active_configs=[config])
    assert victim.exists() and not report["candidates"]


# LLM: 不可依赖操作系统fd计数接口；捕获本次成功open并在故障后直接fstat验证均已关闭。
# 函数用途: 完整祖先链首次fstat失败也不能泄漏已打开FD。
def test_open_chain_releases_fd_on_initial_fstat_error(home, monkeypatch):
    opened = []
    original_open, original_fstat = cleanup.os.open, cleanup.os.fstat

    # LLM: 记录真实open返回值，不改变FD语义。
    # 函数用途: 保存应在退出后关闭的目录句柄。
    def tracked_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    # LLM: 仅对本次目录FD注入EIO，其他FD保持真实。
    # 函数用途: 重现打开根目录后首次元数据读取异常。
    def broken_fstat(fd):
        if fd in opened:
            raise OSError(5, "injected EIO")
        return original_fstat(fd)

    monkeypatch.setattr(cleanup.os, "open", tracked_open)
    monkeypatch.setattr(cleanup.os, "fstat", broken_fstat)
    with pytest.raises(OSError), cleanup._open_chain(home):
        pytest.fail("故障不能被吞掉")
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            original_fstat(fd)


# LLM: 独审指出相对路径在产品中以运行根解析；先调用真实解析器证明引用，而不是仅匹配配置文字。
# 函数用途: 相对运行根不明或绝对父根覆盖时，workspace删除都须失败关闭。
@pytest.mark.parametrize("value", ["relative", "absolute-parent"])
def test_active_runtime_path_semantics_preserve_workspace(home, tmp_path, monkeypatch, value):
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.runtime_paths import resolve_runtime_paths_for_agent

    workspace = home / OWNER / "workspace/runtime/workspaces/test-ws-relative"
    victim = write(workspace, "local_store/local.db", "active-database")
    age_tree(workspace)
    raw = "local_store/local.db" if value == "relative" else str(workspace.parent)
    config = write(tmp_path, "selected.yaml", "local_store_path: " + raw + "\n")
    monkeypatch.setenv("MY_AGENT_CONFIG", str(config))
    resolved = resolve_runtime_paths_for_agent(
        SimpleNamespace(local_store_path=raw), workspace,
        SimpleNamespace(owner_home_dir=home / OWNER),
    ).paths["local_store_path"]
    assert resolved == (victim if value == "relative" else workspace.parent)
    approved = cleanup.run_cleanup(home, now=NOW)
    report = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert victim.exists(), "可读配置不代表原文未匹配的workspace已不活动"
    assert victim.read_text() == "active-database"
    assert report["categories"][CATEGORIES[2]]["deleted_files"] == 0

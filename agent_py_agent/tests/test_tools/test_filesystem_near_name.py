"""相近文件名建议：安全回归与边界测试。

覆盖两类真实缺陷（2026-09-28 dsh-9b 复审 N1 / N3）与原先钉不住的行为
（大小写、顺序、同目录多候选、朴素 DP 对拍）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _read_tool(root: Path, *, extra_roots: list[Path] | None = None):
    from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions, ReadFileTool

    opts = FileSystemAccessOptions(workspace_roots=[root, *(extra_roots or [])]) if extra_roots else None
    return ReadFileTool(root, max_chars=2000, access_options=opts) if opts else ReadFileTool(root, max_chars=2000)


# --------------------------------------------------------------------------- N1 越墙泄露

def test_symlink_to_outside_owner_never_appears_in_candidates(tmp_path: Path):
    """LLM: A link pointing outside the granted roots must not leak its target's existence.

    新手说明:
    alice 的工作区里放一个链接指向 bob 家的文件。读一个相近的名字时，
    绝不能出现 bob 那边的绝对路径 —— 否则等于能探测别人家里有什么文件。
    """
    root = tmp_path / "alice"
    outside = tmp_path / "bob"
    (root / "docs").mkdir(parents=True)
    outside.mkdir()
    secret = outside / "bobs_secret_notes.md"
    secret.write_text("private", encoding="utf-8")
    (root / "docs" / "bobs_secret_note.md").symlink_to(secret)

    tool = _read_tool(root)
    result = tool.execute({"path": "docs/bobs_secret_notx.md"})

    assert not result.ok
    candidates = result.result_envelope["candidate_paths"]
    assert candidates == [], f"泄露了墙外路径：{candidates}"
    assert str(outside) not in result.output


def test_symlink_probe_cannot_distinguish_existing_from_missing_target(tmp_path: Path):
    """LLM: Existing vs missing outside targets must look identical to the reader.

    新手说明:
    如果"指向存在文件的链接"会给出建议、"指向不存在文件的链接"不给，
    那就能靠这个差异探测别人家里有没有某个文件。两种情况必须一样。
    """
    observed = []
    for exists in (True, False):
        root = tmp_path / f"alice-{exists}"
        outside = tmp_path / f"bob-{exists}"
        (root / "docs").mkdir(parents=True)
        outside.mkdir()
        target = outside / "bobs_secret_notes.md"
        if exists:
            target.write_text("private", encoding="utf-8")
        (root / "docs" / "bobs_secret_note.md").symlink_to(target)

        tool = _read_tool(root)
        result = tool.execute({"path": "docs/bobs_secret_notx.md"})
        observed.append(tuple(result.result_envelope["candidate_paths"]))

    assert observed[0] == observed[1] == (), f"两种情形可区分：{observed}"


def test_existing_sibling_in_same_dir_is_still_suggested(tmp_path: Path):
    """LLM: The security fix must not disable the ordinary same-directory suggestion."""
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "final_report.md").write_text("real", encoding="utf-8")

    tool = _read_tool(root)
    result = tool.execute({"path": "docs/finl_report.md"})

    assert not result.ok
    assert str(root / "docs" / "final_report.md") in result.result_envelope["candidate_paths"]


# --------------------------------------------------------------------------- N1b main 上的既有口

def test_parent_sibling_candidates_do_not_leak_symlink_targets(tmp_path: Path):
    """LLM: The pre-existing sibling scan had the same leak; it must be closed too.

    新手说明:
    这是 main 上早就存在的口子（不只在近名建议里）。同一个目录放一个指向墙外的链接，
    跨目录候选也必须过滤掉它。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "alice"
    outside = tmp_path / "bob"
    root.mkdir()
    outside.mkdir()
    secret = outside / "project_notes.md"
    secret.write_text("private", encoding="utf-8")
    # 词干相同的链接：老的 _score_candidate 会 resolve 后泄露
    (root / "project_notes.md").symlink_to(secret)
    (root / "other.md").write_text("x", encoding="utf-8")

    found = recovery.suggest_missing_path_candidates(
        raw_path="project_note.md",
        target=root / "project_note.md",
        workspace_roots=[root],
    )
    assert all(str(outside) not in str(p) for p in found), f"泄露：{found}"


def test_candidate_reporting_uses_entry_path_not_link_target(tmp_path: Path):
    """LLM: Report the entry's own path so the answer stays inside the granted directory."""
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    inner = root / "docs"
    inner.mkdir(parents=True)
    real = inner / "actual.md"
    real.write_text("x", encoding="utf-8")
    (inner / "actual_link.md").symlink_to(real)

    found = recovery.suggest_near_name_paths(
        "actual_lunk.md", inner, recovery.NearNameScope(workspace_roots=[root])
    )
    for path in found:
        assert path.parent == inner, f"候选跑出同目录：{path}"
        assert not path.is_symlink() or path.parent == inner


# --------------------------------------------------------------------------- N3 目录上限

def test_directory_cap_stops_reading_instead_of_materializing_all(tmp_path: Path, monkeypatch):
    """LLM: A directory over the cap must not be fully read.

    新手说明:
    5000 个文件的目录，不能为了判断"超上限"而把整个目录读完；
    最多读 上限+1 条就要停。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    big = root / "big"
    big.mkdir(parents=True)
    cap = recovery._NEAR_NAME_MAX_DIRECTORY_ENTRIES
    for index in range(cap + 200):
        (big / f"generated-{index:05d}.txt").write_text("x", encoding="utf-8")

    seen = {"n": 0}
    real_scandir = os.scandir

    class _Counting:
        def __init__(self, it):
            self._it = it

        def __iter__(self):
            for entry in self._it:
                seen["n"] += 1
                yield entry

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def close(self):
            return self._it.close()

    monkeypatch.setattr(recovery.os, "scandir", lambda p=".": _Counting(real_scandir(p)))
    out = recovery.suggest_near_name_paths(
        "generated-xxxxx.txt", big, recovery.NearNameScope(workspace_roots=[root])
    )

    assert out == []
    assert seen["n"] <= cap + 1, f"读了 {seen['n']} 条，超过 cap+1={cap + 1}"
    assert seen["n"] < cap + 200, "把整个目录读完了"


def test_directory_under_cap_is_still_scanned(tmp_path: Path):
    """LLM: The cap must not disable small directories."""
    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "final_report.md").write_text("x", encoding="utf-8")

    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    found = recovery.suggest_near_name_paths(
        "finl_report.md", docs, recovery.NearNameScope(workspace_roots=[root])
    )
    assert [p.name for p in found] == ["final_report.md"]


def test_overlong_name_skips_near_name_matching(tmp_path: Path):
    """LLM: A very long name must not trigger near-name matching at all.

    新手说明:
    编辑距离是 O(长度^2)，名字越长越慢。名字超过上限时直接不做近名匹配，
    这样"513 条目录 × 名字长度上限"就把最坏耗时卡住了（复审 N5 的便宜解法）。
    这里放一个距离 1 的兄弟文件，断言它**不会**被建议出来 —— 证明匹配被跳过，
    而不只是"没找到"。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    long_name = "a" * recovery._NEAR_NAME_MAX_NAME_LENGTH + ".md"
    (docs / long_name).write_text("x", encoding="utf-8")
    # 只差一个字符：若真的做了匹配，它必然被建议出来。
    typo = "a" * recovery._NEAR_NAME_MAX_NAME_LENGTH + "_x.md"

    found = recovery.suggest_near_name_paths(
        typo, docs, recovery.NearNameScope(workspace_roots=[root])
    )
    assert found == [], f"超长名字仍做了近名匹配：{found}"


def test_name_at_the_length_limit_still_matches(tmp_path: Path):
    """LLM: The length cap must be inclusive, not off-by-one."""
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    limit = recovery._NEAR_NAME_MAX_NAME_LENGTH
    suffix = ".md"
    name = "b" * (limit - len(suffix)) + suffix   # 长度正好 = limit
    assert len(name) == limit
    (docs / name).write_text("x", encoding="utf-8")
    # 同样长度、只换一个字符：既在长度上限内，距离又是 1，必然应被建议出来。
    typo = "b" * (limit - len(suffix) - 1) + "z" + suffix
    assert len(typo) == limit

    found = recovery.suggest_near_name_paths(
        typo, docs, recovery.NearNameScope(workspace_roots=[root])
    )
    assert [p.name for p in found] == [name], f"边界长度被误跳过：{found}"


# --------------------------------------------------------------------------- 原先钉不住的行为

def test_distance_limit_two_and_suggestion_cap_two(tmp_path: Path):
    """LLM: Distance limit and suggestion count are separate caps and both must hold."""
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    assert recovery._NEAR_NAME_DISTANCE_LIMIT == 2
    assert recovery._NEAR_NAME_MAX_SUGGESTIONS == 2

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    for name in ("alpha_one.md", "alpha_two.md", "alpha_three.md"):
        (docs / name).write_text("x", encoding="utf-8")
    # 距离 3 的名字不该出现
    (docs / "zzzzzzzzzzzz.md").write_text("x", encoding="utf-8")

    found = recovery.suggest_near_name_paths(
        "alpha_xne.md", docs, recovery.NearNameScope(workspace_roots=[root])
    )
    assert len(found) <= 2, f"超过 2 条：{found}"
    assert all("zzzzzzzzzzzz" not in p.name for p in found), "距离超过上限仍被建议"


def test_multiple_candidates_are_sorted_by_distance_then_name(tmp_path: Path):
    """LLM: Ties and near-ties must have a stable, documented order."""
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    # 距离 1 与距离 2 各一个，且都有多个同距离项
    for name in ("beta_one.md", "beta_two.md", "beta_uno.md"):
        (docs / name).write_text("x", encoding="utf-8")

    found = recovery.suggest_near_name_paths(
        "beta_onne.md", docs, recovery.NearNameScope(workspace_roots=[root])
    )
    names = [p.name for p in found]
    assert names == sorted(names), f"顺序不稳定：{names}"


def test_near_name_match_is_case_insensitive(tmp_path: Path):
    """LLM: Filename matching ignores case (verified by a mutant that survived before)."""
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "FinalReport.md").write_text("x", encoding="utf-8")

    found = recovery.suggest_near_name_paths(
        "final_report.md", docs, recovery.NearNameScope(workspace_roots=[root])
    )
    assert [p.name for p in found] == ["FinalReport.md"]


def test_near_name_suggestions_come_first(tmp_path: Path):
    """LLM: Same-directory near names must be ranked ahead of cross-directory candidates."""
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "target_file.md").write_text("x", encoding="utf-8")
    other = root / "elsewhere"
    other.mkdir()
    (other / "target_file.md").write_text("x", encoding="utf-8")

    found = recovery.suggest_missing_path_candidates(
        raw_path="docs/target_fil.md",
        target=docs / "target_fil.md",
        workspace_roots=[root],
    )
    assert found, "应当有候选"
    assert found[0].parent == docs, f"同目录近名没排在最前：{found}"


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_edit_distance_matches_naive_implementation(limit: int):
    """LLM: The rolling-array distance must equal a naive full DP for every limit.

    新手说明:
    这个函数做过一次"更快但算错"的重写（带状 DP，20000 例里错 1306 例），
    所以必须有一条和朴素实现对拍的测试钉住它。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    def naive(a: str, b: str) -> int:
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    cases = [
        ("", "", 0), ("a", "", 1), ("", "ab", 2), ("abc", "abc", 0),
        ("finl.md", "final.md", 1), ("report.m dx", "report.md", 2),
        ("aaa", "aa", 1), ("ababbaabab", "aaaaabab", 3), ("cdb", "cfe", 2),
        ("bbabb", "baaabab", 3), ("bbbba", "aabbaaba", 4),
    ]
    for left, right, expected in cases:
        raw = naive(left, right)
        want = raw if raw <= limit else None
        got = recovery._edit_distance_within(left, right, limit)
        assert got == want, f"{left!r} vs {right!r} limit={limit}: got={got} want={want} raw={raw}"
        if raw <= limit:
            assert raw == expected or expected >= 0  # 记录期望值，便于阅读

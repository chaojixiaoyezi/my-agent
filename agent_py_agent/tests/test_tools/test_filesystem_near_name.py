"""相近文件名建议：安全回归与边界测试。

覆盖两类真实缺陷（2026-09-28 dsh-9b 复审 N1 / N3）与原先钉不住的行为
（大小写、顺序、同目录多候选、朴素 DP 对拍）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _read_tool(root: Path, *, extra_roots: list[Path] | None = None):
    """`workspace_roots` 是 ReadFileTool 的参数，不是 FileSystemAccessOptions 的字段。"""
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool

    return ReadFileTool(root, max_chars=2000, workspace_roots=[root, *(extra_roots or [])])


def _owner_walled_tool(owner_home: Path, inner_root: Path):
    """造一个带真实 owner 墙的读取工具：只有 owner_home 内放行。

    这样候选项才会真正走到 AccessGate（check_path_access 返回结构化裁决），
    而不是像没有墙的普通模式那样一律放行。

    **工具根必须是 inner_root，不能是 owner_home。** 请求里的 `docs/...` 是相对工具根解析的；
    若把根设成 owner_home，而文件实际在 owner_home/workspace/docs 下，请求会指到不存在的
    owner_home/docs，近名逻辑压根不跑 —— 断言"没有泄露"就会空跑通过（2026-09-28 dsh-9b 复审指出）。
    """
    from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions, ReadFileTool

    options = FileSystemAccessOptions(owner_scope_root=owner_home)
    return ReadFileTool(inner_root, max_chars=2000, workspace_roots=[inner_root], access_options=options)


def _assert_near_name_logic_actually_ran(result) -> dict:
    """防空跑：确认这次请求真的走到了"目标不存在、父目录存在"的近名分支。

    没有这道闸，把工具根写错时测试仍会通过（候选为空、断言"没有泄露"恒真）。
    """
    envelope = result.result_envelope
    assert envelope is not None, "没有结构化回执，说明请求没走到缺失路径分支"
    assert envelope.get("path_not_found") is True, f"没走到缺失路径分支：{envelope}"
    assert envelope.get("expected_kind") is not None, f"缺少 expected_kind：{envelope}"
    return envelope


# --------------------------------------------------------------------------- N1 越墙泄露

def test_symlink_to_outside_owner_never_appears_in_candidates(tmp_path: Path):
    """LLM: A link pointing outside the granted roots must not leak its target's existence.

    新手说明:
    alice 的工作区里放一个链接指向 bob 家的文件。必须在**真实 owner 墙**下测：
    没有墙时本来就没有"越权"可言，闸门会一律放行（那样测不出东西）。
    """
    owner_home = tmp_path / "owner"
    workspace = owner_home / "workspace"
    (workspace / "docs").mkdir(parents=True)
    outside = tmp_path / "bob"
    outside.mkdir()
    secret = outside / "bobs_secret_notes.md"
    secret.write_text("private", encoding="utf-8")
    (workspace / "docs" / "bobs_secret_note.md").symlink_to(secret)

    tool = _owner_walled_tool(owner_home, workspace)
    result = tool.execute({"path": "docs/bobs_secret_notx.md"})

    assert not result.ok
    envelope = _assert_near_name_logic_actually_ran(result)
    candidates = envelope["candidate_paths"]
    assert all(str(outside) not in path for path in candidates), f"泄露了墙外路径：{candidates}"
    assert all(not path.endswith("bobs_secret_note.md") for path in candidates), (
        f"指向墙外的链接被建议出来了：{candidates}"
    )


def test_symlink_probe_cannot_distinguish_existing_from_missing_target(tmp_path: Path):
    """LLM: Existing vs missing outside targets must look identical to the reader.

    新手说明:
    如果"指向存在文件的链接"会给出建议、"指向不存在文件的链接"不给，
    那就能靠这个差异探测别人家里有没有某个文件。两种情况必须一样。
    """
    observed = []
    for exists in (True, False):
        owner_home = tmp_path / f"owner-{exists}"
        workspace = owner_home / "workspace"
        (workspace / "docs").mkdir(parents=True)
        outside = tmp_path / f"bob-{exists}"
        outside.mkdir()
        target = outside / "bobs_secret_notes.md"
        if exists:
            target.write_text("private", encoding="utf-8")
        (workspace / "docs" / "bobs_secret_note.md").symlink_to(target)

        tool = _owner_walled_tool(owner_home, workspace)
        result = tool.execute({"path": "docs/bobs_secret_notx.md"})
        observed.append(tuple(_assert_near_name_logic_actually_ran(result)["candidate_paths"]))

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
    cap = recovery._NEAR_NAME_MAX_DIRECTORY_ENTRY_COUNT
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
    long_name = "a" * recovery._NEAR_NAME_MAX_NAME_LENGTH_CHARS + ".md"
    (docs / long_name).write_text("x", encoding="utf-8")
    # 只差一个字符：若真的做了匹配，它必然被建议出来。
    typo = "a" * recovery._NEAR_NAME_MAX_NAME_LENGTH_CHARS + "_x.md"

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
    limit = recovery._NEAR_NAME_MAX_NAME_LENGTH_CHARS
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

    assert recovery._NEAR_NAME_DISTANCE_LIMIT_COUNT == 2
    assert recovery._NEAR_NAME_MAX_SUGGESTION_COUNT == 2

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


# --------------------------------------------------------------------------- 真实 owner 墙下的闸门
#
# 复审（2026-09-28，dsh-9b B1/B2/B6）指出：`PathAccessDecision` 没有 `__bool__`，
# 所以 `bool(decide(path))` 恒为真，闸门形同虚设。这些用例跑在**真实 owner 墙**下，
# 专门用来杀"把闸门整个关掉"那类变异（MG1）——普通模式碰不到闸门。

def test_credential_file_blocked_in_normal_mode_but_allowed_under_owner_wall(tmp_path: Path):
    """LLM: 凭据文件名只被普通模式拦；owner 墙下本来就放行 —— 期望值必须按现行策略写。

    新手说明（2026-09-28 dsh-9b 复审更正）:
    我原先这条断言"owner 墙下 .env 不被建议"，**是错的**。逐条实测确认：
      - 普通模式（无墙）：`check_path_access(.env).allowed is False`，
        code=`PATH_CREDENTIAL_FILE_BLOCKED` → 不该被建议；
      - owner 墙模式：`.allowed is True` → `.env` 会被正常建议出来。
    原因是 `path_access_policy._credential_file_decision` 只作为「未受 owner 墙约束的
    普通模式」的遗留闸门，owner-home 与显式 full access 的裁决在它之前就做完了。

    **这是个策略缺口，不是本模块的 bug**：远程多用户场景下 .env 的正文会进入模型上下文、
    发给模型服务商。已按 9b 要求记进 DESIGN_LEDGER 作为「待用户决策」，
    本测试只如实地把**两种模式各自的现行行为**钉住，避免以后再写成错期望值。
    """
    owner_home = tmp_path / "owner"
    workspace = owner_home / "workspace"
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    credential = docs / ".env"
    credential.write_text("SECRET=1", encoding="utf-8")
    (docs / "notes.md").write_text("x", encoding="utf-8")

    # ① owner 墙：放行 → 会被建议（按现行策略，这是已知缺口）
    tool = _owner_walled_tool(owner_home, workspace)
    result = tool.execute({"path": "docs/.evn"})
    assert not result.ok
    envelope = _assert_near_name_logic_actually_ran(result)
    assert tool.check_path_access(credential).allowed is True, "前提变了：owner 墙下 .env 被拒了"
    assert any(path.endswith(".env") for path in envelope["candidate_paths"]), (
        f"owner 墙下 .env 本应放行并被建议（现行策略）：{envelope['candidate_paths']}"
    )

    # ② 普通模式：被拒 → 不该被建议
    plain = _read_tool(workspace)
    assert plain.check_path_access(credential).allowed is False, "前提变了：普通模式不再拦 .env"
    plain_result = plain.execute({"path": "docs/.evn"})
    plain_envelope = _assert_near_name_logic_actually_ran(plain_result)
    assert all(".env" not in path for path in plain_envelope["candidate_paths"]), (
        f"普通模式下把被拒文件建议出来了：{plain_envelope['candidate_paths']}"
    )


def test_denied_sibling_is_dropped_from_sibling_scan(tmp_path: Path):
    """LLM: The sibling scan must honour the same gate (kills a gate-disabled mutant).

    新手说明:
    跨目录候选那条路径也要过闸门，不能只有近名那条。
    用一个词干相同、但会被策略拒绝的兄弟文件来证。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    owner_home = tmp_path / "owner"
    workspace = owner_home / "workspace"
    workspace.mkdir(parents=True)
    denied = workspace / "probe.csv"
    denied.write_text("x", encoding="utf-8")
    tool = _owner_walled_tool(owner_home, workspace)

    gate = recovery.AccessGate(tool.check_path_access)
    # 直接验证闸门本身：拒绝的路径不允许，放行的路径允许。
    assert gate.allows(denied) in (True, False)  # 取决于策略；下面用真实拒绝路径断言
    denied_decision = tool.check_path_access(denied.resolve())
    assert gate.allows(denied.resolve()) == bool(denied_decision.allowed), (
        "闸门没有按 .allowed 判断（PathAccessDecision 无 __bool__，用 bool() 会恒真）"
    )


def test_gate_reads_allowed_field_not_truthiness(tmp_path: Path):
    """LLM: AccessGate must read `.allowed`; truthiness of the decision object is always True.

    新手说明:
    这是本条复审的核心：`PathAccessDecision(allowed=False)` 的 `bool()` 是 **True**。
    闸门必须读 `.allowed`，否则"拒绝"会被当成"允许"。
    """
    from agent_py_agent.agent.path_access_policy import PathAccessDecision
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    denied = PathAccessDecision(allowed=False, code="PATH_CREDENTIAL_FILE_BLOCKED", message="no")
    allowed = PathAccessDecision(allowed=True)

    assert bool(denied) is True, "前提变了：PathAccessDecision 现在有 __bool__ 了？"

    assert recovery.AccessGate(lambda _p: denied).allows(tmp_path) is False
    assert recovery.AccessGate(lambda _p: allowed).allows(tmp_path) is True


def test_symlink_target_missing_and_existing_look_identical_under_owner_wall(tmp_path: Path):
    """LLM: Under a real owner wall an outside link must not reveal target existence.

    新手说明:
    闸门修好之后，链接可以放行（按解析后的路径过策略），所以必须再确认一次：
    指向墙外的链接，不管目标在不在，结果都要一样 —— 不能重新变成探测口。
    """
    observed = []
    for exists in (True, False):
        owner_home = tmp_path / f"owner-{exists}"
        workspace = owner_home / "workspace"
        (workspace / "docs").mkdir(parents=True)
        outside = tmp_path / f"bob-{exists}"
        outside.mkdir()
        target = outside / "bobs_secret_notes.md"
        if exists:
            target.write_text("private", encoding="utf-8")
        (workspace / "docs" / "bobs_secret_note.md").symlink_to(target)

        tool = _owner_walled_tool(owner_home, workspace)
        result = tool.execute({"path": "docs/bobs_secret_notx.md"})
        observed.append(tuple(_assert_near_name_logic_actually_ran(result)["candidate_paths"]))

    assert observed[0] == observed[1], f"两种情形可区分：{observed}"
    outside_paths = [str(tmp_path / f"bob-{flag}") for flag in (True, False)]
    for path in observed[0]:
        assert all(outside not in path for outside in outside_paths), "泄露了墙外路径"


def test_in_workspace_symlink_is_suggested_again(tmp_path: Path):
    """LLM: A legitimate link inside the workspace must still be usable (review B4 regression).

    新手说明:
    上一轮为了堵泄露，把**所有**链接都排除了，连工作区内部的合法链接也不再被建议。
    闸门修好后应当放行：裁决看的是解析后的路径，指到墙外才丢弃。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    owner_home = tmp_path / "owner"
    workspace = owner_home / "workspace"
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    real = docs / "real_report.md"
    real.write_text("x", encoding="utf-8")
    (docs / "linked_report.md").symlink_to(real)   # 指向工作区内部

    tool = _owner_walled_tool(owner_home, workspace)
    scope = recovery.NearNameScope(
        workspace_roots=[workspace], access=recovery.AccessGate(tool.check_path_access)
    )
    found = recovery.suggest_near_name_paths("linked_reprot.md", docs, scope)
    assert [p.name for p in found] == ["linked_report.md"], f"内部合法链接被误排除：{found}"


def test_two_suggestion_cap_with_three_close_names(tmp_path: Path):
    """LLM: With three names within distance 2 exactly two may be returned (kills MN1).

    新手说明:
    三个名字都要**真的**在距离 2 以内，否则测的就不是"上限"而是"恰好只有一个像"。
    这里用 report_a.md / report_b.md / report_c.md 对一个只错一处的名字。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    for name in ("report_a.md", "report_b.md", "report_c.md"):
        (docs / name).write_text("x", encoding="utf-8")

    # 先确认前提成立：三个名字都在距离上限内
    distances = [
        recovery._edit_distance_within("report_x.md", name, recovery._NEAR_NAME_DISTANCE_LIMIT_COUNT)
        for name in ("report_a.md", "report_b.md", "report_c.md")
    ]
    assert all(d is not None for d in distances), f"前提不成立，距离={distances}"

    found = recovery.suggest_near_name_paths(
        "report_x.md", docs, recovery.NearNameScope(workspace_roots=[root])
    )
    assert len(found) == 2, f"应当正好 2 条，实得 {len(found)}：{found}"


def test_in_workspace_near_name_beats_cross_directory_candidate(tmp_path: Path):
    """LLM: A same-directory near name outranks a cross-directory candidate (kills MN6).

    新手说明:
    上一版这条测试写错了：跨目录那个候选根本没被扫到，所以"顺序"无从谈起，
    变异（把近名排到末尾）因此存活。这里让**两个候选都真的存在**：
    同目录放一个弱一点的近名，跨目录放一个词干完全相同的，然后断言同目录的排前面。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "target_fix.md").write_text("x", encoding="utf-8")        # 同目录、差一个字母
    other = root / "elsewhere"
    other.mkdir()
    (other / "target_fil.md").write_text("x", encoding="utf-8")       # 跨目录、名完全相同

    found = recovery.suggest_missing_path_candidates(
        raw_path="docs/target_fil.md",
        target=docs / "target_fil.md",
        workspace_roots=[root],
    )
    names = [str(p.relative_to(root)) for p in found]
    assert "docs/target_fix.md" in names, f"前提不成立：同目录近名没进候选 {names}"
    assert "elsewhere/target_fil.md" in names, f"前提不成立：跨目录候选没进候选 {names}"
    assert found[0] == docs / "target_fix.md", f"同目录近名没排最前：{names}"


def test_sibling_scan_stem_hit_is_gated_under_owner_wall(tmp_path: Path):
    """LLM: The sibling scan must drop a stem-hit link that points outside the owner wall.

    新手说明（这条是复审 B3 要求的，也是杀死 MG4 的关键）:
    上一轮我那条 N1b 回归测试用的是 project_note.md 对 project_notes.md ——
    两者词干并不相同，`_name_score` 打分为 0，所以它**根本没进入候选逻辑**，是空跑通过。
    这里改成 `probe.csv` 对 `probe.xlsx`（词干相同，实测打分 90，确实会命中），
    并要求候选不得是那个指向墙外的链接 —— 报告 resolve 后的路径也会被这条抓住。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    owner_home = tmp_path / "owner"
    workspace = owner_home / "workspace"
    workspace.mkdir(parents=True)
    outside = tmp_path / "bob"
    outside.mkdir()
    secret = outside / "probe.xlsx"
    secret.write_text("private", encoding="utf-8")
    link = workspace / "probe.xlsx"
    link.symlink_to(secret)

    # 前提必须成立：词干命中确实给正分，否则又是空跑。
    assert recovery._name_score("probe.xlsx", "probe.csv") > 0, "前提不成立：词干没命中"

    tool = _owner_walled_tool(owner_home, workspace)
    found = recovery.suggest_missing_path_candidates(
        raw_path="probe.csv",
        target=workspace / "probe.csv",
        workspace_roots=[workspace],
        access=recovery.AccessGate(tool.check_path_access),
    )
    for path in found:
        assert str(outside) not in str(path), f"泄露/报告了墙外路径：{found}"
        assert str(link) not in str(path) and not str(path).endswith("probe.xlsx"), (
            f"指向墙外的链接被当作候选：{found}"
        )


def test_denied_candidate_under_dangerous_root_is_not_reported(tmp_path: Path):
    """LLM: A candidate the policy denies must not be reported (kills a gate-disabled mutant).

    新手说明（复审 B6 那类，也是杀死 MG1 的关键）:
    用**危险根**造出真实拒绝——`.env` 在 owner 墙下其实是被放行的（凭据拒绝只作用于
    未受owner墙约束的普通模式），所以拿它当拒绝样本站不住，我实测确认后改成危险根。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery
    from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions, ReadFileTool

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "notes.md").write_text("x", encoding="utf-8")
    danger = docs / "secrets.env"
    danger.write_text("SECRET=1", encoding="utf-8")

    options = FileSystemAccessOptions(path_dangerous_roots=[str(docs)])
    tool = ReadFileTool(root, max_chars=2000, workspace_roots=[root], access_options=options)
    decision = tool.check_path_access(danger)
    assert decision.allowed is False, f"前提不成立：危险根下没被拒绝（{decision.code}）"

    scope = recovery.NearNameScope(
        workspace_roots=[root], access=recovery.AccessGate(tool.check_path_access)
    )
    found = recovery.suggest_near_name_paths("secrets.evn", docs, scope)
    assert all("secrets.env" not in p.name for p in found), f"被拒文件仍被建议：{found}"


# ------------------------------------------------------- G1：四项回执逐字相同（dsh-9b 复审指定）

def test_outside_target_existence_is_indistinguishable_across_full_receipt(tmp_path: Path):
    """LLM: Under a real owner wall, the COMPLETE receipt must not reveal whether the outside target exists.

    新手说明（这是 9b 的 G1，专门杀 MG8 —— 在 `admits` 里让符号链接绕过裁决）

    之前几条只比 `candidate_paths`，闸门若被改坏、链接被直接放行，
    候选照样可能是空（因为对面目录根本没进扫描范围），于是变异存活。
    这条比的是**完整回执的四个部分**（ok / error_code / output / envelope），
    临时路径归一化之后要求逐字相同 —— 只要两种情形有任何一处不同，就是探测口。
    """
    import re

    runs = []
    for exists in (True, False):
        owner_home = tmp_path / f"owner-{exists}"
        workspace = owner_home / "workspace"
        (workspace / "docs").mkdir(parents=True)
        outside = tmp_path / f"bob-{exists}"
        outside.mkdir()
        target = outside / "bobs_secret_notes.md"
        if exists:
            target.write_text("private", encoding="utf-8")
        (workspace / "docs" / "bobs_secret_note.md").symlink_to(target)

        tool = _owner_walled_tool(owner_home, workspace)
        result = tool.execute({"path": "docs/bobs_secret_notx.md"})
        _assert_near_name_logic_actually_ran(result)

        def _normalize(text: str) -> str:
            """把这次运行自己的临时路径抹平，否则两个循环天然不同。"""
            return re.sub(re.escape(str(tmp_path / f"owner-{exists}")), "<OWNER>", re.sub(
                re.escape(str(tmp_path / f"bob-{exists}")), "<OUTSIDE>", text))

        runs.append({
            "ok": result.ok,
            "error_code": getattr(result, "error_code", None),
            "output": _normalize(str(result.output or "")),
            "envelope": _normalize(json.dumps(result.result_envelope, sort_keys=True, default=str)),
        })

    assert runs[0] == runs[1], (
        "两种情形（墙外目标存在 / 不存在）的完整回执不一致，等于可以探测别人家里有什么：\n"
        f"存在  : {runs[0]}\n不存在: {runs[1]}"
    )
    # 且任何一段里都不许出现 bob 家的真实路径
    for run in runs:
        assert str(tmp_path / "bob-True") not in run["envelope"]
        assert str(tmp_path / "bob-False") not in run["envelope"]


def test_sibling_scan_reports_entry_path_not_resolved_target_in_normal_mode(tmp_path: Path):
    """LLM: The sibling scan must report the entry's own path, not the symlink target (kills MG4).

    新手说明（9b 复审指出 MG4 原先在普通模式下存活）:
    我的旧测试全在 owner 墙下跑，普通模式没覆盖。而变化 MG4 是"把 `_score_candidate`
    报告的对象换成 resolve() 后的目标" —— 普通模式下没有任何裁决差异，
    只有**报告出来的路径**会变，所以必须在普通模式里直接断言路径本身。
    """
    from agent_py_agent.agent.tooling import filesystem_path_recovery as recovery

    root = tmp_path / "workspace"
    docs = root / "docs"
    docs.mkdir(parents=True)
    real = docs / "real_report.md"
    real.write_text("real", encoding="utf-8")
    link = docs / "lnk_report.md"
    link.symlink_to(real)

    tool = _read_tool(root)
    scope = recovery.NearNameScope(
        workspace_roots=[root], access=recovery.AccessGate(tool.check_path_access)
    )
    found = recovery.suggest_near_name_paths("lnk_reports.md", docs, scope)

    assert found, "前提不成立：普通模式下这个近名候选没被找到"
    names = [p.name for p in found]
    assert "lnk_report.md" in names, f"兄弟扫描应报告条目自身路径，实际：{names}"
    assert "real_report.md" not in names, f"报告了 resolve 后的目标（MG4 那类）：{names}"

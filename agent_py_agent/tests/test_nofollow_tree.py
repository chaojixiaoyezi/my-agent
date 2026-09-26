"""固定目录递归清理不能沿符号链接越界。"""

import pytest

from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.common.nofollow_tree import remove_tree_beneath


def test_recursive_delete_preserves_link_targets_and_missing_is_idempotent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    target = tmp_path / "managed" / "old"
    target.mkdir(parents=True)
    (target / "linked-directory").symlink_to(outside, target_is_directory=True)
    (target / "linked-file").symlink_to(outside / "keep.txt")
    (target / "nested" / "deeper").mkdir(parents=True)
    (target / "nested" / "deeper" / "file.txt").write_text("gone")
    (target / "nested" / "deeper" / "linked-back").symlink_to(outside, target_is_directory=True)
    remove_tree_beneath(tmp_path, ("managed", "old"))
    remove_tree_beneath(tmp_path, ("managed", "old"))
    assert not target.exists() and (outside / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("link_parent", [False, True])
def test_top_or_parent_symlink_never_becomes_delete_target(tmp_path, link_parent):
    outside = tmp_path / "outside"
    (outside / "old").mkdir(parents=True)
    link = tmp_path / "managed"
    link.symlink_to(outside, target_is_directory=True)
    parts = ("managed", "old") if link_parent else ("managed",)
    with pytest.raises((OSError, NoFollowPathError)):
        remove_tree_beneath(tmp_path, parts)
    assert (outside / "old").exists() and link.is_symlink()

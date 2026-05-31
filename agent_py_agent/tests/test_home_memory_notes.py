from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.home_memory_notes import (
    LessonNoteRequest,
    append_hot_note,
    upsert_lesson_note,
)
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)


def test_append_hot_note_adds_dated_bullet_without_duplicate(tmp_path):
    paths = ensure_my_agent_home(tmp_path / "home")

    first = append_hot_note(paths, "真实测试失败先查系统卡点，不要加硬门。", created_at="2026-05-31T10:00:00+08:00")
    second = append_hot_note(paths, "真实测试失败先查系统卡点，不要加硬门。", created_at="2026-05-31T10:01:00+08:00")

    text = Path(paths.memory_hot_md).read_text(encoding="utf-8")
    assert first.changed is True
    assert second.changed is False
    assert text.count("真实测试失败先查系统卡点，不要加硬门。") == 1
    assert "2026-05-31" in text


def test_upsert_lesson_note_updates_lesson_and_route_index(tmp_path):
    paths = ensure_my_agent_home(tmp_path / "home")

    result = upsert_lesson_note(
        paths,
        LessonNoteRequest(
            lesson_id="quality-loop",
            topic="质量返工循环",
            content="返工提示必须说明缺什么和建议下一步，但不阻断普通任务。",
            trigger_keywords=("返工", "质量", "closeout"),
        ),
    )

    lesson_text = Path(result.lesson_path).read_text(encoding="utf-8")
    index_text = Path(paths.memory_routing_index_md).read_text(encoding="utf-8")
    assert result.changed is True
    assert "返工提示必须说明缺什么" in lesson_text
    assert "## lessons.quality-loop" in index_text
    assert "authority_path: memory/lessons/quality-loop.md" in index_text


def test_provider_owner_hot_and_lesson_notes_stay_in_owner_home(tmp_path):
    paths = ensure_my_agent_home(tmp_path / "home")
    owner = ensure_owner_home(paths.root, OwnerIdentity.provider_user("feishu", "ou_123"))
    owner_paths = home_paths_with_owner(paths, owner)

    hot = append_hot_note(owner_paths, "这个飞书用户只看自己的 HOT 记忆。", created_at="2026-05-31T10:00:00+08:00")
    lesson = upsert_lesson_note(
        owner_paths,
        LessonNoteRequest(
            lesson_id="owner-scope",
            topic="owner 隔离",
            content="不同外部用户的长期教训不能写到本地主账号。",
            trigger_keywords=("owner", "隔离"),
        ),
    )

    assert hot.hot_path == str(owner_paths.owner_memory_hot_md)
    assert lesson.lesson_path.startswith(str(owner_paths.owner_memory_lessons_dir))
    assert lesson.route_index_path == str(owner_paths.owner_memory_routing_index_md)
    assert "这个飞书用户" in Path(owner_paths.owner_memory_hot_md).read_text(encoding="utf-8")
    assert "这个飞书用户" not in Path(paths.memory_hot_md).read_text(encoding="utf-8")

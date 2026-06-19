"""P2-L8 自学习闭环端到端:accept 候选 → 落 lesson 文件 → builder 能中文 n-gram 召回。"""

import json

from agent.prompting_parts.builder import _stem_matches
from agent.subagents.services.learning import SubAgentLearningService


class _FakeManager:
    def __init__(self, workspace, owner_home):
        self.workspace_root = str(workspace)
        self.workspace = str(workspace)
        self.owner_home_dir = str(owner_home)
        self.enable_self_learning = True


def _seed_draft(svc, cand_id, lesson, evidence=None):
    payload = {"id": cand_id, "lesson": lesson, "status": "draft", "evidence": evidence or []}
    (svc.learning_drafts_dir() / f"{cand_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_accept_promotes_to_lesson_and_recallable(tmp_path):
    """闭环:accept → 落 lesson 文件 → builder 用中文 n-gram 召回到它(沉淀→召回 接通)。"""
    home = tmp_path / "home"
    (home).mkdir()
    svc = SubAgentLearningService(_FakeManager(tmp_path / "ws", home))
    _seed_draft(svc, "learn1", "日志运营值守要用按需唤醒器", [{"detail": "单run扛不住无限期"}])
    svc.set_learning_candidate_status("learn1", "accepted")

    lessons_dir = home / "memory" / "lessons"
    md_files = list(lessons_dir.glob("*.md"))
    assert len(md_files) == 1  # accept 落了 lesson 文件
    content = md_files[0].read_text(encoding="utf-8")
    assert "日志运营值守要用按需唤醒器" in content
    assert "单run扛不住无限期" in content  # 证据也写进去

    # builder 能召回(中文 n-gram):prompt 只模糊提到"日志运营值守"
    hits = _stem_matches(lessons_dir, "帮我做日志运营值守".casefold())
    assert len(hits) == 1  # 召回到!自学习闭环打通


def test_reject_does_not_promote(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    svc = SubAgentLearningService(_FakeManager(tmp_path / "ws", home))
    _seed_draft(svc, "learn2", "不该被沉淀的草稿")
    svc.set_learning_candidate_status("learn2", "rejected")
    lessons_dir = home / "memory" / "lessons"
    assert not lessons_dir.exists() or not list(lessons_dir.glob("*.md"))  # rejected 不落文件


def test_no_owner_home_skips_gracefully(tmp_path):
    """owner_home 缺失时 accept 不报错(优雅跳过,不阻断审核)。"""
    svc = SubAgentLearningService(_FakeManager(tmp_path / "ws", ""))
    _seed_draft(svc, "learn3", "某经验")
    result = svc.set_learning_candidate_status("learn3", "accepted")  # 不抛
    assert result.status == "accepted"

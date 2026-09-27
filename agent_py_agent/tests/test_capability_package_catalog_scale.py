"""规模夹具只验证包目录预算及零正文展开，不代表模型召回率或真实任务速度。"""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from agent_py_agent.tests.test_capability_package_discovery import (
    discovery_fixture,
    package_fixture,
)


@pytest.mark.parametrize("count", [1, 10, 100, 1000])
def test_many_packages_keep_private_methods_out_of_bounded_metadata(tmp_path, skill_catalog_factory, record_property, count):
    reads = []
    contents = {"CAPABILITY.md": b"PRIVATE-PACKAGE-ENTRY",
                **{f"methods/private-{index:03}.md": b"PRIVATE-METHOD-BODY" for index in range(200)}}
    base = package_fixture(files=contents, reads=reads)
    packages = [replace(base, package_id=f"story-pack-{index:04}") for index in range(count)]
    started = time.perf_counter()
    _, snapshot, agent, _ = discovery_fixture(tmp_path, skill_catalog_factory, packages)
    rendered = agent.capability_router.render_skill_metadata_index(context_window_tokens=8000)
    elapsed = time.perf_counter() - started

    assert len(snapshot.entries) == 1
    assert len(snapshot.packages) == count
    assert len(agent.capability_router.cards(kinds={"skill"})) == 1
    assert len(agent.capability_router.cards(kinds={"capability_package"})) == count
    assert len(rendered) < 3000
    assert "PRIVATE-PACKAGE-ENTRY" not in rendered
    assert "PRIVATE-METHOD-BODY" not in rendered
    assert "private-199" not in rendered
    assert reads == []
    if count >= 100:
        assert "未显示" in rendered or "未展示" in rendered
        assert rendered.count("package_id:") < count
    record_property("package_count", count)
    record_property("private_member_count", count * len(contents))
    record_property("metadata_bytes", len(rendered.encode()))
    record_property("metadata_elapsed_seconds", elapsed)

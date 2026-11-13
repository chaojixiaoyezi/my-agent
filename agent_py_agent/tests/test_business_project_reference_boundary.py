"""项目引用沿原 canonical projects 目录解析，不接受路径或符号链接身份。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.gateway_parts.business_host_ports import (
    build_default_business_space_reference_resolver,
)


def _service(root):
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id="owner", owner_home_dir=root))
    service = BusinessSpaceService(
        root,
        owner_id="owner",
        enabled=True,
        reference_resolver=build_default_business_space_reference_resolver(host),
    )
    return service, BusinessActor("owner", "actor", service, lambda action, scope: True)


def test_canonical_project_reference_survives_service_rebuild(tmp_path):
    (tmp_path / "projects" / "作品-one").mkdir(parents=True)
    service, actor = _service(tmp_path)
    first = service.reference("project", "作品-one", actor=actor)
    rebuilt, other = _service(tmp_path)
    assert rebuilt.reference("project", "作品-one", actor=other) == first
    assert first == {
        "owner_id": "owner",
        "ref": "作品-one",
        "project_ref": "作品-one",
        "state": "available",
    }
    with pytest.raises(BusinessError, match="REFERENCE_NOT_FOUND"):
        service.reference("project", "not-registered", actor=actor)


@pytest.mark.parametrize(
    "ref", ["..", ".", "../outside", "/tmp", "nested/name", r"nested\name", "bad\x00name"]
)
def test_project_reference_rejects_path_identity_before_lookup(tmp_path, ref):
    service, actor = _service(tmp_path)
    with pytest.raises(BusinessError) as caught:
        service.reference("project", ref, actor=actor)
    assert caught.value.code == "INVALID_SCHEMA"
    assert caught.value.details["reason"] == "PROJECT_REF_MUST_BE_SINGLE_COMPONENT"


@pytest.mark.parametrize("component", ["projects", "project", "file"])
def test_project_reference_rejects_symlink_parent_leaf_and_regular_file(tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "owner"
    root.mkdir()
    if component == "projects":
        (outside / "valid").mkdir()
        (root / "projects").symlink_to(outside, target_is_directory=True)
    else:
        (root / "projects").mkdir()
        if component == "project":
            (root / "projects" / "valid").symlink_to(outside, target_is_directory=True)
        else:
            (root / "projects" / "valid").write_text("not a project directory")
    service, actor = _service(root)
    with pytest.raises(BusinessError) as caught:
        service.reference("project", "valid", actor=actor)
    assert caught.value.code == "SPACE_SCOPE_MISMATCH"
    assert caught.value.details["reason"] == "PROJECT_DIRECTORY_UNSAFE"
    assert list(outside.iterdir()) == ([outside / "valid"] if component == "projects" else [])

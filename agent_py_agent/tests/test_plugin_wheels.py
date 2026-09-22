"""固定 wheel 集合纯验证；没有真实模型、插件执行或产品环境变更。"""

from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_manifest import PluginPackageError
from agent_py_agent.agent.plugin_wheels import inspect_plugin_wheels
from agent_py_agent.tests.plugin_wheel_fixtures import change_wheel, make_wheel, package_wheels


def test_wheels_are_validated_without_import_process_or_network(monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("预检不得启动进程"))
    original = make_wheel(requires=("helper>=1",))
    package = package_wheels(original, make_wheel("helper"))
    result = inspect_plugin_wheels(package)
    assert [wheel.name for wheel in result] == ["peek", "helper"]
    assert result[0].requirements == ("helper>=1",)
    assert result[0].content == original[1]
    assert result[0].dist_info == "peek-1.0.dist-info"


@pytest.mark.parametrize(
    "wheels,reason",
    [
        ((make_wheel(requires=("missing>=1",)),), "wheel_dependency"),
        ((make_wheel(requires=("helper>=2",)), make_wheel("helper")), "wheel_dependency"),
        ((make_wheel(), make_wheel(version="2.0")), "wheel_duplicate"),
        ((make_wheel(python=">=99"),), "wheel_python"),
        ((make_wheel(tag="py2-none-any"),), "wheel_platform"),
        ((make_wheel(requires=("helper @ https://example.invalid/helper.whl",)),), "wheel_url_dependency"),
        ((make_wheel(requires=('helper @ https://example.invalid/x.whl ; python_version < "1"',)),), "wheel_url_dependency"),
        ((make_wheel(requires=("helper[fast]",)), make_wheel("helper")), "wheel_extra"),
        ((make_wheel(metadata_tail="Name: another\n"),), "invalid_wheel"),
        ((make_wheel(wheel_version="2.0"),), "invalid_wheel"),
    ],
)
def test_invalid_fixed_wheel_sets_fail_before_environment(wheels, reason):
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_wheels(package_wheels(*wheels))
    assert error.value.reason == reason
    assert "https" not in str(error.value)


def test_base_dependencies_of_every_supplied_wheel_are_required():
    package = package_wheels(make_wheel(), make_wheel("unused", requires=("missing",)))
    with pytest.raises(PluginPackageError, match="依赖"):
        inspect_plugin_wheels(package)


def test_markers_extras_and_cyclic_dependencies_use_packaging_semantics():
    root = make_wheel(requires=("helper[fast]", 'absent ; python_version < "1"'))
    helper = make_wheel("helper", extras=("fast", "unused"), requires=(
        'leaf>=1; extra == "fast"', 'unneeded ; extra == "unused"', "peek",
    ))
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_wheels(package_wheels(root, helper))
    assert error.value.reason == "wheel_dependency"
    assert len(inspect_plugin_wheels(package_wheels(root, helper, make_wheel("leaf")))) == 3


@pytest.mark.parametrize(
    "changes,remove",
    [
        ({"../escape": b"bad"}, ()),
        ({"peek/changed.py": b"bad"}, ()),
        ({"peek/__init__.py": b"changed"}, ()),
        ({"PEEK/__INIT__.py": b"alias"}, ()),
        ({"other-1.0.dist-info/METADATA": b"Name: other\n"}, ()),
        ({}, ("peek-1.0.dist-info/RECORD",)),
        ({}, ("peek-1.0.dist-info/METADATA",)),
        ({"peek-1.0.dist-info/RECORD": b"../escape,,\n"}, ()),
    ],
)
def test_members_records_and_metadata_are_checked(changes, remove):
    package = package_wheels(change_wheel(make_wheel(), changes=changes, remove=remove))
    with pytest.raises(PluginPackageError):
        inspect_plugin_wheels(package)


def test_wheel_directories_are_allowed_without_relaxing_outer_package():
    package = package_wheels(change_wheel(make_wheel(), changes={"peek/": b""}))
    assert inspect_plugin_wheels(package)[0].name == "peek"


def test_unicode_equivalent_names_in_one_wheel_are_rejected():
    package = package_wheels(make_wheel(files={"café.txt": b"same", "cafe\u0301.txt": b"same"}))
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_wheels(package)
    assert error.value.reason == "invalid_archive"


def test_compressed_tag_budget_is_checked_before_packaging_expands_it(monkeypatch):
    from agent_py_agent.agent import plugin_wheels

    compressed = ".".join(f"p{number}" for number in range(32))
    package = package_wheels(make_wheel(tag="-".join((compressed,) * 3)))
    monkeypatch.setattr(plugin_wheels, "parse_wheel_filename", lambda *_: pytest.fail("超预算不能展开标签"))
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_wheels(package)
    assert error.value.reason == "package_limit"


@pytest.mark.parametrize("name", ["INSTALLER", "installer", "REQUESTED", "direct_url.json"])
def test_wheels_cannot_preseed_installer_facts_or_case_aliases(name):
    package = package_wheels(make_wheel(files={f"peek-1.0.dist-info/{name}": b"pip\n"}))
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_wheels(package)
    assert error.value.reason == "invalid_wheel"


def test_untrusted_snapshot_cannot_replace_verified_manifest():
    package = package_wheels(make_wheel())
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_wheels(replace(package, manifest=replace(package.manifest, version="other")))
    assert error.value.reason == "package_integrity"


def test_wheel_metadata_and_aggregate_expansion_have_budgets():
    from agent_py_agent.agent.plugin_package import PackageReadLimits

    package = package_wheels(make_wheel(files={"data.txt": b"x" * 1024}), make_wheel("helper"))
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_wheels(package, limits=PackageReadLimits(expanded_bytes=1200, members=30))
    assert error.value.reason == "package_limit"

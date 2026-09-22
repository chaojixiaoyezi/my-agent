"""安装布局的纯文件计划测试，不执行 pip 或插件。"""

import pytest

from agent_py_agent.agent.plugin_manifest import PluginPackageError
from agent_py_agent.agent.plugin_wheel_layout import WheelInstallLayout, plan_wheel_installation
from agent_py_agent.agent.plugin_wheels import inspect_plugin_wheels
from agent_py_agent.tests.plugin_wheel_fixtures import make_wheel, package_wheels


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "python"
    site = root / "lib" / "site-packages"
    site.mkdir(parents=True)
    scripts = root / "bin"
    scripts.mkdir()
    (scripts / "python").write_bytes(b"interpreter")
    (root / "pyvenv.cfg").write_bytes(b"configuration")
    (site / "pip").mkdir()
    (site / "pip" / "__init__.py").write_bytes(b"installer")
    return WheelInstallLayout(root, site, site, scripts, root, root / "include")


@pytest.mark.parametrize("member", [
    "pip/added.py", "pip.py", "PIP/extra.py",
    "peek-1.0.data/scripts/python", "peek-1.0.data/data/pyvenv.cfg",
])
def test_wheels_cannot_replace_or_extend_bootstrap(layout, member):
    wheels = inspect_plugin_wheels(package_wheels(make_wheel(files={member: b"bad"})))
    with pytest.raises(PluginPackageError) as error:
        plan_wheel_installation(wheels, layout)
    assert error.value.reason == "wheel_collision"


@pytest.mark.parametrize("first,second", [
    ("shared/data.txt", "shared/data.txt"),
    ("shared", "shared/sub/file.txt"),
    ("shared/DATA.txt", "SHARED/data.txt"),
    ("shared/café.txt", "shared/cafe\u0301.txt"),
])
def test_cross_wheel_collisions_are_rejected_before_install(layout, first, second):
    wheels = inspect_plugin_wheels(package_wheels(
        make_wheel(files={first: b"a"}), make_wheel("helper", files={second: b"b"}),
    ))
    with pytest.raises(PluginPackageError) as error:
        plan_wheel_installation(wheels, layout)
    assert error.value.reason == "wheel_collision"


def test_namespace_directory_can_be_shared_by_distinct_files(layout):
    wheels = inspect_plugin_wheels(package_wheels(
        make_wheel(files={"namespace/one.py": b"a"}),
        make_wheel("helper", files={"namespace/two.py": b"b"}),
    ))
    assert len(plan_wheel_installation(wheels, layout)) == 10


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", "python"])
def test_generated_entry_points_participate_in_path_and_collision_checks(layout, name):
    wheels = inspect_plugin_wheels(package_wheels(make_wheel(files={
        "peek-1.0.dist-info/entry_points.txt": f"[console_scripts]\n{name} = peek:main\n".encode(),
    })))
    with pytest.raises(PluginPackageError):
        plan_wheel_installation(wheels, layout)


def test_data_scheme_and_root_members_cannot_alias(layout):
    wheels = inspect_plugin_wheels(package_wheels(make_wheel(files={
        "shared.py": b"a", "peek-1.0.data/purelib/shared.py": b"b",
    })))
    with pytest.raises(PluginPackageError, match="覆盖"):
        plan_wheel_installation(wheels, layout)


def test_existing_symlink_directory_is_rejected(layout, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (layout.purelib / "peek").symlink_to(outside, target_is_directory=True)
    wheels = inspect_plugin_wheels(package_wheels(make_wheel()))
    with pytest.raises(PluginPackageError):
        plan_wheel_installation(wheels, layout)
    assert not list(outside.iterdir())

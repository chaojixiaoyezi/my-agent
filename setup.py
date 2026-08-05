from __future__ import annotations

import runpy
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

_POLICY = runpy.run_path(str(Path(__file__).with_name("package_boundary_policy.py")))
is_dev_only_module = _POLICY["is_dev_only_module"]
forbidden_distribution_member = _POLICY["forbidden_distribution_member"]


class ProductionBuildPy(_build_py):
    """Exclude source-present developer harness modules from production wheels."""

    def run(self) -> None:
        # ``setuptools`` normally overlays the current source tree onto an
        # existing ``build/lib`` directory.  That leaves modules which were
        # deleted from the source tree in later wheels.  A distribution build
        # must be a projection of the current source tree, so rebuild its
        # staging directory from scratch every time.
        build_root = Path(self.build_lib)
        if build_root.exists():
            shutil.rmtree(build_root)
        super().run()
        # Remove source-present developer-only modules from the fresh staging
        # tree before setuptools assembles the wheel.
        if not build_root.exists():
            return
        for path in sorted(build_root.rglob("*"), reverse=True):
            if path.is_file():
                member = path.relative_to(build_root).as_posix()
                if forbidden_distribution_member(member):
                    path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()

    def find_package_modules(self, package: str, package_dir: str):
        modules = super().find_package_modules(package, package_dir)
        return [
            item
            for item in modules
            if not is_dev_only_module(f"{item[0]}.{item[1]}")
        ]


setup(cmdclass={"build_py": ProductionBuildPy})

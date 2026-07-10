from __future__ import annotations

import runpy
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

_POLICY = runpy.run_path(str(Path(__file__).with_name("package_boundary_policy.py")))
is_dev_only_module = _POLICY["is_dev_only_module"]
forbidden_distribution_member = _POLICY["forbidden_distribution_member"]


class ProductionBuildPy(_build_py):
    """Exclude source-present developer harness modules from production wheels."""

    def run(self) -> None:
        super().run()
        # setuptools reuses build/lib across invocations. Remove forbidden
        # output after every build so an old developer wheel cannot pollute a
        # later production wheel even when the source selection is now clean.
        build_root = Path(self.build_lib)
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

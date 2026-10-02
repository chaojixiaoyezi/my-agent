# LLM: 验证 A、B 两个样包 declaration.json 里的 capability.verification 块（能力包 v2 块 7）经真实构建器和真实校验器
#   原样往返、检查程序成员按 sha 钉住、启用前确认内容列出关联输入，并且声明的参数和输入真的能驱动包内检查程序
#   （模拟宿主：只把这一个成员拷成 verifier.py，用 -I -S 跑）。只用公开合成资料，不起 Gateway、不碰真实 owner home。
# 模块用途: 证明两个包的核验声明和检查器的命令行是一致的，宿主照声明跑就能拿到 pack_verifier_result.v1。

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.capability_verifier_consent import (
    CONSENT_KIND,
    verifier_confirmation_code,
    verifier_confirmation_details,
    verifier_confirmation_message,
    verifier_consent_matches,
)
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.tests.test_capability_package_examples import EXAMPLES
from agent_py_agent.tests.test_plugin_management import manager
from scripts.build_capability_package import build_capability_package

# 包 → (检查程序的关联输入参数名, 确认说明里应出现的输入描述, 宿主交给检查程序的样例文件：{target} 与各输入参数)
PACKAGES = {
    "drama-text-a": (["--source"], ["--source（任务开始时已有的文件，必需）"],
                     {"{target}": "resources/example-delivery.json", "--source": "resources/example-source.json"}),
    "drama-workflow-b": (["--handoff", "--baseline-project"],
                         ["--handoff（本回合写出的文件）", "--baseline-project（任务开始时已有的文件）"],
                         {"{target}": "resources/example-project.json", "--baseline-project": "resources/example-project.json"}),
}


# LLM: 用仓库唯一的构建脚本从样包目录构建，构建产物只写 pytest 临时目录。
# 函数用途: 返回（样包声明, 构建出的包字节）。
def _built(tmp_path: Path, package: str) -> tuple[dict, bytes]:
    root = EXAMPLES / package
    declaration = json.loads((root / "declaration.json").read_text(encoding="utf-8"))
    bundle = build_capability_package(declaration, root, tmp_path / f"{package}.zip")
    return declaration, bundle.read_bytes()


@pytest.mark.parametrize("package", PACKAGES)
def test_verification_block_roundtrips_and_pins_the_checker(tmp_path, package):
    declaration, raw = _built(tmp_path, package)
    inspected = inspect_plugin_package(raw)
    verification = inspected.manifest.capability.verification
    assert verification.to_payload() == declaration["capability"]["verification"]
    assert verification.input_policy == "preserve_originals"
    assert [item.required for item in verification.deliverables] == [True]
    [verifier] = verification.verifiers
    member = {item.path: item for item in inspected.manifest.files}[verifier.member]
    assert member.sha256 == hashlib.sha256((EXAMPLES / package / verifier.member).read_bytes()).hexdigest()
    assert member.executable is False, "检查程序只由宿主在沙箱里跑，包本身不声明可执行入口"


@pytest.mark.parametrize("package", PACKAGES)
def test_confirmation_lists_the_declared_inputs(tmp_path, package):
    flags, texts, _files = PACKAGES[package]
    _declaration, raw = _built(tmp_path, package)
    inspected = inspect_plugin_package(raw)
    details = verifier_confirmation_details(inspected.manifest, inspected.sha256)
    assert details["kind"] == CONSENT_KIND
    assert [entry["flag"] for entry in details["verifiers"][0]["inputs"]] == flags
    message = verifier_confirmation_message({**details, "confirm_code": verifier_confirmation_code(details)})
    assert all(text in message for text in texts), message


@pytest.mark.parametrize("package", PACKAGES)
def test_declared_args_and_inputs_drive_the_real_checker_like_the_host(tmp_path, package):
    _flags, _texts, files = PACKAGES[package]
    root = EXAMPLES / package
    declaration = json.loads((root / "declaration.json").read_text(encoding="utf-8"))
    [verifier] = declaration["capability"]["verification"]["verifiers"]
    shutil.copyfile(root / verifier["member"], tmp_path / "verifier.py")
    args = [str(root / files["{target}"]) if arg == "{target}" else arg for arg in verifier["args"]]
    for entry in verifier["inputs"]:
        if entry["flag"] in files:
            args += [entry["flag"], str(root / files[entry["flag"]])]
    process = subprocess.run([sys.executable, "-I", "-S", str(tmp_path / "verifier.py"), *args], cwd=tmp_path,
                             env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert process.returncode == 0 and not process.stderr, process.stderr
    result = json.loads(process.stdout)
    assert result["schema"] == "pack_verifier_result.v1" and result["valid"] is True, result["errors"]


@pytest.mark.parametrize("package", PACKAGES)
def test_enable_asks_for_confirmation_then_records_consent(tmp_path, package):
    _declaration, raw = _built(tmp_path, package)
    service, source = manager(tmp_path)
    source.write_bytes(raw)
    installed = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="inst")
    assert installed["state"] == "succeeded", installed
    preview = service.command(f"/plugins enable {package}", revision=service.catalog().revision, request_id="en1")
    confirmation = preview["details"]["confirmation"]
    assert preview["details"]["reason"] == "confirmation_required" and confirmation["kind"] == CONSENT_KIND
    assert service.installations.snapshot()[0].activation is None, "没确认前不启用"
    enabled = service.command(f"/plugins enable {package} --confirm {confirmation['confirm_code']}",
                              revision=service.catalog().revision, request_id="en2")
    assert enabled["state"] == "succeeded", enabled
    entry = service.installations.snapshot()[0]
    assert verifier_consent_matches(entry.manifest, entry.package_sha256, entry.activation.verifier_consent_sha256)

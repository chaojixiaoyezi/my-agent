"""环境组件使用的标准 wheel 夹具；测试产物不构成真实 TUI 交付。"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import zipfile

from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.tests.test_plugin_package import _manifest


# LLM: 仅生成测试包，不调用构建后端或网络；RECORD 保持标准格式，默认模块是不可执行的导入陷阱。
# 函数用途: 在临时测试中构造真正可由 pip 安装的 wheel，便于覆盖元数据与隔离行为。
def make_wheel(
    name="peek", version="1.0", *, requires=(), extras=(), python="", tag="py3-none-any",
    files=None, metadata_tail="", wheel_version="1.0",
):
    distribution = name.replace("-", "_")
    info = f"{distribution}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {item}\n" for item in requires)
    metadata += "".join(f"Provides-Extra: {item}\n" for item in extras)
    metadata += f"Requires-Python: {python}\n" if python else ""
    members = {f"{distribution}/__init__.py": b"raise RuntimeError('must not import')\n", **(files or {})}
    members[f"{info}/METADATA"] = (metadata + metadata_tail + "\n").encode()
    members[f"{info}/WHEEL"] = (
        f"Wheel-Version: {wheel_version}\nRoot-Is-Purelib: true\nTag: {tag}\n"
    ).encode()
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for path, data in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((path, f"sha256={digest}", str(len(data))))
    writer.writerow((f"{info}/RECORD", "", ""))
    members[f"{info}/RECORD"] = record.getvalue().encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in members.items():
            archive.writestr(path, data)
    return f"wheels/{distribution}-{version}-{tag}.whl", stream.getvalue()


# LLM: 描述与归档摘要同步生成，内部 wheel 的失败应由环境预检识别，不能误归为外包摘要篡改。
# 函数用途: 将合成 wheel 集合组装为原生产读取器接受的插件快照。
def package_wheels(*wheels):
    manifest = _manifest(wheels[0][1])
    manifest["entry_wheel"] = wheels[0][0]
    manifest["wheels"] = [{"path": path, "sha256": hashlib.sha256(data).hexdigest()} for path, data in wheels]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest))
        for path, data in wheels:
            archive.writestr(path, data)
    return inspect_plugin_package(stream.getvalue())


# LLM: 仅篡改测试归档；调用方可模拟坏 RECORD、目录和摘要，不把这些成员提取到磁盘。
# 函数用途: 对一个合法 wheel 精确增加、替换或删除成员，同时保留文件名。
def change_wheel(wheel, *, changes=None, remove=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(wheel[1])) as source, zipfile.ZipFile(stream, "w") as target:
        members = {name: source.read(name) for name in source.namelist() if name not in remove}
        members.update(changes or {})
        for path, data in members.items():
            target.writestr(path, data)
    return wheel[0], stream.getvalue()

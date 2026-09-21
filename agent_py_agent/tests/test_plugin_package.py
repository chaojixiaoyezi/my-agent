"""本地包合同测试；临时归档不安装依赖、不运行插件，也不替代真实 TUI 验收。"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import struct
import zipfile
from dataclasses import asdict, replace

import pytest

from agent_py_agent.agent.command_arguments import ArgumentSpec, CommandActionSpec
from agent_py_agent.agent.plugin_command_catalog import PluginCommandCatalog
from agent_py_agent.agent.plugin_commands import parse_plugin_command
from agent_py_agent.agent.plugin_manifest import PluginManifest, PluginPackageError
from agent_py_agent.agent.plugin_package import PackageReadLimits, read_plugin_package


# LLM: 合成 wheel 只是包中不可执行的测试字节，不能当作 pip 安装或进程隔离证据。
# 函数用途: 生成携带导入陷阱的 wheel，测试读取包时不会执行模块或写业务文件。
def _wheel() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("peek/__init__.py", "raise RuntimeError('must not import')\n")
        archive.writestr("peek-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
    return stream.getvalue()


# LLM: 此夹具使用公共命令声明的完整 JSON 形态，修改协议时同步目录往返测试。
# 函数用途: 构造最小只读包描述，保留真实 schema、文件摘要和模块入口之间的关联。
def _manifest(wheel: bytes) -> dict:
    return {
        "schema_version": "plugin_package.v1",
        "plugin_id": "sample-peek",
        "version": "1.0",
        "summary": "读取工作目录",
        "entry_module": "peek",
        "entry_wheel": "wheels/peek-1.0-py3-none-any.whl",
        "wheels": [
            {
                "path": "wheels/peek-1.0-py3-none-any.whl",
                "sha256": hashlib.sha256(wheel).hexdigest(),
            }
        ],
        "actions": [
            asdict(
                CommandActionSpec(
                    "read",
                    "读取文件",
                    (ArgumentSpec("path", "路径", required=True, path=True),),
                    kind="tool",
                    target="read",
                )
            )
        ],
        "default_action": "read",
        "tools": [
            {
                "name": "read",
                "description": "读取文本",
                "requested_effect": "read_only",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
        "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }


# LLM: extra 允许构造非法成员，生产读取器必须拒绝，测试者不把这些文件提取到磁盘。
# 函数用途: 创建一份可精确篡改描述及成员的 ZIP 字节快照。
def _bundle(
    *, change=None, raw_manifest=None, extra=(), compression=zipfile.ZIP_STORED, wheel_info=None
) -> bytes:
    wheel = _wheel()
    manifest = json.loads(json.dumps(_manifest(wheel)))
    if change:
        change(manifest)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        archive.writestr(
            "plugin.json", raw_manifest if raw_manifest is not None else json.dumps(manifest)
        )
        archive.writestr(wheel_info or "wheels/peek-1.0-py3-none-any.whl", wheel)
        for path, content in extra:
            archive.writestr(path, content)
    return stream.getvalue()


def test_package_read_is_pure_and_help_uses_existing_command_contract(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("读取不能启动进程"))
    source = tmp_path / "sample.zip"
    original = _bundle()
    source.write_bytes(original)
    package = read_plugin_package(source)
    assert package.sha256 == hashlib.sha256(original).hexdigest()
    assert package.archive_bytes == original
    assert set(tmp_path.iterdir()) == {source}
    command = package.manifest.command_spec
    assert command.enabled is False and command.activation_id == ""
    assert command.package_version == "1.0"
    catalog = PluginCommandCatalog("test-owner", plugins=(command,))
    restored = PluginCommandCatalog.from_payload(catalog.to_payload())
    parsed = parse_plugin_command(
        '/plugins@sample-peek read "中文 文件.txt"', plugins=restored.plugins
    )
    assert parsed.arguments.values["path"] == "中文 文件.txt"
    assert parsed.action.target == "read"
    source.write_bytes(b"changed after inspection")
    assert package.archive_bytes == original


def test_manifest_roundtrip_and_schema_projection_do_not_alias():
    payload = json.loads(json.dumps(_manifest(_wheel())))
    manifest = PluginManifest.from_payload(payload)
    payload["tools"][0]["input_schema"]["properties"].clear()
    projection = manifest.to_payload()
    restored = PluginManifest.from_payload(projection)
    assert restored == manifest
    projection["settings_schema"]["properties"]["forged"] = {"type": "boolean"}
    manifest.tools[0].input_schema["properties"].clear()
    assert "path" in manifest.tools[0].input_schema["properties"]
    assert not manifest.settings_schema["properties"]


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row.update(owner="forged"),
        lambda row: row.update(enabled=True),
        lambda row: row.update(activation_id="forged"),
        lambda row: row.update(environment={"TOKEN": "placeholder"}),
        lambda row: row.update(schema_version="plugin_package.v2"),
        lambda row: row.update(plugin_id="../outside"),
        lambda row: row.update(version=""),
        lambda row: row.update(entry_module="peek; whoami"),
        lambda row: row.update(entry_module="peek.__main__ --flag"),
        lambda row: row.update(entry_module="peek.class"),
        lambda row: row.update(entry_wheel="wheels/missing.whl"),
        lambda row: row["wheels"][0].update(path="../outside.whl"),
        lambda row: row["wheels"][0].update(sha256="0" * 63),
        lambda row: row["wheels"].append(row["wheels"][0]),
        lambda row: row["actions"][0].update(kind="management"),
        lambda row: row["actions"][0].update(target="run_command"),
        lambda row: row["actions"][0].update(available="true"),
        lambda row: row["actions"].append(row["actions"][0]),
        lambda row: row.update(default_action="missing"),
        lambda row: row["tools"][0].update(requested_effect="readonly"),
        lambda row: row["tools"].append(row["tools"][0]),
        lambda row: row["tools"][0].update(input_schema={"type": "array"}),
        lambda row: row["tools"][0].update(input_schema={"$ref": "https://example.invalid/schema"}),
        lambda row: row.update(settings_schema={"type": "string"}),
        lambda row: row.update(tools="read"),
        lambda row: row.update(summary="\ud800"),
        lambda row: row.update(version="\udfff"),
        lambda row: row["tools"][0].update(description="\ud800"),
        lambda row: row["tools"][0]["input_schema"].update(description="\ud800"),
        lambda row: row["settings_schema"].update(description="\ud800"),
        lambda row: row["actions"][0].update(summary="\ud800"),
        lambda row: row["actions"][0]["arguments"][0].update(summary="\ud800"),
    ],
)
def test_invalid_declarations_fail_before_any_installation(tmp_path, change):
    source = tmp_path / "invalid.zip"
    source.write_bytes(_bundle(change=change))
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "invalid_manifest"
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":"x","schema_version":"plugin_package.v1"}',
        b'{"nested":{"name":1,"name":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"[]",
        b"{",
        b"\xff",
    ],
)
def test_ambiguous_or_non_json_manifest_is_rejected(tmp_path, raw):
    source = tmp_path / "invalid.zip"
    source.write_bytes(_bundle(raw_manifest=raw))
    with pytest.raises(PluginPackageError, match="描述"):
        read_plugin_package(source)


@pytest.mark.parametrize(
    "member",
    [
        "../outside",
        "/absolute",
        "C:/outside",
        "wheels\\escape.whl",
        "wheels/../escape.whl",
        "PLUGIN.JSON",
        "wheels/PEEK-1.0-py3-none-any.whl",
        "unlisted.txt",
        "wheels/",
    ],
)
def test_invalid_archive_member_is_not_extracted(tmp_path, member):
    source = tmp_path / "invalid.zip"
    source.write_bytes(_bundle(extra=((member, b"never extract"),)))
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "invalid_archive"
    assert set(tmp_path.iterdir()) == {source}


def test_duplicate_member_rejected(tmp_path):
    source = tmp_path / "invalid.zip"
    with pytest.warns(UserWarning, match="Duplicate"):
        source.write_bytes(_bundle(extra=(("plugin.json", b"{}"),)))
    with pytest.raises(PluginPackageError):
        read_plugin_package(source)


@pytest.mark.parametrize(
    "file_type", [stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR]
)
def test_declared_wheel_must_be_a_regular_archive_member(tmp_path, file_type):
    source = tmp_path / "invalid.zip"
    member = zipfile.ZipInfo("wheels/peek-1.0-py3-none-any.whl")
    member.create_system = 3
    member.external_attr = (file_type | 0o777) << 16
    source.write_bytes(_bundle(wheel_info=member))
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "invalid_archive"


def test_tampered_wheel_digest_rejected(tmp_path):
    source = tmp_path / "invalid.zip"
    source.write_bytes(_bundle(change=lambda row: row["wheels"][0].update(sha256="0" * 64)))
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "digest_mismatch"


@pytest.mark.parametrize(
    "limits",
    [
        replace(PackageReadLimits(), archive_bytes=128),
        replace(PackageReadLimits(), expanded_bytes=128),
        replace(PackageReadLimits(), member_bytes=128),
        replace(PackageReadLimits(), manifest_bytes=128),
        replace(PackageReadLimits(), directory_bytes=64),
        replace(PackageReadLimits(), members=1),
    ],
)
def test_read_limits_are_checked_on_real_bytes(tmp_path, limits):
    source = tmp_path / "large.zip"
    source.write_bytes(_bundle(compression=zipfile.ZIP_DEFLATED))
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source, limits=limits)
    assert caught.value.reason == "package_limit"


@pytest.mark.parametrize("content", [b"", b"not a zip", _bundle()[:-100]])
def test_corrupt_archive_is_structured_failure(tmp_path, content):
    source = tmp_path / "broken.zip"
    source.write_bytes(content)
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "invalid_archive"


@pytest.mark.parametrize("encrypted", [False, True])
def test_invalid_deflate_or_encrypted_member_has_structured_failure(tmp_path, encrypted):
    source = tmp_path / "broken.zip"
    content = bytearray(_bundle(compression=zipfile.ZIP_DEFLATED))
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        member = archive.getinfo("plugin.json")
    if encrypted:
        central = content.index(b"PK\x01\x02")
        struct.pack_into("<H", content, central + 8, member.flag_bits | 1)
        struct.pack_into("<H", content, member.header_offset + 6, member.flag_bits | 1)
    else:
        name_size, extra_size = struct.unpack_from("<HH", content, member.header_offset + 26)
        content[member.header_offset + 30 + name_size + extra_size] = 0xFF
    source.write_bytes(content)
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "invalid_archive"


def test_invalid_utf8_central_directory_has_structured_failure(tmp_path):
    source = tmp_path / "broken.zip"
    content = bytearray(_bundle())
    central = content.index(b"PK\x01\x02")
    struct.pack_into("<H", content, central + 8, 0x800)
    content[central + 46] = 0xFF
    source.write_bytes(content)
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "invalid_archive"


@pytest.mark.parametrize("lie_about_count", [False, True])
def test_member_limit_is_enforced_before_zipfile_allocates_directory(
    tmp_path, monkeypatch, lie_about_count
):
    source = tmp_path / "too-many.zip"
    content = bytearray(_bundle(extra=tuple((f"extra-{index}", b"") for index in range(6))))
    if lie_about_count:
        end = content.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", content, end + 8, 2, 2)
    source.write_bytes(content)
    monkeypatch.setattr(zipfile, "ZipFile", lambda *args, **kwargs: pytest.fail("不应构造完整目录"))
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source, limits=replace(PackageReadLimits(), members=4))
    assert caught.value.reason == "package_limit"


# LLM: 该夹具只包含小型内存目录，没有文件正文或可执行内容；验证 ZIP64 不能改变已经预检的目录范围。
# 函数用途: 构造普通尾部计数为一、实际 ZIP64 重定向目录的畸形包，用于元数据预算回归。
def _zip64_directory_in_member_comment() -> bytes:
    entries = []
    for index in range(7):
        name = f"m{index}".encode()
        header = bytearray(46)
        header[:4] = b"PK\x01\x02"
        struct.pack_into("<HHH", header, 28, len(name), 0, 76 if index == 6 else 0)
        entries.append(bytes(header) + name)
    directory = b"".join(entries)
    zip64 = struct.pack("<4sQHHLLQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, 7, 7, len(directory), 0)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, len(directory), 1)
    last_offset = len(directory) - len(entries[-1])
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(entries[-1]) + 76, last_offset, 0)
    return directory + zip64 + locator + end


def test_hidden_zip64_locator_cannot_redirect_the_prechecked_directory(tmp_path, monkeypatch):
    source = tmp_path / "bad-directory.zip"
    content = _zip64_directory_in_member_comment()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert len(archive.infolist()) == 7
    source.write_bytes(content)
    monkeypatch.setattr(
        zipfile, "ZipFile", lambda *args, **kwargs: pytest.fail("不能分配另一个目录")
    )
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source, limits=replace(PackageReadLimits(), members=4))
    assert caught.value.reason == "invalid_archive"


@pytest.mark.parametrize("field,value", [(4, 1), (10, 65535), (16, 0xFFFFFFFF), (20, 1)])
def test_unsupported_zip_directory_or_bad_boundary_fails_before_parsing(
    tmp_path, monkeypatch, field, value
):
    source = tmp_path / "broken.zip"
    content = bytearray(_bundle())
    end = content.rfind(b"PK\x05\x06")
    struct.pack_into("<L" if field == 16 else "<H", content, end + field, value)
    source.write_bytes(content)
    monkeypatch.setattr(zipfile, "ZipFile", lambda *args, **kwargs: pytest.fail("不应构造完整目录"))
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "invalid_archive"


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "128"])
def test_invalid_read_limit_cannot_disable_the_budget(value):
    with pytest.raises(ValueError):
        PackageReadLimits(archive_bytes=value)


def test_missing_file_is_a_read_failure_without_private_path_in_message(tmp_path):
    source = tmp_path / "private-source.zip"
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "source_unavailable"
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.skipif(not hasattr(__import__("os"), "mkfifo"), reason="平台没有 FIFO")
def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    import os

    source = tmp_path / "pipe"
    os.mkfifo(source)
    with pytest.raises(PluginPackageError) as caught:
        read_plugin_package(source)
    assert caught.value.reason == "invalid_source"

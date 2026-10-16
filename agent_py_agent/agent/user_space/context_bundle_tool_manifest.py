# LLM: 完整工具清单按内容哈希在本用户目录内只存一份（memory_archive/snapshots/tool_manifests/<sha>.json）；
#   快照里只留摘要（小字段 + manifest_sha256 + manifest_ref）。读取一律走 load_bundle_tool_manifest：
#   它从"快照文件自己的路径"推出本用户归档根、按 sha 定位清单、realpath 校验归档边界、校验文件字节的
#   sha256、报结构化错误，并兼容旧格式内联清单（tool_manifest 自带 tools 的旧快照）；不信任快照里写的
#   绝对路径。只有 save=True 且存在 owner home 时才有写文件副作用；save=False 的快照没有落盘清单。
#   清单内容不含 run_id / snapshot_hash / tool_runtimes，保证同一工具集跨请求复用同一个文件；
#   已存在的清单只 touch mtime，配合保留期清理的"30 天 + 引用检查"判定（E1c）。
# 模块用途: 工具清单的内容寻址存储与读取（E1b 快照瘦身）；写入与快照同口径（0700 目录 / 0600 文件私有原子写）。
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..common.json_io import ensure_private_directory_chain, write_private_text_file_atomic

# 摘要形状版本：目标已有 v3 内联完整清单，v4 显式表示摘要 + manifest_ref；完整内容继续沿用原 v3 合同。
# 旧内联快照仍由 tools 字段显式识别，不把旧 schema_version=3 静默改成引用语义。
TOOL_MANIFEST_SUMMARY_SCHEMA_VERSION = 4
# 完整清单的存放位置（相对 memory_archive），按用户目录隔离，不跨用户共享。
_TOOL_MANIFEST_DIR_PARTS = ("snapshots", "tool_manifests")
# 不参与内容哈希的字段：run_id / snapshot_hash 每次运行都不同，tool_runtimes 与 tools 重复。
_TOOL_MANIFEST_EXCLUDED_FIELDS = ("run_id", "snapshot_hash", "tool_runtimes")


# LLM: 读取失败必须是结构化错误（code 机器可判），调用方按 code 分支、不解析 detail 文本。
# 类用途: load_bundle_tool_manifest 失败时抛出，携带 code / manifest_ref / detail 三个结构化字段。
class ToolManifestLoadError(Exception):
    # LLM: code 是调用方唯一分支依据；诊断只携引用和错误，不包含工具正文。
    # 函数用途: 保存清单读取失败的结构化原因。
    def __init__(self, code: str, *, manifest_ref: str = "", detail: str = "") -> None:
        message = f"{code}: {detail}" if detail else code
        super().__init__(message)
        self.code = code
        self.manifest_ref = manifest_ref
        self.detail = detail


# LLM: 规范文本 = 紧凑分隔符 + sort_keys + 原样中文；清单文件字节就是这份文本，
#   所以 sha256 既是对内容的哈希也是对文件的哈希——校验时对读到的字节重算，被改一个字节都会被抓到。
# 函数用途: 生成清单文件的规范 JSON 文本（内容哈希与文件字节的唯一来源）。
def manifest_content_text(content: dict[str, Any]) -> str:
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# LLM: 摘要必须由实际将写入的规范 UTF-8 字节计算，不能依赖 Python 对象相等。
# 函数用途: 计算清单内容（规范 JSON 文本）的 sha256 十六进制摘要。
def manifest_content_sha256(content: dict[str, Any]) -> str:
    return _sha256_bytes(manifest_content_text(content).encode("utf-8"))


# LLM: 清单内容去掉每次运行都不同的字段（run_id / snapshot_hash）和与 tools 重复的 tool_runtimes；
#   tool_load_errors 保留，保证"解引用后与旧格式内联清单（去三字段）逐字段相等"。
# 函数用途: 从完整清单 payload 生成按内容哈希寻址的清单文件内容。
def tool_manifest_file_content(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in _TOOL_MANIFEST_EXCLUDED_FIELDS
    }


# LLM: 与 context_bundle._memory_archive_root 同口径：优先 owner_home_dir/memory_archive，
#   退化到 memory_archive_dir；两处都按"本用户目录"解析，不跨用户共享。
# 函数用途: 从 home_paths 解析本用户的 memory_archive 根目录。
def _memory_archive_root(home_paths: Any) -> Path:
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home:
        return Path(owner_home) / "memory_archive"
    return Path(home_paths.memory_archive_dir)


# LLM: 写入寻址与读取校验共用 SHA-256，改动需同步验证清单旧引用。
# 函数用途: 对字节串计算 sha256 十六进制摘要（文本与文件校验共用同一实现）。
def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# LLM: 内容相同只刷新 mtime；缺失或字节损坏时原子修复；所有路径均按当前 owner 归档解析，不能写越界链接。
#   touch 是清理保护事实；失败时拒绝发布新快照引用，不依赖已经结束的引用扫描兜底。
# 函数用途: 写入或复用本 owner 的内容寻址清单，返回诊断用路径；写入保持 0600 私有原子语义。
def _store_manifest_text(home_paths: Any, sha: str, text: str) -> str:
    archive = Path(os.path.abspath(_memory_archive_root(home_paths)))
    directory = archive.joinpath(*_TOOL_MANIFEST_DIR_PARTS)
    path = directory / f"{sha}.json"
    _assert_manifest_owner_boundary(archive, path, str(path))
    try:
        unchanged = path.read_bytes() == text.encode("utf-8")
    except FileNotFoundError:
        unchanged = False
    if not unchanged:
        ensure_private_directory_chain(archive, directory)
        write_private_text_file_atomic(path, text, ensure_parent_private=False)
    else:
        try:
            os.utime(path)
        except OSError as exc:
            raise ToolManifestLoadError(
                "tool_manifest_reuse_unprotected", manifest_ref=str(path),
                detail="不能刷新清单保留保护，未发布新快照引用",
            ) from exc
    return str(path)


# LLM: 清单候选必须仍在当前 owner 的真实归档根内；不信 manifest_ref，也不跟随越界目录或文件链接。
# 函数用途: 在读写清单前校验真实路径归属，返回边界内的真实路径。
def _assert_manifest_owner_boundary(archive: Path, path: Path, ref: str) -> Path:
    resolved = Path(os.path.realpath(path))
    resolved_root = Path(os.path.realpath(archive))
    if not resolved.is_relative_to(resolved_root):
        raise ToolManifestLoadError(
            "tool_manifest_outside_owner_archive", manifest_ref=ref,
            detail=f"{resolved} 不在归档根 {resolved_root} 内",
        )
    return resolved


# LLM: 摘要只留小字段 + 引用；只有 save=True 且存在 owner home 时才写清单文件——"不保存快照"必须
#   没有落盘副作用。save=False 时 manifest_ref 留空、sha 照填，读取端见到空 ref 就报
#   tool_manifest_not_persisted（见 load_bundle_tool_manifest）。manifest_ref 只作展示与诊断，
#   读取端不信任它（可能被篡改，也可能因目录搬迁失效）。
# 函数用途: 生成快照里的 tool_manifest 摘要；仅在保存快照时把完整清单写入本用户目录（有写文件副作用）。
def build_tool_manifest_summary(
    payload: dict[str, Any], home_paths: Any | None, *, save: bool
) -> dict[str, Any]:
    content = tool_manifest_file_content(payload)
    text = manifest_content_text(content)
    sha = _sha256_bytes(text.encode("utf-8"))
    if home_paths is None or not save:
        manifest_ref = ""
    else:
        manifest_ref = _store_manifest_text(home_paths, sha, text)
    return {
        "schema_name": str(payload.get("schema_name") or ""),
        "schema_version": TOOL_MANIFEST_SUMMARY_SCHEMA_VERSION,
        "run_id": str(payload.get("run_id") or ""),
        "snapshot_hash": str(payload.get("snapshot_hash") or ""),
        "owner_type": str(payload.get("owner_type") or ""),
        "permission_mode": str(payload.get("permission_mode") or ""),
        "permission_scope": str(payload.get("permission_scope") or ""),
        "allowed_tools": payload.get("allowed_tools"),
        "visible_tools": list(payload.get("visible_tools") or []),
        "executable_tools": list(payload.get("executable_tools") or []),
        "unavailable_tools": list(payload.get("unavailable_tools") or []),
        "tool_load_errors": list(payload.get("tool_load_errors") or []),
        "manifest_sha256": sha,
        "manifest_ref": manifest_ref,
    }


# LLM: 旧格式（tool_manifest 自带 tools）直接返回内联清单，不报错；新格式不信任快照里写的
#   manifest_ref（可能被篡改，也可能因目录搬迁失效），而是从"快照文件自己的路径"推出本用户
#   memory_archive 根，按 manifest_sha256 在 snapshots/tool_manifests/ 下定位；realpath 后必须
#   仍在该归档根内，否则报 tool_manifest_outside_owner_archive。save=False 的快照没有落盘清单
#   （ref 为空），读取报 tool_manifest_not_persisted。需要完整清单的产品代码和测试都走这个函数。
# 函数用途: 从快照 bundle 解引用出完整工具清单；失败抛 ToolManifestLoadError。
def load_bundle_tool_manifest(
    bundle: dict[str, Any], *, snapshot_path: str | Path | None = None
) -> dict[str, Any]:
    manifest = bundle.get("tool_manifest") if isinstance(bundle, dict) else None
    if not isinstance(manifest, dict) or not manifest:
        raise ToolManifestLoadError("tool_manifest_missing")
    if "tools" in manifest:
        return manifest
    ref = str(manifest.get("manifest_ref") or "").strip()
    sha = str(manifest.get("manifest_sha256") or "").strip()
    if not ref or not sha:
        raise ToolManifestLoadError(
            "tool_manifest_not_persisted",
            manifest_ref=ref,
            detail="快照未保存（save=False）或摘要缺少 manifest_ref / manifest_sha256",
        )
    if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
        raise ToolManifestLoadError("tool_manifest_sha256_invalid", manifest_ref=ref)
    path = _resolve_manifest_path(snapshot_path, sha, ref)
    raw = _read_manifest_bytes(path, ref)
    if _sha256_bytes(raw) != sha:
        raise ToolManifestLoadError("tool_manifest_sha256_mismatch", manifest_ref=ref)
    return _parse_manifest_bytes(raw, ref)


# LLM: 清单位置 = 快照所在归档根 + snapshots/tool_manifests/<sha>.json；快照路径缺失或不在
#   snapshots/ 树下时报 tool_manifest_snapshot_path_invalid。realpath 双端解析后再比，
#   清单目录被换成指向归档外的符号链接也会被拒（tool_manifest_outside_owner_archive）。
# 函数用途: 由快照路径与内容哈希推算并校验清单文件路径（不读快照里写的绝对路径）。
def _resolve_manifest_path(snapshot_path: str | Path | None, sha: str, ref: str) -> Path:
    archive_root = _archive_root_from_snapshot_path(snapshot_path)
    if archive_root is None:
        raise ToolManifestLoadError(
            "tool_manifest_snapshot_path_invalid",
            manifest_ref=ref,
            detail=f"snapshot_path={snapshot_path!r}",
        )
    candidate = archive_root.joinpath(*_TOOL_MANIFEST_DIR_PARTS, f"{sha}.json")
    return _assert_manifest_owner_boundary(archive_root, candidate, ref)


# LLM: 归档锚点只来自调用方持有的实际快照路径；搬迁后仍按相对归档布局定位。
# 函数用途: 从快照文件路径向上找最近的 snapshots 目录，返回其父目录（本用户 memory_archive 根）。
def _archive_root_from_snapshot_path(snapshot_path: str | Path | None) -> Path | None:
    if not snapshot_path:
        return None
    for parent in Path(snapshot_path).parents:
        if parent.name == "snapshots":
            return parent.parent
    return None


# LLM: 读取失败必须转成可判断的错误码；不得换 owner 或回退 manifest_ref 指向的文件。
# 函数用途: 读清单文件字节；读不到时抛结构化错误（文件被删/不可读）。
def _read_manifest_bytes(path: Path, ref: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ToolManifestLoadError(
            "tool_manifest_file_missing", manifest_ref=ref, detail=str(exc)
        ) from exc


# LLM: 完整清单必须是 UTF-8 JSON 对象；损坏内容不能被当成有效空清单。
# 函数用途: 解析清单文件字节；不是 JSON 对象时抛结构化错误。
def _parse_manifest_bytes(raw: bytes, ref: str) -> dict[str, Any]:
    try:
        content = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ToolManifestLoadError(
            "tool_manifest_file_invalid", manifest_ref=ref, detail=str(exc)
        ) from exc
    if not isinstance(content, dict):
        raise ToolManifestLoadError("tool_manifest_file_invalid", manifest_ref=ref)
    return content


__all__ = [
    "ToolManifestLoadError",
    "build_tool_manifest_summary",
    "load_bundle_tool_manifest",
    "manifest_content_sha256",
    "manifest_content_text",
    "tool_manifest_file_content",
]

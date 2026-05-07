# LLM: 这些方法会批量触碰数据库和正文文件，改动时优先保护可恢复性。
# 模块用途: LocalStore 维护操作，包括重建索引、清库、统计和缺失文件检查。

from __future__ import annotations

"""implements LocalStore maintenance, statistics, reset, FTS rebuild, and integrity checks.

给人看的解释：
这个文件只管维护类动作。
比如重建 FTS、清空索引准备重建、统计每种来源有多少记录，以及检查正文文件有没有丢。
"""

from typing import Any


# LLM: LocalStoreMaintenanceMixin 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStoreMaintenanceMixin 封装 LocalStore 本地事实索引 的一组相关操作，供上层组合调用。
class LocalStoreMaintenanceMixin:

    # LLM: LocalStoreMaintenanceMixin.rebuild_fts 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 重建 rebuild_fts，让派生索引重新对齐主记录。
    def rebuild_fts(self) -> int:
        """用 `records` 表和文件系统内容重建 FTS5 索引。"""

        if not self.fts_available:
            return 0
        with self._connection() as conn:
            conn.execute("DELETE FROM records_fts")
            rows = conn.execute("SELECT id, title, content_path, content_preview FROM records").fetchall()
            count = 0
            for row in rows:
                content = self._read_content(row)
                self._replace_fts_row(conn, row["id"], row["title"], content)
                count += 1
            self._record_event(conn, "fts_rebuilt", "", {"count": count})
            conn.commit()
        return count

    # LLM: LocalStoreMaintenanceMixin.reset 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 清理 reset 相关状态，并让调用方知道是否完成。
    def reset(self, *, remove_content_files: bool = False, reset_events_file: bool = True) -> None:
        with self._connection() as conn:
            if self.fts_available:
                conn.execute("DELETE FROM records_fts")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM records")
            conn.commit()
        if reset_events_file:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            self.events_path.write_text("", encoding="utf-8")
        if remove_content_files:
            self._remove_content_files()

    # LLM: LocalStoreMaintenanceMixin._remove_content_files 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 清理 remove_content_files 相关状态，并让调用方知道是否完成。
    def _remove_content_files(self) -> None:
        """Best-effort cleanup for content files after database reset."""
        if not self.files_dir.exists():
            return
        for path in self.files_dir.glob("*.txt"):
            try:
                path.unlink()
            except OSError:
                continue

    # LLM: LocalStoreMaintenanceMixin.count_records 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 汇总 count_records 的统计信息供状态页或诊断使用。
    def count_records(self, *, source_type: str | None = None) -> int:
        """统计记录数，可按 source_type 过滤。"""

        where, params = self._record_filters(source_type=source_type)
        with self._connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM records {where}", params).fetchone()[0])

    # LLM: LocalStoreMaintenanceMixin.source_counts 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 汇总 source_counts 的统计信息供状态页或诊断使用。
    def source_counts(self) -> dict[str, int]:
        """按 source_type 汇总记录数。"""

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT source_type, COUNT(*) AS count FROM records GROUP BY source_type ORDER BY source_type"
            ).fetchall()
        return {str(row["source_type"]): int(row["count"]) for row in rows}

    # LLM: LocalStoreMaintenanceMixin.missing_content_files 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 missing_content_files 步骤，并保持调用方依赖的数据形状。
    def missing_content_files(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """检查 records 指向的正文文件是否还存在。"""

        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_id, title, content_path
                FROM records
                ORDER BY updated_at DESC
                """
            ).fetchall()
        missing: list[dict[str, Any]] = []
        for row in rows:
            path = self._resolve_content_path(row["content_path"])
            if path.exists():
                continue
            missing.append(
                {
                    "id": row["id"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "title": row["title"],
                    "content_path": str(path),
                }
            )
            if limit > 0 and len(missing) >= limit:
                break
        return missing

    # LLM: LocalStoreMaintenanceMixin.stats 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 汇总 stats 的统计信息供状态页或诊断使用。
    def stats(self) -> dict[str, Any]:
        """返回本地事实源的当前状态。"""

        with self._connection() as conn:
            record_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "files_dir": str(self.files_dir),
            "events_path": str(self.events_path),
            "fts5_enabled": self.fts_available,
            "record_count": record_count,
            "event_count": event_count,
        }

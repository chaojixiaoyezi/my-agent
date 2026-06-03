
from __future__ import annotations

"""implements LocalStore maintenance, statistics, reset, FTS rebuild, and integrity checks.

这个文件只管维护类动作。
比如重建 FTS、清空索引准备重建、统计每种来源有多少记录，以及检查正文文件有没有丢。
"""

from typing import Any


class LocalStoreMaintenanceMixin:

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

    def _remove_content_files(self) -> None:
        """Best-effort cleanup for content files after database reset."""
        if not self.files_dir.exists():
            return
        for path in self.files_dir.glob("*.txt"):
            try:
                path.unlink()
            except OSError:
                continue

    def count_records(self, *, source_type: str | None = None) -> int:
        """统计记录数，可按 source_type 过滤。"""

        where, params = self._record_filters(source_type=source_type)
        with self._connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM records {where}", params).fetchone()[0])

    def source_counts(self) -> dict[str, int]:
        """按 source_type 汇总记录数。"""

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT source_type, COUNT(*) AS count FROM records GROUP BY source_type ORDER BY source_type"
            ).fetchall()
        return {str(row["source_type"]): int(row["count"]) for row in rows}

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

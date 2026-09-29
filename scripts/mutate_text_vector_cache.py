#!/usr/bin/env python3
"""任务 9 向量缓存的变异验证脚本（提交进仓库，不留在 /tmp）。

用法：在仓库根目录执行 ``python3 scripts/mutate_text_vector_cache.py``。
每个变异体对源码做**精确替换**（要求锚点唯一）→ 跑 ``tests/test_memory_vector_cache.py`` → 还原。
KILLED = 测试变红（说明测试钉住了这条行为）；SURVIVED = 测试仍全绿（盲区，必须补测试或删冗余判据）。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "agent_py_agent"
TESTS = "tests/test_memory_vector_cache.py"

CACHE = PKG / "agent/retrieval/text_vector_cache.py"
HYBRID = PKG / "agent/retrieval/hybrid.py"
JSONL = PKG / "agent/memory_store/jsonl.py"
MAINT = PKG / "agent/user_space/owner_maintenance.py"

# (编号, 文件:行锚点, 说明, 原文, 替换后, 预期变红的测试)
MUTANTS = [
    (
        "M1", "jsonl.py:_live_cache_keys",
        "回写不做 active 身份复核（复活已删/被替换事实的向量）",
        JSONL,
        "            cache.put(rows, keep=lambda: self._live_cache_keys(fingerprint))",
        "            cache.put(rows)",
        "test_p5_delete_during_embed_leaves_no_orphan",
    ),
    (
        "M2", "jsonl.py:_text_vector_cache",
        "缓存不再独立文件（写回权威向量库）",
        JSONL,
        "            self._text_vector_cache_obj = TextVectorCache(self.path.parent / \"memory_text_vectors.json\")",
        "            self._text_vector_cache_obj = TextVectorCache(self.path.parent / \"memory_vectors.json\")",
        "test_cache_lives_in_its_own_file_and_never_rewrites_plaintext_store",
    ),
    (
        "M3", "hybrid.py:_vector_order",
        "去掉向量长度守卫（长度不一致也当命中）",
        HYBRID,
        "            if not (isinstance(cache.get(doc_id), list) and len(cache[doc_id]) == qlen)",
        "            if not isinstance(cache.get(doc_id), list)",
        "test_cache_vector_length_mismatch_is_rembedded_not_used",
    ),
    (
        "M4", "text_vector_cache.py:embedder_fingerprint",
        "缓存键只用模型名、不含端点指纹",
        CACHE,
        "    payload = f\"{type(embedder).__name__}\\x00{api_base}\\x00{model}\"",
        "    payload = f\"{type(embedder).__name__}\\x00{model}\"",
        "test_fingerprint_differs_on_endpoint_and_never_leaks_key",
    ),
    (
        "M5", "jsonl.py:_forget_cached_vectors",
        "清理键退回裸正文（keywords_en 事实删不掉）",
        JSONL,
        "                self._cache_key(fingerprint, index_text(content, attributes))",
        "                self._cache_key(fingerprint, content)",
        "test_p1_keywords_record_cache_is_removed",
    ),
    (
        "M6", "jsonl.py:superseded_versions",
        "replace 不清被覆盖的旧版本键",
        JSONL,
        "                    if record.action == \"replace\" and (prior := latest_events.get(record.entry_id)) is not None",
        "                    if False",
        "test_p2_replace_drops_old_text_key",
    ),
    (
        "M7", "jsonl.py:index_all",
        "index_all 不回收孤儿键",
        JSONL,
        "            # 即便没配 LocalStore，正文哈希缓存仍可能积累孤儿键；回收不依赖 FTS 索引。\n            self._retain_text_cache_keys()\n            return 0",
        "            return 0",
        "test_index_all_reclaims_even_without_local_store",
    ),
    (
        "M8", "text_vector_cache.py:_flush(removed)",
        "remove 不回读磁盘（删除被旧快照加回）",
        CACHE,
        "                for key in removed:\n                    on_disk.pop(key, None)\n",
        "",
        "test_x1_flush_does_not_resurrect_key_deleted_by_other_instance",
    ),
    (
        "M9", "jsonl.py:_cached_vectors_for",
        "健康状态成功时不清除错误",
        JSONL,
        "            found = cache.get(list(keys.values()))\n            self._record_semantic_health(\"cache_read\")",
        "            found = cache.get(list(keys.values()))",
        "test_h1_cache_read_success_restores_health",
    ),
    (
        "MX", "text_vector_cache.py:_flush(merged)",
        "跨进程合并改成'只写整份内存快照'（别人删掉的键被写回）",
        CACHE,
        "                on_disk.update(added)",
        "                on_disk.update(self._items)",
        "test_x1_flush_does_not_resurrect_key_deleted_by_other_instance",
    ),
    (
        "MP5B", "text_vector_cache.py:put(keep)",
        "put 忽略锁内 keep 判据（P5b 窗口重新打开）",
        CACHE,
        "            if keep is not None:\n                valid = keep()\n                added = {key: vector for key, vector in added.items() if key in valid}\n",
        "",
        "test_p5b_put_rechecks_validity_inside_lock",
    ),
    (
        "MLOAD", "text_vector_cache.py:_load",
        "_load 遇到坏值整体失败（缓存被永久关掉）",
        CACHE,
        "            try:\n                items[key] = [float(x) for x in value]\n            except (TypeError, ValueError):\n                continue  # 该键含非数值：只丢这条，其余键照常可用\n",
        "            items[key] = [float(x) for x in value]\n",
        "test_load_skips_bad_value_but_keeps_other_keys",
    ),
    (
        "MY1", "owner_maintenance.py:_reclaim_text_vector_cache_orphans",
        "维护回收函数开头直接 return 0（空转：天天写假的 reclaimed: 0）",
        MAINT,
        "    try:\n        from ..memory_store.jsonl import JsonlMemory\n",
        "    return 0\n    try:\n        from ..memory_store.jsonl import JsonlMemory\n",
        "test_maintenance_reclaims_orphan_on_real_layout",
    ),
    (
        "MY2", "text_vector_cache.py:_flush 主路径写失败",
        "主路径 rename 失败不留错误（健康状态看不出来）",
        CACHE,
        '                    self.last_write_error = OSError("cache write failed")\n',
        "",
        "test_w1_primary_write_failure_records_error",
    ),
    (
        "MY3", "text_vector_cache.py:_flush keep 位置",
        "把 keep() 挪到文件锁之外（P5c 窗口重新打开）",
        CACHE,
        "                if keep is not None:\n                    valid = keep()\n                    added = {key: vector for key, vector in added.items() if key in valid}\n",
        "",
        "test_p5c_keep_is_evaluated_inside_the_file_lock",
    ),
    (
        "MYD", "text_vector_cache.py:remove",
        "remove 回到只看本实例内存（P5d 跨实例孤儿）",
        CACHE,
        "            self._flush(removed=targets)",
        "            if dropped:\n                self._flush(removed=targets)",
        "test_p5d_remove_subtracts_from_disk_even_if_not_in_memory",
    ),
    (
        "MYX", "text_vector_cache.py:_flush 锁失败回退",
        "拿锁失败时仍写盘（X1b 复活已删键）",
        CACHE,
        "            self.last_write_error = exc\n            for key in removed:\n                self._items.pop(key, None)\n            self._items.update(added)",
        "            self.last_write_error = exc\n            for key in removed:\n                self._items.pop(key, None)\n            self._items.update(added)\n            self._flush_unlocked()",
        "test_x1b_lock_failure_does_not_write",
    ),
    (
        "MY7", "text_vector_cache.py:__init__ 构造宽松",
        "_load 改回吞掉所有 OSError（构造时把读错误抛出，add/search 整次失败）",
        CACHE,
        "        try:\n            self._items: dict[str, list[float]] = self._load()\n        except OSError as exc:\n            self.last_read_error = exc\n            self._items = {}",
        "        self._items: dict[str, list[float]] = self._load()",
        "test_v1_add_succeeds_when_cache_unreadable / test_v2_search_degrades_when_cache_unreadable",
    ),
    (
        "MY8", "jsonl.py:_remember_cached_vectors 健康上报",
        "不把 cache.last_write_error 报给健康状态（写失败隐身）",
        JSONL,
        "            reported = getattr(cache, \"last_write_error\", None)\n            if reported:\n                self._record_semantic_health(\"cache_write\", reported)",
        "",
        "test_w1_write_failure_reaches_jsonl_health",
    ),
    (
        "MY9", "text_vector_cache.py:_flush 跳过写",
        "去掉「盘上内容没变就跳过重写」（V4：无意义整文件重写回来）",
        CACHE,
        "                if on_disk == before:\n                    self._items = on_disk\n                    return\n",
        "",
        "test_v4_remove_absent_key_does_not_rewrite_file",
    ),
    (
        "MY10", "text_vector_cache.py:_flush 跳过写的判据",
        "跳过写改成只看本实例内存（P5d 跨实例键的删除被漏掉）",
        CACHE,
        "                if on_disk == before:",
        "                if not added and not (removed & set(self._items)):",
        "test_v4_remove_present_key_still_rewrites / test_p5d_remove_subtracts_from_disk_even_if_not_in_memory",
    ),
    (
        "MY11", "jsonl.py:reclaim 错误带出",
        "回收只回条数、不把缓存读错误带出来（V5：维护状态又写假的空错误）",
        JSONL,
        "        error = getattr(cache, \"last_write_error\", None) or getattr(cache, \"last_read_error\", None)\n        if error is not None:\n            return dropped, f\"{type(error).__name__}: {error}\"\n        return dropped, \"\"",
        "        return dropped, \"\"",
        "test_v5_reclaim_reports_error_when_cache_corrupt",
    ),
    (
        "MY12", "text_vector_cache.py:_load 坏 JSON 记录读错误",
        "坏 JSON 不记读错误（回收把读不出内容当成没有孤儿）",
        CACHE,
        "            self.last_read_error = exc\n            return {}\n        if not isinstance(raw, dict):",
        "            return {}\n        if not isinstance(raw, dict):",
        "test_v5_reclaim_reports_error_when_cache_corrupt",
    ),
]


def run_tests() -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider", "--no-header", "-x"],
        cwd=PKG,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    if not run_tests():
        print("基线未通过，先修基线")
        return 1
    killed = survived = skipped = 0
    for name, anchor, desc, path, old, new, expected in MUTANTS:
        src = path.read_text(encoding="utf-8")
        hits = src.count(old)
        if hits != 1:
            skipped += 1
            print(f"SKIP      {name} [{anchor}] {desc}：锚点命中 {hits} 次（需唯一）")
            continue
        path.write_text(src.replace(old, new), encoding="utf-8")
        try:
            green = run_tests()
        finally:
            path.write_text(src, encoding="utf-8")
        if green:
            survived += 1
            print(f"SURVIVED  {name} [{anchor}] {desc}（预期应被 {expected} 杀死）")
        else:
            killed += 1
            print(f"KILLED    {name} [{anchor}] {desc}")
    print(f"\n== killed={killed} survived={survived} skipped={skipped} ==")
    return 0 if survived == 0 and skipped == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

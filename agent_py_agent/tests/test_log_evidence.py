from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EVIDENCE_MODULE = "agent_py_agent.agent.log_analysis.cases.evidence"


def _run_evidence_code(code_body: str) -> None:
    code_body = code_body.replace("\\", "\\\\")
    code = (
        "import sys\n"
        f"sys.path.insert(0, {json.dumps(_ROOT)})\n"
        "from pathlib import Path\n"
        f"from {_EVIDENCE_MODULE} import LocalEvidenceStore\n"
        f"{code_body}"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"


class TestEvidenceStoreIntegration:
    def test_evidence_store_init(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
store = LocalEvidenceStore("{tmp_path}")
assert store.root == Path("{tmp_path}")
assert store.evidence_dir.exists()
print("PASS: init")
''')

    def test_write_query_result(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
import json, pathlib
store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-test-001",
    parameters={{"start": "2026-05-01"}},
    rows=[{{"ip": "1.2.3.4"}}],
    row_count=1, truncated=False, summary={{"total": 1}},
)
assert ref.evidence_id == "q-test-001"
assert ref.query_id == "q-test-001"
assert ref.row_count == 1
evidence_file = pathlib.Path(ref.path)
assert evidence_file.exists()
with open(evidence_file, encoding="utf-8") as f:
    data = json.load(f)
assert data["query_id"] == "q-test-001"
assert data["row_count"] == 1
print("PASS: write_query_result")
''')

    def test_write_empty_rows(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
import json
store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-empty-001", parameters={{}}, rows=[], row_count=0,
    truncated=False, summary={{"total": 0}},
)
assert ref.row_count == 0
with open(ref.path, encoding="utf-8") as f:
    data = json.load(f)
assert data["rows"] == []
print("PASS")
''')

    def test_write_special_characters(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
import json
store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-special-001",
    parameters={{"query": "SELECT * FROM users WHERE name = '张三'"}},
    rows=[], row_count=0, truncated=False, summary={{}},
)
with open(ref.path, encoding="utf-8") as f:
    data = json.load(f)
assert "张三" in data["parameters"]["query"]
print("PASS")
''')

    def test_read_json_exists(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
store = LocalEvidenceStore("{tmp_path}")
store.write_query_result(
    query_id="q-read-001", parameters={{}}, rows=[{{"test": "data"}}],
    row_count=1, truncated=False, summary={{}},
)
data = store.read_json("{tmp_path}/evidence/q-read-001.json")
assert data["query_id"] == "q-read-001"
print("PASS")
''')

    def test_read_json_not_exists_raises(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
store = LocalEvidenceStore("{tmp_path}")
try:
    store.read_json("{tmp_path}/nonexistent.json")
    assert False, "should have raised FileNotFoundError"
except FileNotFoundError:
    pass
print("PASS")
''')

    def test_read_json_truncates_large_file(self, tmp_path: Path) -> None:
        large_file = tmp_path / "large.json"
        large_file.write_text("x" * 300_000, encoding="utf-8")
        _run_evidence_code(f'''
store = LocalEvidenceStore("{tmp_path}")
data = store.read_json("{large_file}", max_bytes=200_000)
assert data["truncated"] is True
assert data["size_bytes"] > 200_000
print("PASS")
''')

    def test_evidence_ref_has_content_hash(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-hash-001", parameters={{}}, rows=[], row_count=0,
    truncated=False, summary={{}},
)
assert ref.content_hash != ""
assert ref.sha256 != ""
print("PASS")
''')

    def test_evidence_ref_path_in_metadata(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-meta-001", parameters={{}}, rows=[], row_count=0,
    truncated=False, summary={{}},
)
assert "evidence_path" in ref.metadata
print("PASS")
''')

    def test_evidence_ref_created_at_timestamp(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-time-001", parameters={{}}, rows=[], row_count=0,
    truncated=False, summary={{}},
)
assert ref.created_at != ""
assert "Z" in ref.created_at or "+00:00" in ref.created_at
print("PASS")
''')

    def test_multiple_writes_same_query_id(self, tmp_path: Path) -> None:
        _run_evidence_code(f'''
import json
store = LocalEvidenceStore("{tmp_path}")
store.write_query_result(
    query_id="q-dup-001", parameters={{}}, rows=[{{"v": 1}}],
    row_count=1, truncated=False, summary={{}},
)
ref2 = store.write_query_result(
    query_id="q-dup-001", parameters={{}}, rows=[{{"v": 2}}],
    row_count=1, truncated=False, summary={{}},
)
with open(ref2.path, encoding="utf-8") as f:
    data = json.load(f)
assert data["rows"][0]["v"] == 2
print("PASS")
''')


class TestEvidenceModuleFunctions:
    def test_evidence_store_module_structure(self) -> None:
        import importlib.util

        spec = importlib.util.find_spec(_EVIDENCE_MODULE)
        assert spec is not None
        assert spec.loader is not None

    def test_evidence_ref_kind_field(self) -> None:
        pass

    def test_evidence_store_creates_nested_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "nested" / "path"
        code = (
            "import sys\n"
            f"sys.path.insert(0, {json.dumps(_ROOT)})\n"
            f"from {_EVIDENCE_MODULE} import LocalEvidenceStore\n"
            f"store = LocalEvidenceStore({json.dumps(str(nested))})\n"
            "assert store.evidence_dir.exists()\n"
            'print("PASS: nested_dirs")\n'
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0
        assert "PASS: nested_dirs" in result.stdout

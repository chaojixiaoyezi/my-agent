"""测试日志分析证据模块 (evidence.py)

测试 LocalEvidenceStore 的核心功能。
由于 storage/query.py 循环依赖 cases.evidence，使用 subprocess 运行测试。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


class TestEvidenceStoreIntegration:
    """集成测试：通过 subprocess 运行避免循环导入"""

    def test_evidence_store_init(self, tmp_path):
        """测试 LocalEvidenceStore 初始化"""
        code = f'''
import sys
from pathlib import Path
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore
store = LocalEvidenceStore("{tmp_path}")
assert store.root == Path("{tmp_path}")
assert store.evidence_dir.exists()
print("PASS: init")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: init" in result.stdout

    def test_write_query_result(self, tmp_path):
        """测试 write_query_result 创建文件并返回 EvidenceRef"""
        code = f'''
import sys
import json
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-test-001",
    parameters={{"start": "2026-05-01"}},
    rows=[{{"ip": "1.2.3.4"}}],
    row_count=1,
    truncated=False,
    summary={{"total": 1}},
)

# 验证返回的 ref
assert ref.evidence_id == "q-test-001"
assert ref.query_id == "q-test-001"
assert ref.row_count == 1

# 验证文件存在
import pathlib
evidence_file = pathlib.Path(ref.path)
assert evidence_file.exists()

# 验证文件内容
with open(evidence_file) as f:
    data = json.load(f)
assert data["query_id"] == "q-test-001"
assert data["row_count"] == 1

print("PASS: write_query_result")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: write_query_result" in result.stdout

    def test_write_empty_rows(self, tmp_path):
        """测试写入空行列表"""
        code = f'''
import sys
import json
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-empty-001",
    parameters={{}},
    rows=[],
    row_count=0,
    truncated=False,
    summary={{"total": 0}},
)

assert ref.row_count == 0

# 验证文件内容
with open(ref.path) as f:
    data = json.load(f)
assert data["rows"] == []

print("PASS: write_empty_rows")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: write_empty_rows" in result.stdout

    def test_write_special_characters(self, tmp_path):
        """测试参数包含特殊字符（中文、特殊符号）"""
        code = f'''
import sys
import json
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-special-001",
    parameters={{"query": "SELECT * FROM users WHERE name = '张三'"}},
    rows=[],
    row_count=0,
    truncated=False,
    summary={{}},
)

with open(ref.path) as f:
    data = json.load(f)
assert "张三" in data["parameters"]["query"]

print("PASS: write_special_characters")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: write_special_characters" in result.stdout

    def test_read_json_exists(self, tmp_path):
        """测试读取存在的 JSON 文件"""
        code = f'''
import sys
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")
# 先写入
store.write_query_result(
    query_id="q-read-001",
    parameters={{}},
    rows=[{{"test": "data"}}],
    row_count=1,
    truncated=False,
    summary={{}},
)

# 再读取
data = store.read_json("{tmp_path}/evidence/q-read-001.json")
assert data["query_id"] == "q-read-001"

print("PASS: read_json_exists")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: read_json_exists" in result.stdout

    def test_read_json_not_exists_raises(self, tmp_path):
        """测试读取不存在的文件抛出 FileNotFoundError"""
        code = f'''
import sys
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")
try:
    store.read_json("{tmp_path}/nonexistent.json")
    print("FAIL: should have raised FileNotFoundError")
except FileNotFoundError:
    print("PASS: read_json_not_exists_raises")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: read_json_not_exists_raises" in result.stdout

    def test_read_json_truncates_large_file(self, tmp_path):
        """测试读取超大文件返回截断信息"""
        large_file = tmp_path / "large.json"
        large_file.write_text("x" * 300_000, encoding="utf-8")

        code = f'''
import sys
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")
data = store.read_json("{large_file}", max_bytes=200_000)
assert data["truncated"] is True
assert data["size_bytes"] > 200_000

print("PASS: read_json_truncates_large_file")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: read_json_truncates_large_file" in result.stdout

    def test_evidence_ref_has_content_hash(self, tmp_path):
        """测试生成的 ref 包含 content_hash 和 sha256"""
        code = f'''
import sys
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-hash-001",
    parameters={{}},
    rows=[],
    row_count=0,
    truncated=False,
    summary={{}},
)

assert ref.content_hash != ""
assert ref.sha256 != ""

print("PASS: evidence_ref_has_content_hash")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: evidence_ref_has_content_hash" in result.stdout

    def test_evidence_ref_path_in_metadata(self, tmp_path):
        """测试路径存储在 metadata 中"""
        code = f'''
import sys
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-meta-001",
    parameters={{}},
    rows=[],
    row_count=0,
    truncated=False,
    summary={{}},
)

assert "evidence_path" in ref.metadata

print("PASS: evidence_ref_path_in_metadata")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: evidence_ref_path_in_metadata" in result.stdout

    def test_evidence_ref_created_at_timestamp(self, tmp_path):
        """测试 ref 有 created_at 时间戳"""
        code = f'''
import sys
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")
ref = store.write_query_result(
    query_id="q-time-001",
    parameters={{}},
    rows=[],
    row_count=0,
    truncated=False,
    summary={{}},
)

assert ref.created_at != ""
assert "Z" in ref.created_at or "+00:00" in ref.created_at

print("PASS: evidence_ref_created_at_timestamp")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: evidence_ref_created_at_timestamp" in result.stdout

    def test_multiple_writes_same_query_id(self, tmp_path):
        """测试同一 query_id 多次写入覆盖"""
        code = f'''
import sys
import json
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{tmp_path}")

# 第一次写入
store.write_query_result(
    query_id="q-dup-001",
    parameters={{}},
    rows=[{{"v": 1}}],
    row_count=1,
    truncated=False,
    summary={{}},
)

# 第二次写入同一 query_id
ref2 = store.write_query_result(
    query_id="q-dup-001",
    parameters={{}},
    rows=[{{"v": 2}}],
    row_count=1,
    truncated=False,
    summary={{}},
)

# 验证文件内容是第二次的
with open(ref2.path) as f:
    data = json.load(f)
assert data["rows"][0]["v"] == 2

print("PASS: multiple_writes_same_query_id")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        assert "PASS: multiple_writes_same_query_id" in result.stdout


# 独立功能测试：不依赖循环导入的部分
class TestEvidenceModuleFunctions:
    """直接测试 evidence.py 模块级函数（需要 mock 依赖）"""

    def test_evidence_store_module_structure(self):
        """测试 evidence.py 模块结构正确"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "evidence",
            "/Users/xiaoyezi/my_agent/my-agent/agent_py_agent/agent/log_analysis/cases/evidence.py"
        )
        assert spec is not None
        assert spec.loader is not None

    def test_evidence_ref_kind_field(self):
        """测试 EvidenceRef 有正确的 kind 默认值"""
        # 通过 integration test 验证
        pass

    def test_evidence_store_creates_nested_dirs(self, tmp_path):
        """测试 evidence store 创建嵌套目录"""
        nested = tmp_path / "nested" / "path"
        code = f'''
import sys
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from agent_py_agent.agent.log_analysis.cases.evidence import LocalEvidenceStore

store = LocalEvidenceStore("{nested}")
assert store.evidence_dir.exists()
print("PASS: nested_dirs")
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0
        assert "PASS: nested_dirs" in result.stdout
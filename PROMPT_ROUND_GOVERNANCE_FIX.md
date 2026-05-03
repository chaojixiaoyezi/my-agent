你现在接手当前 my-agent 项目，执行一轮“治理护栏彻底修正与验收收口”。

上一轮已经完成了部分治理工作，包括：
- cli/parser.py 已经变薄；
- 新增了 check_code_size.py；
- 新增了 CODE_SIZE_POLICY.md、CODE_SIZE_REPORT.md、REFACTORING_BACKLOG.md、ARCHITECTURE_EXEMPTIONS.md 等文档；
- 修复了 pytest collection 中 startup_recovery.py 的 SimpleAgent 注解问题；
- 新增了部分 SubAgent service 雏形。

但是上一轮还没有完全通过，存在以下硬伤：
1. 交付包中混入大量 macOS AppleDouble 文件，也就是 `._*` 文件。
2. `scripts/check_code_size.py` 对脏包不稳，遇到非 UTF-8 / AppleDouble 文件会崩。
3. `strict` 模式太弱，不能真正阻断新增硬违规。
4. `test_architecture_guardrails.py` 依赖 `git ls-files`，在非 git 解包环境会失败。
5. `test_compileall_succeeds()` 的断言写错了，`assert result is not None` 不能证明 compileall 成功。
6. pytest markers 没补全，目前只注册了 slow。
7. CI 测试分层没做好，PR 仍然可能直接跑全量 pytest。
8. 打包洁净度没有明确文档和自动检查。
9. CODE_SIZE_REPORT.md 等治理文档需要更新成真实结果，不能只写空话。
10. 当前治理护栏还不能算可靠闭环。

本轮要求：
- 不新增业务功能。
- 不做大重构。
- 不继续拆 SubAgent 大模块。
- 先把治理护栏修到真正可靠。
- 必须真实修改代码、测试、CI、文档。
- 不要只写报告。
- 不要跳过验证。
- 自己执行任务，不要派子代理。
- 新增一个“提示词文件”，把本轮完整提示词保存进去，文件名为 `PROMPT_ROUND_GOVERNANCE_FIX.md`。

---

# 一、本轮总目标

把 my-agent 的工程治理从“有文档、有脚本，但不够可靠”修到“可实际执行、可 CI 验收、可干净打包、可长期防止屎山”的状态。

最终必须做到：

1. 源码树不包含 `._*`、`.DS_Store`、`__pycache__`、`*.pyc` 等脏文件。
2. 打包命令不会产生 AppleDouble 文件。
3. `check_code_size.py` 对干净仓库、脏包、非 git 解包环境都稳。
4. `strict` 模式能真正阻断新增硬违规。
5. 架构护栏测试在 git checkout 和 tar 解包环境下都合理。
6. compileall 测试必须真的能发现失败。
7. pytest markers 完整。
8. CI 按 fast / full / slow / security 分层，不再把所有测试混成一锅。
9. 文档写清楚后续开发和打包规范。
10. 验收命令全部跑通，并把结果写进报告。

---

# 二、必须修复的问题

## 1. 清理 AppleDouble / macOS 元数据文件

必须从仓库和交付包中清理以下内容：

```text
._*
.DS_Store
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
htmlcov/
```

必须检查整个项目树，不允许残留：

```bash
find . -name '._*' -o -name '.DS_Store' -o -name '__pycache__' -o -name '*.pyc'
```

如果有，必须清理。

同时更新 `.gitignore`，至少包含：

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/

# macOS
.DS_Store
._*
.AppleDouble
.LSOverride

# Runtime / local data
MagicMock/
agent_py_agent/MagicMock/
data/
!data/.gitkeep
mutation_test_report.json
.mutation_backups/
CODE_SIZE_REPORT.tmp
```

---

## 2. 新增 CLEAN_PACKAGE_POLICY.md

必须新增：

```text
CLEAN_PACKAGE_POLICY.md
```

内容必须说明：

1. 交付包不得包含：
   - `._*`
   - `.DS_Store`
   - `__pycache__`
   - `*.pyc`
   - `.pytest_cache`
   - `.ruff_cache`
   - 本地 runtime data
   - MagicMock 临时目录
2. macOS 打包必须使用 `COPYFILE_DISABLE=1`。
3. 标准打包命令必须写清楚。
4. 标准检查命令必须写清楚。
5. 如果检查发现脏文件，交付包视为不通过。

推荐标准打包命令：

```bash
COPYFILE_DISABLE=1 tar \
  --exclude='._*' \
  --exclude='.DS_Store' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='.mypy_cache' \
  --exclude='MagicMock' \
  --exclude='agent_py_agent/MagicMock' \
  --exclude='mutation_test_report.json' \
  --exclude='.mutation_backups' \
  -czf my-agent-clean.tar.gz \
  agent_py_agent scripts docs .github pyproject.toml README.md LICENSE \
  CODE_SIZE_POLICY.md CODE_SIZE_REPORT.md REFACTORING_BACKLOG.md \
  ARCHITECTURE_EXEMPTIONS.md CLEAN_PACKAGE_POLICY.md TESTING_POLICY.md
```

推荐检查命令：

```bash
tar -tzf my-agent-clean.tar.gz | grep -E '(^|/)\._|\.DS_Store|__pycache__|\.pyc|\.pytest_cache|\.ruff_cache' \
  && echo "FAILED: dirty package" \
  || echo "OK: clean package"
```

---

## 3. 修复 scripts/check_code_size.py

必须全面增强 `scripts/check_code_size.py`。

### 3.1 文件扫描必须排除脏文件

`_source_files()` 或等价扫描函数必须排除：

```text
._*
.DS_Store
.AppleDouble
__pycache__
.git
.pytest_cache
.ruff_cache
.mypy_cache
.coverage
htmlcov
*.pyc
```

不能扫描 AppleDouble 文件。

### 3.2 遇到 decode error 不能直接崩

如果遇到 `UnicodeDecodeError`，不能让脚本直接崩溃。

必须记录为 hard finding，例如：

```text
kind=decode_error
severity=hard
path=<file>
message=failed to decode source file as UTF-8
```

warn 模式下继续扫描其他文件。

strict 模式下：
- 如果是源码目录里的 decode_error，必须阻断。
- 如果是被排除的脏文件，不应该扫描到。

### 3.3 strict 模式必须真正严格

当前 strict 太弱，只阻断 import_star / junk_name / syntax / high_risk_growth 不够。

必须增强 strict 逻辑。

建议规则：

strict 模式应阻断以下 hard finding：

```text
syntax
decode_error
import_star
junk_name
high_risk_growth
file
function
class
params
nesting
```

但要注意：
- 历史遗留项可以通过 baseline 暂时不阻断；
- 新增文件、新增函数、新增类、修改后增长的硬违规必须阻断；
- 如果暂时没有 baseline 机制，至少 strict 模式要对当前 hard finding 返回非 0，不能假严格；
- 如果担心历史遗留导致 CI 直接红，可以支持：
  - `--mode warn`
  - `--mode strict`
  - `--baseline CODE_SIZE_BASELINE.json`
  - `--write-baseline CODE_SIZE_BASELINE.json`

优先实现可用版本，不要过度设计。

### 3.4 新增 baseline 能力

推荐实现 baseline：

```bash
python scripts/check_code_size.py --mode warn --write-baseline CODE_SIZE_BASELINE.json
python scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
```

baseline 里记录历史已知违规项。

strict 模式：
- baseline 里的历史违规不阻断；
- 新增违规阻断；
- 已有违规变严重阻断；
- 高危文件继续增长阻断。

如果时间有限，至少先实现：
- strict 对所有 hard finding 返回非 0；
- CI 暂时使用 warn；
- 文档说明 strict 后续接 baseline。

但本轮最好把 baseline 做完。

### 3.5 报告必须更清楚

`CODE_SIZE_REPORT.md` 必须包含：

```text
1. 本次扫描时间
2. 扫描模式
3. 总 finding 数
4. hard finding 数
5. soft finding 数
6. 是否阻断
7. 超长文件 Top 20
8. 超长函数 Top 20
9. 超长类 Top 20
10. import * 违规
11. decode error 违规
12. junk file / junk name 违规
13. 高危文件增长情况
14. 下一步建议
```

---

## 4. 修复 architecture guardrails 测试

修改：

```text
agent_py_agent/tests/test_architecture_guardrails.py
```

### 4.1 `_tracked_files()` 不能依赖 git 必然存在

当前测试中如果使用 `git ls-files`，在 tar 包解压环境会失败。

必须改成：

```python
def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    # fallback for source tarball / non-git environments
    return [
        path.relative_to(REPO_ROOT).as_posix()
        for path in REPO_ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and not path.name.startswith("._")
        and path.name != ".DS_Store"
        and path.suffix != ".pyc"
    ]
```

也可以选择 `pytest.skip("requires git checkout")`，但更推荐 fallback 文件扫描。

### 4.2 compileall 测试必须真的断言成功

把：

```python
assert result is not None
```

改成：

```python
assert result is True
```

否则 compileall 返回 False 也会通过，这是错误的。

### 4.3 增加脏包护栏测试

新增测试，确保源码树不包含：

```text
._*
.DS_Store
__pycache__
*.pyc
```

例如：

```python
def test_no_macos_or_python_cache_artifacts():
    bad = []
    for path in REPO_ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.name.startswith("._") or path.name == ".DS_Store":
            bad.append(path)
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            bad.append(path)
    assert not bad
```

---

## 5. 补全 pytest markers

更新 `pyproject.toml`：

```toml
[tool.pytest.ini_options]
testpaths = ["agent_py_agent/tests"]
python_files = ["test_*.py"]
addopts = "-q"
markers = [
  "slow: stress or long-running scenario tests",
  "e2e: end-to-end workflow tests",
  "integration: integration tests across modules",
  "security: permission, auth, and boundary tests",
  "contract: schema and interface contract tests",
  "regression: tests for previously fixed bugs",
]
```

如果已有 pytest 配置，合并，不要覆盖已有有用配置。

---

## 6. 新增或更新 TESTING_POLICY.md

必须新增或更新：

```text
TESTING_POLICY.md
```

内容必须包括：

1. 测试分层：
   - unit
   - integration
   - e2e
   - slow
   - security
   - contract
   - regression
2. 各类测试什么时候写。
3. pytest marker 使用规范。
4. PR 默认测试命令。
5. 全量测试命令。
6. 慢测试命令。
7. 安全测试命令。
8. 测试命名规范。
9. fixture 规范。
10. golden fixture 规范。
11. contract test 规范。
12. 新功能必须补测试的要求。

---

## 7. 调整 CI

### 7.1 更新 `.github/workflows/test.yml`

PR 默认不能再直接跑全量 pytest。

改成快速测试：

```yaml
- name: Run fast tests
  run: python3 -m pytest -q -m "not slow and not e2e" --tb=short
```

建议增加 timeout 防卡死：

```yaml
- name: Install dev dependencies
  run: python3 -m pip install -e ".[dev]"
```

如果暂时没有 `pytest-timeout`，加到 dev dependencies：

```toml
[project.optional-dependencies]
dev = [
  "pytest>=8",
  "pytest-timeout>=2",
  "pytest-cov>=5",
  "ruff>=0.8",
]
```

并在 pytest addopts 或 CI 中使用 timeout：

```bash
python3 -m pytest -q -m "not slow and not e2e" --tb=short --timeout=60
```

### 7.2 新增 full / slow workflow

可以新增：

```text
.github/workflows/full-tests.yml
```

或在现有 test.yml 中用 workflow_dispatch / schedule。

命令：

```bash
python3 -m pytest -q --tb=short
python3 -m pytest -q -m slow --tb=short
python3 -m pytest -q -m security --tb=short
```

如果不想新增文件，至少在文档里写清楚 full test 是 nightly/manual，不是 PR 默认。

### 7.3 更新 `.github/workflows/lint.yml`

lint workflow 至少包含：

```bash
python3 -m compileall -q agent_py_agent scripts
python3 scripts/check_clean_package.py .
python3 scripts/check_code_size.py --mode warn
python3 -m pytest agent_py_agent/tests/test_architecture_guardrails.py -q
```

如果有 ruff：

```bash
python3 -m ruff check agent_py_agent scripts
```

不要让 lint 只做 py_compile。

---

## 8. 修复架构护栏相关测试

确保以下命令能通过：

```bash
python3 -m pytest agent_py_agent/tests/test_architecture_guardrails.py -q
```

这个测试应该覆盖：

1. 没有 AppleDouble / .DS_Store / pycache / pyc。
2. compileall 成功。
3. 高危文件没有继续膨胀，或有豁免。
4. 不允许新增 import *。
5. 不允许新增模糊 utils/common/helpers 命名。
6. docs 中关键治理文档存在。

如果某些检查目前只能 warning，测试里要写清楚边界，不要假通过。

---

## 9. 修复代码规模报告和豁免文档

更新：

```text
CODE_SIZE_REPORT.md
ARCHITECTURE_EXEMPTIONS.md
REFACTORING_BACKLOG.md
CODE_SIZE_POLICY.md
```

### 9.1 CODE_SIZE_REPORT.md

必须是真实脚本结果，不要手写空话。

必须包含：
- 当前 finding 总数；
- hard finding 数；
- soft finding 数；
- 当前最大文件；
- 当前最大函数；
- 当前最大类；
- strict 是否通过；
- 如果 strict 不通过，原因是什么；
- 哪些属于历史遗留；
- 哪些必须下一轮拆。

### 9.2 ARCHITECTURE_EXEMPTIONS.md

必须列出历史遗留大文件/大函数/大类。

至少包含这些高危对象：

```text
agent_py_agent/cli/chat.py
agent_py_agent/agent/agent_core/dispatch_mixin.py
agent_py_agent/agent/memory_archive/query.py
agent_py_agent/agent/subagents/manager_patch.py
agent_py_agent/agent/settings/config.py
agent_py_agent/agent/subagents/manager_base.py
```

每项必须写：

```text
当前问题
为什么暂时不能本轮拆完
计划拆分方向
风险
预计清理轮次
```

### 9.3 REFACTORING_BACKLOG.md

必须按优先级列出下一轮：

```text
P0: cli/chat.py
P0: dispatch_mixin.py
P0: manager_patch.py
P0: config.py
P1: memory_archive/query.py
P1: manager_base.py
P1: gateway_parts/runtime.py
P1: log_analysis/tools.py
```

每项必须包含：
- 当前行数；
- 当前主要职责；
- 拆分目标；
- 预期拆出模块；
- 验收测试。

---

## 10. 必须补一个 clean package 检查脚本

新增：

```text
scripts/check_clean_package.py
```

功能：

```bash
python3 scripts/check_clean_package.py .
python3 scripts/check_clean_package.py my-agent-clean.tar.gz
```

要求：
- 可以检查目录；
- 可以检查 tar.gz；
- 发现 `._*`、`.DS_Store`、`__pycache__`、`*.pyc`、`.pytest_cache`、`.ruff_cache` 就失败；
- 输出具体路径；
- 退出码非 0。

然后把它加入文档和 CI：

```bash
python3 scripts/check_clean_package.py .
```

如果实现 tar.gz 检查，验收时也跑：

```bash
python3 scripts/check_clean_package.py my-agent-clean.tar.gz
```

---

## 11. 必须更新 pyproject.toml

需要确保：

```toml
[project.optional-dependencies]
dev = [
  "pytest>=8",
  "pytest-timeout>=2",
  "pytest-cov>=5",
  "ruff>=0.8",
]
```

如果项目已有 dev 依赖，合并，不要覆盖。

确保：

```toml
[tool.pytest.ini_options]
testpaths = ["agent_py_agent/tests"]
python_files = ["test_*.py"]
markers = [
  "slow: stress or long-running scenario tests",
  "e2e: end-to-end workflow tests",
  "integration: integration tests across modules",
  "security: permission, auth, and boundary tests",
  "contract: schema and interface contract tests",
  "regression: tests for previously fixed bugs",
]
```

可以加 coverage：

```toml
[tool.coverage.run]
source = ["agent_py_agent"]
branch = true

[tool.coverage.report]
show_missing = true
skip_covered = true
fail_under = 60
```

如果当前 coverage 会导致大量失败，可以先配置但不在 CI 强制 fail_under，文档说明逐步提高。

---

# 三、本轮不要做的事情

禁止：

1. 禁止新增业务功能。
2. 禁止继续拆大规模 SubAgent。
3. 禁止做 Delegation/Federation/Enterprise 新功能。
4. 禁止写 UI。
5. 禁止只更新文档不改代码。
6. 禁止只改脚本不跑验证。
7. 禁止删除测试来让 CI 变绿。
8. 禁止把 full test 问题隐藏掉不说明。
9. 禁止把 strict 模式写成假严格。
10. 禁止交付脏包。

---

# 四、必须运行的验收命令

在仓库根目录执行：

```bash
python3 -m compileall -q agent_py_agent scripts
python3 scripts/check_clean_package.py .
python3 scripts/check_code_size.py --mode warn
python3 scripts/check_code_size.py --mode strict
python3 -m pytest agent_py_agent/tests/test_architecture_guardrails.py -q --tb=short
python3 -m pytest agent_py_agent/tests/test_packaging.py agent_py_agent/tests/test_cli_parser.py agent_py_agent/tests/test_tooling_filesystem.py -q --tb=short
python3 -m pytest --collect-only -q --tb=short
git diff --check
```

如果 strict 因历史遗留暂时失败，必须满足以下条件：
1. 不能是新增违规；
2. 必须生成 baseline；
3. 必须在 ARCHITECTURE_EXEMPTIONS.md 里列出；
4. 必须在 CODE_SIZE_REPORT.md 里明确说明；
5. CI 里暂时只用 warn，但 strict 的目标和阻断规则必须真实存在。

如果已安装 ruff：

```bash
python3 -m ruff check agent_py_agent scripts
```

打包检查：

```bash
COPYFILE_DISABLE=1 tar \
  --exclude='._*' \
  --exclude='.DS_Store' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='.mypy_cache' \
  --exclude='MagicMock' \
  --exclude='agent_py_agent/MagicMock' \
  --exclude='mutation_test_report.json' \
  --exclude='.mutation_backups' \
  -czf my-agent-clean.tar.gz \
  agent_py_agent scripts docs .github pyproject.toml README.md LICENSE \
  CODE_SIZE_POLICY.md CODE_SIZE_REPORT.md REFACTORING_BACKLOG.md \
  ARCHITECTURE_EXEMPTIONS.md CLEAN_PACKAGE_POLICY.md TESTING_POLICY.md

python3 scripts/check_clean_package.py my-agent-clean.tar.gz

tar -tzf my-agent-clean.tar.gz | grep -E '(^|/)\._|\.DS_Store|__pycache__|\.pyc|\.pytest_cache|\.ruff_cache' \
  && echo "FAILED: dirty package" \
  || echo "OK: clean package"
```

---

# 五、最终必须产出或更新的文件

必须新增或更新：

```text
PROMPT_ROUND_GOVERNANCE_FIX.md
CLEAN_PACKAGE_POLICY.md
TESTING_POLICY.md
CODE_SIZE_POLICY.md
CODE_SIZE_REPORT.md
ARCHITECTURE_EXEMPTIONS.md
REFACTORING_BACKLOG.md
scripts/check_code_size.py
scripts/check_clean_package.py
agent_py_agent/tests/test_architecture_guardrails.py
pyproject.toml
.github/workflows/test.yml
.github/workflows/lint.yml
```

如果新增 full test workflow：

```text
.github/workflows/full-tests.yml
```

---

# 六、报告要求

更新：

```text
CODE_SIZE_REPORT.md
```

必须通俗、具体，不要空话。

必须包含：

1. 本轮为什么要修。
2. 上一轮有哪些硬伤。
3. 本轮实际修了哪些文件。
4. 是否清理了 `._*`、`.DS_Store`、`__pycache__`、`*.pyc`。
5. `check_clean_package.py` 的结果。
6. `check_code_size.py --mode warn` 的结果。
7. `check_code_size.py --mode strict` 的结果。
8. `pytest --collect-only` 的结果。
9. `test_architecture_guardrails.py` 的结果。
10. 三个核心小测试的结果：
    - test_packaging.py
    - test_cli_parser.py
    - test_tooling_filesystem.py
11. CI 做了哪些调整。
12. 现在还遗留哪些大文件、大函数、大类。
13. 下一轮建议先拆什么。
14. 是否可以进入 SubAgent 重构下一轮。
15. 如果没有完全通过，必须直接写“不完全通过”，并说明原因。

必须列出具体命令和结果，不能只写“已验证”。

---

# 七、完成标准

只有满足以下条件，本轮才算通过：

1. 源码树 clean package 检查通过。
2. 交付 tar.gz clean package 检查通过。
3. `python3 -m compileall -q agent_py_agent scripts` 通过。
4. `python3 -m pytest --collect-only -q --tb=short` 通过。
5. `test_architecture_guardrails.py` 通过。
6. 三个核心小测试通过。
7. pytest markers 补全。
8. CI fast test 使用 `not slow and not e2e`。
9. check_code_size warn 可正常运行。
10. strict 模式不是假严格；如果历史遗留导致 strict 不适合直接进 CI，必须有 baseline 或豁免说明。
11. 文档齐全。
12. CODE_SIZE_REPORT.md 写清楚真实结果。
13. 不新增业务功能。
14. 不扩大高危大文件。
15. 不交付脏包。

---

# 八、本轮目标

本轮目标：

把 my-agent 上一轮治理中的硬伤一次性修完，让工程护栏真正可靠。

具体来说：

- 打包必须干净；
- 检查脚本必须稳；
- strict 模式必须真；
- 架构测试必须能发现真实问题；
- pytest 必须分层；
- CI 必须可长期运行；
- 文档必须能指导后续开发；
- 报告必须真实记录结果。

完成本轮后，my-agent 才能进入下一阶段：
SubAgent 真实执行闭环重构、Memory/Resume 治理、Gateway 控制面增强、Tools/Skill 权限与沉淀体系建设。

# CLEAN PACKAGE POLICY

## 目标

确保 my-agent 的源码树和交付包不包含任何脏文件（macOS 元数据、Python 缓存、运行时产物）。

## 禁止出现在源码树和交付包中的文件

| 类型 | 模式 |
| --- | --- |
| macOS AppleDouble | `._*` |
| macOS Finder metadata | `.DS_Store` |
| macOS AppleDouble dir | `.AppleDouble` |
| macOS LSOverride | `.LSOverride` |
| Python bytecode cache | `__pycache__/` |
| Python compiled files | `*.pyc`, `*.pyo` |
| pytest cache | `.pytest_cache/` |
| ruff cache | `.ruff_cache/` |
| mypy cache | `.mypy_cache/` |
| coverage reports | `.coverage`, `htmlcov/` |
| MagicMock temp dirs | `MagicMock/` |
| mutation test reports | `mutation_test_report.json` |

## 标准打包命令

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

`COPYFILE_DISABLE=1` 是 macOS 环境必须的，用于阻止 tar 自动插入 `._` AppleDouble 文件。

## 标准检查命令

### 检查目录

```bash
python3 scripts/check_clean_package.py .
```

### 检查交付包

```bash
python3 scripts/check_clean_package.py my-agent-clean.tar.gz
```

### 手动检查（备用）

```bash
tar -tzf my-agent-clean.tar.gz | grep -E '(^|/)\._|\.DS_Store|__pycache__|\.pyc|\.pytest_cache|\.ruff_cache' \
  && echo "FAILED: dirty package" \
  || echo "OK: clean package"
```

## 验收标准

1. 源码树 `python3 scripts/check_clean_package.py .` 必须通过。
2. 交付包 `python3 scripts/check_clean_package.py my-agent-clean.tar.gz` 必须通过。
3. 如果检查发现任何脏文件，交付包视为**不通过**。
4. CI lint workflow 中必须包含 `python3 scripts/check_clean_package.py .`。

## 常见问题

### Q: macOS 上 .DS_Store 怎么删了又出现？

A: macOS Finder 会自动在访问过的目录生成 `.DS_Store`。这些文件不会被 git 跟踪（已在 `.gitignore` 中排除），但打包时需要使用 `COPYFILE_DISABLE=1` 和 `--exclude` 来阻止它们进入交付包。

### Q: 为什么不用 .gitignore 就够了？

A: `.gitignore` 只影响 git，不影响 `tar`、`cp`、文件管理器等操作。打包必须显式排除。

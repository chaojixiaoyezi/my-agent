# CODE SIZE POLICY

LLM: use `python scripts/check_code_size.py --mode warn` to refresh `CODE_SIZE_REPORT.md`.

给人看的解释：
现在不再把“文件行数大”当成硬门。文件可以为了主链路清楚、调用更直接而变大；报告只用来观察趋势。函数、类、参数、嵌套、星号导入、语法错误和新增垃圾文件名仍然是局部可读性硬门。

---

## 1. File Size

| Item | Current Rule |
|------|--------------|
| File length | Advisory only. Above soft limit appears in `CODE_SIZE_REPORT.md`, but does not block. |
| Preferred reason to split | A file owns unrelated responsibilities, not because it crossed a line-count number. |
| Preferred reason to merge | A layer only forwards calls, hides the main chain, or keeps historical path names alive. |

---

## 2. Local Complexity

| Metric | Soft | Hard |
|--------|-----:|-----:|
| Function / method length | 60 | 100 |
| Class length | 250 | 350 |
| Mixin length | 200 | 250 |
| Parameter count | 6 | 8 |
| Nesting depth | 3 | 4 |

Hard local-complexity findings block in `--mode strict`. Whole-file length findings never block.

---

## 3. Always Forbidden

- `import *`
- New vague filenames such as `utils.py`, `helpers.py`, `common.py`, `old.py`, `tmp.py`
- Reintroducing deleted historical-path modules or empty forwarding layers
- Adding a bypass when the current main chain should be fixed directly

---

## 4. Current Command

```bash
python scripts/check_code_size.py --mode warn
```

Latest generated report: `CODE_SIZE_REPORT.md`.

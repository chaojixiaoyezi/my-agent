# LLM: 只读结构删除门；383项均保留责任映射，未知语义不能记成通过，不触及用户数据或真实服务。
# 模块用途: 校验文件、AST、生产引用、规范CLI及基线真实守护入口，输出可分席位复核的JSON。
from __future__ import annotations

import argparse
import ast
import collections
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("optimization_removal_manifest.json")
DELETED_DOCTORS = ("agent_py_agent/cli/memory_doctor.py", "agent_py_agent/cli/memory_commands/memory_doctor_cmd.py")


# LLM: 语法错误和越界符号链接是检查失败，不当成目标已删除；文件内容只从指定repo读取。
# 函数用途: 严格读取一个检出内的Python语法树。
def read_tree(repo, relative):
    path = (repo / relative).resolve()
    if not path.is_relative_to(repo.resolve()):
        raise ValueError("检查路径越出仓库")
    return ast.parse(path.read_text(encoding="utf-8"), filename=relative)


# LLM: 定义、声明字段和常量都从AST取，不用注释字符串猜删除结果。
# 函数用途: 找到仍存在的指定Python符号定义或声明。
def defined_symbols(tree):
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result.add(node.target.id)
        elif isinstance(node, ast.Assign):
            result.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return result


# LLM: 只扫生产Python包与脚本；测试、版本化decoder和本检查器不是旧业务消费者。
# 函数用途: 检查被删除模块/符号是否仍被静态导入、调用或显式动态加载。
def forbidden_references(repo, *, symbols=(), modules=(), allow_paths=()):
    hits = []
    for base in (repo / "agent_py_agent", repo / "scripts"):
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(repo).as_posix()
            if "/tests/" in relative or relative in allow_paths:
                continue
            tree = read_tree(repo, relative)
            for node in ast.walk(tree):
                bad = ""
                if isinstance(node, ast.ImportFrom):
                    prefix = node.module or ""
                    names = [prefix, *(prefix + "." + a.name for a in node.names)]
                    if any(m == name or name.endswith("." + m.split(".")[-1]) for m in modules for name in names):
                        bad = "import"
                    if any(a.name in symbols for a in node.names):
                        bad = "symbol_import"
                elif isinstance(node, ast.Import) and any(a.name in modules for a in node.names):
                    bad = "import"
                elif isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                    if name in symbols:
                        bad = "call"
                    if name in {"import_module", "__import__"} and any(isinstance(a, ast.Constant) and a.value in modules for a in node.args):
                        bad = "dynamic_import"
                    if name == "getattr" and any(isinstance(a, ast.Constant) and a.value in symbols for a in node.args):
                        bad = "dynamic_symbol"
                if bad:
                    hits.append({"path": relative, "line": node.lineno, "kind": bad})
    return hits


# LLM: 使用目标repo的独立解释器进程装配真实parser，不从本进程模块缓存借用其他检出。
# 函数用途: 采集当前真实注册路径、处理器及冻结参数合同，不执行handler。
def cli_snapshot(repo):
    code = r'''
import argparse,json
from pathlib import Path
import agent_py_agent
if Path(agent_py_agent.__file__).resolve().parent != Path.cwd().resolve() / 'agent_py_agent':
 raise RuntimeError('目标解释器导入了其他检出')
from agent_py_agent.cli.parser import build_parser
from agent_py_agent.agent.command_catalog import COMMAND_CATALOG
root=Path.cwd().resolve();paths={}
def norm(value):
 if callable(value):return getattr(value,'__module__','')+'.'+getattr(value,'__qualname__',repr(value))
 if isinstance(value,Path):return str(value).replace(str(root),'<REPO>')
 if isinstance(value,(str,int,float,bool)) or value is None:return value.replace(str(root),'<REPO>') if isinstance(value,str) else value
 if isinstance(value,(list,tuple)):return [norm(x) for x in value]
 if isinstance(value,dict):return {k:norm(v) for k,v in value.items()}
 return repr(value)
def walk(p,prefix=()):
 for a in p._actions:
  if isinstance(a,argparse._SubParsersAction):
   for name,child in a.choices.items():paths[' '.join((*prefix,name))]=child;walk(child,(*prefix,name))
p=build_parser();walk(p)
roots=next(a for a in p._actions if isinstance(a,argparse._SubParsersAction)).choices
frozen={}
for name in ['gateway supervisor','gateway run','adapter start','gateway maintenance-lock','gateway maintenance-unlock']:
 if name not in paths:continue
 q=paths[name];frozen[name]={'handler':norm(q._defaults.get('func')),'arguments':[{k:norm(getattr(a,k)) for k in ['option_strings','dest','default','nargs','type','choices','required']} for a in q._actions if not isinstance(a,argparse._SubParsersAction)]}
print(json.dumps({'roots':sorted(roots),'paths':sorted(paths),'frozen_cli':frozen,'aliases':{s.name:list(s.aliases) for s in COMMAND_CATALOG if s.aliases}}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=repo, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("目标CLI装配失败: " + result.stderr[-2000:])
    return json.loads(result.stdout)


# LLM: 保留项检查原测试方法存在且有断言，不能以删除整份测试让删除门假通过。
# 函数用途: 校验MCP标准list_tools与管理员logout两个明确保留合同。
def retained_check(repo, row):
    for loc in row["locations"]:
        tree = read_tree(repo, loc["path"])
        name = loc.get("retained_test")
        tests = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (not name or n.name == name)]
        if not tests or not any(isinstance(n, ast.Assert) for test in tests for n in ast.walk(test)):
            return False
    return True


# LLM: 记录行状态与实际证据，不以只读清单中的implementation_status字段宣称删除完成。
# 函数用途: 构造一个结构核验结果。
def result_row(row, status, evidence):
    return {"id": row["id"], "worker": row["implementation_worker"], "status": status,
            "verification_level": "structural", "evidence": evidence}


# LLM: 源清单的自然语言不能自动变成通过；已编码结构合同给出证据，其余明确待领域复核。
# 函数用途: 按真实删除类别核对一条清单。
def check_row(repo, row, manifest, snapshot):
    action = row["remove_code"]
    if row["retained_contract_correction"]:
        return result_row(row, "retained_contract_correction" if retained_check(repo, row) else "fail", "原测试方法及断言保留")
    if action == "delete_file_after_service_and_projection_extraction":
        present = [loc["path"] for loc in row["locations"] if (repo / loc["path"]).exists()]
        return result_row(row, "fail" if present else "pass", {"remaining_files": present})
    if action == "delete_old_cli_registration_not_business_handler":
        evidence = []
        for loc in row["locations"]:
            old = loc.get("cli_path")
            if not old or old not in manifest["cli_mapping"]:
                return result_row(row, "pending_semantic_check", "缺少可核对的规范CLI映射")
            target = manifest["cli_mapping"][old]
            evidence.append({"old": old, "canonical": target})
            if target not in snapshot["paths"] or (old != target and old in snapshot["paths"] and not target.startswith(old + " ")):
                return result_row(row, "fail", evidence)
        return result_row(row, "pass", evidence)
    if action in {"delete_ambiguous_alias_member", "move_aliases_out_of_canonical_catalog"}:
        return result_row(row, "fail" if snapshot["aliases"] else "pass", {"remaining_aliases": snapshot["aliases"]})
    if action == "delete_ambiguous_exit_word":
        tree = read_tree(repo, "agent_py_agent/cli/chat_parts/slash_commands.py")
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "is_exit_command"]
        bad = not funcs or any(isinstance(n, ast.Constant) and n.value in {"logout", "/logout"} for f in funcs for n in ast.walk(f))
        return result_row(row, "fail" if bad else "pass", "退出判据不再包含歧义词")
    if action == "delete_duplicate_handler_and_aggregation_after_merge":
        symbols = defined_symbols(read_tree(repo, "agent_py_agent/cli/memory_admin_commands.py"))
        canonical = defined_symbols(read_tree(repo, "agent_py_agent/agent/user_space/home_doctor.py"))
        ok = "_doctor_payload" not in symbols and "DoctorService" in canonical
        return result_row(row, "pass" if ok else "fail", "旧聚合函数消失，规范DoctorService存在")
    if action == "delete_legacy_reexports":
        tree = read_tree(repo, "agent_py_agent/cli/memory_commands/__init__.py")
        names = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        bad = names & {"cmd_memory_doctor", "_memory_config_payload", "_build_routing_doctor", "_build_archive_doctor"}
        return result_row(row, "fail" if bad else "pass", {"remaining_exports": sorted(bad)})
    if row["id"] == "DEL-BRANCH-08":
        tree = read_tree(repo, "agent_py_agent/cli/config_cmd.py")
        old = {"load_simple_yaml", "set_simple_yaml_value"}
        bad = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in old or isinstance(n, ast.alias) and n.name in old]
        return result_row(row, "fail" if bad else "pass", {"raw_yaml_references": bad})
    if action == "replace_obsolete_test_expectation":
        for loc in row["locations"]:
            tree = read_tree(repo, loc["path"])
            if not any(isinstance(n, ast.Assert) for n in ast.walk(tree)):
                return result_row(row, "fail", "原合同测试或其断言缺失")
        return result_row(row, "requires_behavioral_test", "测试保留；语义由指定CLI回归验收，结构门不代替pytest")
    if action == "remove_obsolete_generated_input_or_callsite":
        hits = []
        for loc in row["locations"]:
            if not (repo / loc["path"]).exists():
                continue
            match = re.search(r"中的\s+([a-z][a-z0-9_-]*)", str(loc.get("symbol", "")))
            if match is None:
                return result_row(row, "pending_semantic_check", "生成内容缺少精确旧命令标记")
            old = match.group(1)
            tree = read_tree(repo, loc["path"])
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if not re.search(r"(?<![\w-])" + re.escape(old) + r"(?![\w-])", node.value):
                    continue
                parent = parents.get(node)
                if old == "subagent-run" and node.value == "subagent-run:" and loc["path"] == "agent_py_agent/cli/scenario_cases/subagent_cases.py":
                    container = parents.get(parent)
                    if isinstance(container, ast.keyword) and container.arg == "request_id":
                        continue
                    if isinstance(container, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "request_id" and v is parent for k, v in zip(container.keys, container.values)):
                        continue
                if isinstance(parent, ast.Expr):
                    continue
                call = parent if isinstance(parent, ast.Call) else parents.get(parent)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "add_parser":
                    continue
                hits.append({"path": loc["path"], "line": node.lineno, "old": old})
        return result_row(row, "fail" if hits else "pass", {"remaining_generated_references": hits})
    if row["id"] == "RD-15":
        text = (repo / "CLI_REFERENCE.md").read_text(encoding="utf-8")
        missing = [name for name in manifest["canonical_roots"] if "`my-agent " + name + "`" not in text]
        if missing:
            return result_row(row, "fail", {"missing_canonical_docs": missing})
        if not row.get("verification_checks"):
            remaining = [loc["path"] for loc in row["locations"] if loc["path"] != "CLI_REFERENCE.md"]
            return result_row(row, "pending_semantic_check", {"own_cli_roots_verified": True, "remaining_documentation": remaining,
                              "reason": "其余平台文档须在集成分支补充准确检查，不能用CLI根目录存在外推整条RD-15通过"})
    # 领域worker可在manifest补充精确、可执行的结构合同；没有规则时不能自动宣称删除。
    checks = row.get("verification_checks", [])
    if checks:
        results = []
        for check in checks:
            path = (repo / check["path"]).resolve()
            if not path.is_relative_to(repo.resolve()):
                raise ValueError("结构合同路径越出仓库")
            if check["kind"] == "file_absent":
                results.append(not path.exists())
            elif check["kind"] == "symbol_absent":
                results.append(not path.exists() or check["symbol"] not in defined_symbols(read_tree(repo, check["path"])))
            elif check["kind"] == "text_absent":
                results.append(check["text"] not in path.read_text(encoding="utf-8"))
            else:
                return result_row(row, "pending_semantic_check", "未知结构合同类型")
        return result_row(row, "pass" if all(results) else "fail", {"checks": results})
    return result_row(row, "pending_semantic_check", "该领域替代/分支语义尚无精确结构合同，需所属席位补证")


# LLM: 两个保留合同和真实守护入口始终核对；跨线项保持原责任，不能用单席通过外推全仓完成。
# 函数用途: 运行指定席位结构门并完整报告全部清单的覆盖与欠账。
def verify(repo=ROOT, *, worker="all", manifest_path=MANIFEST):
    repo = Path(repo).resolve()
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    rows = manifest["rows"]
    ids = {r["id"] for r in rows}
    retained = {r["id"] for r in rows if r["retained_contract_correction"]}
    if len(rows) != 383 or len(ids) != 383 or retained != {"SURF-DEL-195", "SURF-DEL-268"}:
        raise ValueError("383项及两项保留合同清单不完整")
    if len(manifest["cli_mapping"]) != 144 or len({p.split()[0] for p in manifest["cli_mapping"]}) != 70:
        raise ValueError("70根144路径的原始输入映射不完整")
    if any(r["implementation_worker"] not in {"sol-" + str(i) for i in range(1, 10)} for r in rows):
        raise ValueError("清单包含未知责任席位")
    snapshot = cli_snapshot(repo)
    global_checks = []
    global_checks.append({"kind": "canonical_roots", "pass": len(snapshot["roots"]) == 24 and snapshot["roots"] == sorted(manifest["canonical_roots"])})
    missing_targets = sorted(set(manifest["cli_mapping"].values()) - set(snapshot["paths"]))
    global_checks.append({"kind": "all_144_cli_mappings", "pass": not missing_targets, "missing": missing_targets})
    for path, frozen in manifest["frozen_cli"].items():
        global_checks.append({"kind": "frozen_cli", "path": path, "pass": snapshot["frozen_cli"].get(path) == frozen})
    for module in manifest["frozen_modules"]:
        path = repo / module.replace(".", "/")
        exists = path.with_suffix(".py").is_file() or (path / "__main__.py").is_file()
        global_checks.append({"kind": "frozen_module", "module": module, "pass": exists})
    for row in rows:
        if row["retained_contract_correction"]:
            global_checks.append({"kind": "retained_contract", "id": row["id"], "pass": retained_check(repo, row)})
    assigned = set(manifest.get("task_assignments", {}).get(worker, ()))
    if not assigned <= ids:
        raise ValueError("任务分配包含清单外ID")
    selected = [r for r in rows if worker == "all" or r["implementation_worker"] == worker or r["id"] in assigned]
    results = []
    for row in selected:
        if worker != "all" and row["implementation_worker"] != worker:
            results.append(result_row(row, "cross_worker_dependency", "只映射责任，须在集成分支核验，不跨线修改或宣称删除"))
            continue
        try:
            results.append(check_row(repo, row, manifest, snapshot))
        except (OSError, SyntaxError, ValueError, KeyError) as exc:
            results.append(result_row(row, "fail", {"error_type": type(exc).__name__, "error": str(exc)}))
    references = forbidden_references(repo, modules=("agent_py_agent.cli.memory_doctor", "agent_py_agent.cli.memory_commands.memory_doctor_cmd"),
                                      symbols=("cmd_memory_doctor",), allow_paths=("scripts/verify_optimization_removal.py",))
    global_checks.append({"kind": "removed_doctor_consumers", "pass": not references, "hits": references})
    counts = dict(collections.Counter(r["status"] for r in results))
    failed = any(not c["pass"] for c in global_checks) or counts.get("fail", 0) > 0
    incomplete = counts.get("pending_semantic_check", 0) > 0
    status = "fail" if failed else "incomplete" if incomplete else "pass"
    return {"schema": "my-agent.optimization-removal-result.v2", "status": status, "worker": worker,
            "inspection_baseline": manifest["inspection_baseline"], "global_checks": global_checks, "counts": counts,
            "manifest_coverage": {"total_rows": len(rows), "selected_rows": len(selected), "retained_contracts": 2,
                                  "extra_candidates": len(manifest["extra_candidates"])},
            "rows": results, "extra_candidates": manifest["extra_candidates"],
            "cross_worker_dependencies": [{"id": r["id"], "worker": r["implementation_worker"]} for r in rows if r not in selected],
            "limitation": "结构门通过不证明行为测试通过；requires_behavioral_test项必须与指定pytest结果合并验收。"}


# LLM: 仅由主入口校验后的缓存路径调用；临时文件写完再替换目录项，不就地截断硬链接目标。
# 函数用途: 原子替换独立诊断报告，不改写同inode的产品原件。
def _write_report(output, text):
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".optimization-removal-", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, output)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


# LLM: 默认只打印JSON；--output是显式报告输出，不写产品文件。失败和未知分别返回1/2。
# 函数用途: 提供分席位、全量及旧audit脚本兼容的命令行入口。
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--worker", choices=["all", *("sol-" + str(i) for i in range(1, 10))], default="all")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify(args.repo, worker=args.worker, manifest_path=args.manifest)
    except Exception as exc:
        report = {"status": "fail", "error_type": type(exc).__name__, "error": str(exc)}
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = args.output.resolve()
        repo = args.repo.resolve()
        in_report_dir = output.is_relative_to(repo / ".pytest_cache")
        if output.suffix != ".json" or not in_report_dir or output in {args.manifest.resolve(), MANIFEST.resolve(), Path(__file__).resolve()}:
            parser.error("报告仅允许写入指定仓库的.pytest_cache内独立JSON，不能覆盖产品、清单或仓外文件")
        try:
            _write_report(output, text)
        except OSError as exc:
            report = {**report, "status": "fail", "phase": "report_write", "error_type": type(exc).__name__}
            text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(text, end="")
    return 0 if report["status"] == "pass" else 2 if report["status"] == "incomplete" else 1


if __name__ == "__main__":
    raise SystemExit(main())

# LLM: 这是开发测量工具，不进入 wheel；只能操作显式已有的隔离 home，安装复用产品服务，真凭据只由该 home 档案解析。
# 模块用途: 安装样包并测量新会话首句 A/B/C，输出最小 JSONL 和 Markdown，不执行模型工具或包脚本。
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

# 允许照抄 python scripts/eval/pack_pick_bench.py，不依赖用户系统 editable 安装。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
from agent_py_agent.agent.plugin_package import read_plugin_package
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import (
    owner_identity_from_config,
    resolve_owner_home,
)
from scripts.eval.pack_pick_results import judge, summary
from scripts.eval.pack_pick_runtime import BenchCase, arm_context, run_case

__all__ = ["arm_context", "judge", "main", "make_agent", "safe_home", "summary"]


# LLM: resolve 同时覆盖相对路径、符号链接和祖先；在任何创建目录或初始化产品服务之前调用。
# 函数用途: 拒绝真实家目录及其子目录和祖先，home 还必须已经存在。
def safe_home(raw: str | Path) -> Path:
    path = safe_output(raw)
    if not path.is_dir():
        raise ValueError("--home 必须是已经存在的隔离目录")
    return path


# LLM: 输出路径也守相同真实 home 边界，不允许用 --out 绕过 --home 保护。
# 函数用途: 校验可创建的隔离输出路径。
def safe_output(raw: str | Path) -> Path:
    path = Path(raw).expanduser().resolve()
    real = (Path.home() / ".my-agent").resolve()
    if path == real or real in path.parents or path in real.parents:
        raise ValueError("拒绝使用真实 my-agent 家目录、其内部路径或祖先")
    return path


# LLM: MY_AGENT_HOME 是一些产品 helper 的默认源，显式 config 与环境必须同指隔离 home；结束恢复原环境。
# 函数用途: 给本轮初始化统一隔离地址，不读取或打印环境里的凭据。
@contextmanager
def isolated_home(home: Path):
    with patch.dict(os.environ, {"MY_AGENT_HOME": str(home)}):
        yield


# LLM: 初始 echo 只负责无网络装配；真实业务由原会话档案作用域解析后端，fake 在生成传输层替换。
# 函数用途: 创建 local/main 产品实例，保持默认 prompt、工具及能力目录，不启动 Gateway 或后台服务。
def make_agent(home: Path) -> SimpleAgent:
    config = AgentConfig(model_backend="echo", api_key_env="", my_agent_home=str(home),
                         my_agent_owner_provider="local", my_agent_owner_kind="main", my_agent_owner_id="main",
                         gateway_per_user_owner_scoping=False)
    return SimpleAgent(config, home / "owners" / "local" / "main")


# LLM: 只接受内容包；先用产品 reader 复验所有输入，再调用原管理安装/启用链，绝不自写安装表或运行包代码。
# 函数用途: 将目录里的 zip 内容包装入隔离 local/main 并启用，返回包编号。
def setup(home: Path, packages: Path) -> list[str]:
    archives = sorted(packages.glob("*.zip")) if packages.is_dir() else []
    if not archives:
        raise ValueError("--packages 目录没有 .zip 能力包")
    snapshots = [read_plugin_package(path) for path in archives]
    if any(not row.manifest.is_content_only for row in snapshots):
        raise ValueError("setup 只允许纯内容能力包")
    agent = make_agent(home)
    owner = resolve_owner_home(home, owner_identity_from_config(agent.config))
    context = plugin_management_context(owner, agent.home_paths, agent.config, agent.conversation_store.threads,
                                       actor_id="local-agent", channel="chat", conversation_id="pack-pick-setup", is_admin=True)
    manager = PluginManagement(context)
    destination = owner.home_dir / "bench-inputs"
    destination.mkdir(exist_ok=True)
    for path, package in zip(archives, snapshots):
        copied = destination / (package.sha256 + ".zip")
        shutil.copyfile(path, copied)
        _management_command(manager, f"/plugins install {shlex.quote(str(copied))}")
        _management_command(manager, f"/plugins enable {shlex.quote(package.manifest.plugin_id)}")
    return [row.manifest.plugin_id for row in manager.installations.snapshot() if row.enabled]


# LLM: 每个管理动作使用原当前目录 revision；非成功不续跑，不自动确认可执行程序或核验授权。
# 函数用途: 执行一次产品已有的内容包安装/启用命令。
def _management_command(manager: PluginManagement, text: str) -> None:
    import uuid

    reply = manager.command(text, revision=manager.catalog().revision, request_id="pick-setup-" + uuid.uuid4().hex)
    if not reply.get("ok"):
        raise RuntimeError("BENCH_SETUP_FAILED:" + str(reply.get("error_code") or reply.get("reason") or "unknown"))


# LLM: 样本身份与正例必须在运行前冻结且去重，错误输入不能触发产品初始化或模型调用。
# 函数用途: 验证题目数组，保留用户原始题面，不做语义改写。
def read_queries(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows or any(not _valid_query(row) for row in rows):
        raise ValueError("句子文件必须是非空数组，每项含 id/query/lang/query_domain/positive_packs")
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("句子编号重复")
    return rows


# LLM: 判定只读字段类型和值，不根据自然语言猜语言、领域或正例。
# 函数用途: 校验一条句子记录。
def _valid_query(row: object) -> bool:
    if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key].strip()
                                       for key in ("id", "query", "lang", "query_domain")):
        return False
    packs = row.get("positive_packs")
    return isinstance(packs, list) and all(isinstance(key, str) and bool(key.strip()) for key in packs) and len(packs) == len(set(packs))


# LLM: 输出排他创建避免覆盖旧评测；每例立刻追加并刷新汇总，失败保留已有数据而不静默续跑。
# 函数用途: 执行一组新会话首句，保存每调用观察和复跑所需的版本、包、题目指纹。
def run(args: argparse.Namespace, home: Path) -> None:
    questions = read_queries(Path(args.queries))
    arms = args.arms.split(",")
    if args.repeat < 1 or not arms or any(arm not in {"A", "B", "C"} for arm in arms) or len(set(arms)) != len(arms):
        raise ValueError("--arms 只接受不重复的 A,B,C；--repeat 必须为正整数")
    if args.fake and args.profile:
        raise ValueError("--fake 不得同时指定真实档案")
    if not args.fake and not args.profile:
        raise ValueError("真跑必须显式指定隔离 home 里的 --profile 档案")
    out = safe_output(args.out)
    agent = make_agent(home)
    _check_packages(agent, questions)
    out.mkdir(parents=True, exist_ok=False)
    _write_metadata(out, agent, args)
    rows = []
    with (out / "calls.jsonl").open("x", encoding="utf-8") as stream:
        for case in _cases(questions, arms, args):
            batch = run_case(agent, case)
            _flush_case(stream, rows, batch, out)


# LLM: 请求失败也要先保存原账本观察；保存完成后才非零退出，不把失败静默排除分母。
# 函数用途: 立即保存一例及更新汇总，失败阻止发送下一例。
def _flush_case(stream, rows: list[dict], batch: list[dict], out: Path) -> None:
    stream.writelines(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in batch)
    stream.flush()
    rows.extend(batch)
    (out / "summary.md").write_text(summary(rows), encoding="utf-8")
    if any(not row["request_ok"] for row in batch):
        raise RuntimeError("BENCH_REQUEST_FAILED")


# LLM: 所有正例必须是本轮真实已启用目录可发现的包；不为假模型补候选或装包。
# 函数用途: 拒绝缺包和拼错正例的评测输入。
def _check_packages(agent: object, questions: list[dict]) -> None:
    visible = {row.package_id for row in agent.current_skill_snapshot().packages}
    positives = {key for row in questions for key in row["positive_packs"]}
    if not visible or not positives.issubset(visible):
        raise ValueError("已启用能力包为空，或正例包不在当前可发现目录")


# LLM: 同一题每臂每重复都有唯一会话；顺序固定便于复验，不隐式随机改题。
# 函数用途: 按重复、题目、组别产生待测例。
def _cases(questions: list[dict], arms: list[str], args: argparse.Namespace):
    for repeat in range(1, args.repeat + 1):
        yield from (BenchCase(question, arm, repeat, args.profile or "", args.fake) for question in questions for arm in arms)


# LLM: 仅记文件/包/源码指纹与档案编号，不能复制密钥、原句文件或包正文。
# 函数用途: 固定本次评测输入，便于 3a 对齐真跑版本和结果。
def _write_metadata(out: Path, agent: object, args: argparse.Namespace) -> None:
    source = Path(__file__).resolve().parents[2]
    files = [*sorted((source / "scripts" / "eval").glob("pack_pick_*.py")),
             source / "agent_py_agent" / "agent" / "capability" / "router.py",
             source / "agent_py_agent" / "agent" / "agent_core" / "_tool_loop_service.py"]
    config = capability_config_for_agent(agent)
    budgets = {key: getattr(config, key) for key in ("capability_package_selection_max_input_tokens",
               "capability_candidate_limit", "capability_bundle_max_tokens", "enable_capability_package_recommendations")}
    data = {"schema": "pack_pick_bench.v1", "fake": args.fake, "profile": args.profile, "arms": args.arms, "capability_settings": budgets,
            "repeat": args.repeat, "queries_sha256": hashlib.sha256(Path(args.queries).read_bytes()).hexdigest(),
            "source_sha256": {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
            "packages": [row.to_ref() for row in agent.current_skill_snapshot().packages]}
    (out / "metadata.json").write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


# LLM: CLI 只暴露 setup/run，fake 明确是校准而非真实评测；不提供任意模型端点或密钥参数。
# 函数用途: 定义可照抄的测量命令。
def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="隔离环境测量新会话第一步是否挑中能力包（不执行模型工具）")
    commands = result.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("setup", help="用产品服务安装并启用内容包")
    prepare.add_argument("--home", required=True)
    prepare.add_argument("--packages", required=True)
    measure = commands.add_parser("run", help="测量首次选择及工具意图")
    measure.add_argument("--home", required=True)
    measure.add_argument("--queries", required=True)
    measure.add_argument("--arms", default="A,B,C")
    measure.add_argument("--out", required=True)
    measure.add_argument("--repeat", type=int, default=1)
    measure.add_argument("--profile")
    measure.add_argument("--fake", action="store_true", help="只校准测量工具，不发网络请求")
    return result


# LLM: 在任何产品初始化前检查真实 home；异常只输出类型不泄露供应商正文或凭据，返回非零。
# 函数用途: 执行测量命令，写文件仅在隔离目录及显式输出目录。
def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        home = safe_home(args.home)
        _dispatch(args, home)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"测量失败（{type(exc).__name__}）；请核对隔离目录、句子格式、包与模型档案。", file=sys.stderr)
        return 1


# LLM: 仅安全 home 校验通过后进入环境作用域；任何错误都会恢复原环境，不吞错误或写生产状态。
# 函数用途: 在同一隔离环境分派安装或测量。
def _dispatch(args: argparse.Namespace, home: Path) -> None:
    with isolated_home(home):
        if args.command == "setup":
            print(json.dumps({"enabled_packages": setup(home, Path(args.packages))}, ensure_ascii=False))
        else:
            run(args, home)


if __name__ == "__main__":
    raise SystemExit(main())

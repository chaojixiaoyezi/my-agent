"""CLI 自动续跑循环（2026-08-14 根因3 设计 v2，审查通过后实施）。

cmd_run 首轮收口后，若收口是可续跑族（共享 gate should_continue_task），
进程内循环续跑——同一 task/run/thread 链路（CliContinuationContext 贯穿），
直到：任务完成 / 预算耗尽 / 不可续跑族（blocked/协议违规/UNKNOWN）/ 用户
停止。续跑轮不走 cli_one_shot failed 兜底（切片3），任务级非终态由本循环
决定。

防失控双保险：resume_limit（policy 预算，默认 3，ensure_ordinary_task_resume
单一权威递增）+ max_rounds（进程内护栏，默认 8）。预算耗尽写
continuation_budget_exhausted 事件，绝不输出 DONE。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..agent.agent_core.cli_run_conversation import bind_cli_run_conversation
from ..agent.agent_core.runtime.loop_models import RunParams
from .resume_contract import (
    CliContinuationContext,
    record_budget_exhausted,
    resume_prompt_for,
)


@dataclass(frozen=True)
class CliResumeOutcome:
    """续跑循环结果（结构化，供 cmd_run 收口与审计）。"""

    status: str  # completed / budget_exhausted / stopped / unresumable
    rounds: int  # 首轮 + 续跑轮数
    final_result: object = None
    reason: str = ""


def should_resume(result: object) -> tuple[bool, str]:
    """收口后是否续跑（共享 gate，gateway 与 CLI 同一判断）。"""
    from ..agent.conversation.runtime import should_continue_task

    return should_continue_task(result)


class ResumeRunOnce:
    """首轮/续跑轮的 run 封装：契约 ID 贯穿同一 task/run/thread 链路。

    - 首轮：bind_cli_run_conversation 显式绑定（thread 建在
      ConversationStore），从绑定 params 构建 CliContinuationContext。
    - 续跑轮：ctx.apply_to 覆写 ID（request_id=root#cont-N, run/task=
      root, thread 复用），resume_context=True。
    """

    def __init__(
        self,
        agent: object,
        *,
        base_params: RunParams,
        on_chunk: object = None,
        save: bool = False,
        delivery_contract: object = None,
    ):
        self.agent = agent
        self.base_params = base_params
        self.on_chunk = on_chunk
        self.save = save
        self.delivery_contract = delivery_contract
        self.ctx: CliContinuationContext | None = None

    def __call__(
        self,
        prompt: str,
        continuation_seq: int = 0,
        attempt_id: str = "",
        continuation_reason: str = "",
    ) -> tuple:
        """返回 (result, bound_params)。首轮后 self.ctx 建立。"""
        if continuation_seq == 0:
            bound = bind_cli_run_conversation(self.agent, self.base_params, prompt)
            result = self.agent.run(
                prompt,
                params=bound,
                save=self.save,
                source="cli_run",
                delivery_contract=self.delivery_contract,
                on_chunk=self.on_chunk,
            )
            # EXEC-29c: 首轮 run 完成后, attach_run_task_workspace_context 已把
            # 权威 task root 写入 agent._current_run_task_workspace。bind 返回的
            # bound 只有 conversation_thread_id、没有 run_workspace(workspace 是
            # agent.run 内部 attach 才生成的)——EXEC-29b 从 bound 读 workspace
            # 恒为空、条件不成立、回写从未发生, 真机(ma-a r2b)续跑仍新建
            # "系统续跑-1"目录。从这里构造 run_workspace 回写 base_params,
            # 续跑轮 attach 走 _existing_workspace_paths 复用首轮目录
            # 。output/work 子目录取
            # _existing_workspace_paths 同款默认(root/output、root/work)。
            root_text = str(
                getattr(self.agent, "_current_run_task_workspace", "") or ""
            ).strip()
            if root_text:
                merged = dict(getattr(self.base_params, "task_attributes", None) or {})
                merged["run_workspace"] = {
                    "task_root": root_text,
                    "output_dir": str(Path(root_text) / "output"),
                    "work_dir": str(Path(root_text) / "work"),
                }
                self.base_params = replace(self.base_params, task_attributes=merged)
            thread_id = str(
                (bound.task_attributes or {}).get("conversation_thread_id") or ""
            )
            # parent_attempt 记首轮真实 DB attempt(双席复核 seq1845 硬门2:
            # bound.attempt_id 是 agent.run 前的合成值, agent.run 内部
            # create_attempt 才发放 DB 权威 attempt——首轮收口后从 DB 查
            # 真实 attempt 作树形链起点, 不依赖 pre-run 值)。
            real_first = self._latest_db_attempt()
            self.ctx = CliContinuationContext(
                root_task_id=str(bound.task_id or bound.run_id or ""),
                root_run_id=str(bound.run_id or bound.request_id or ""),
                root_thread_id=thread_id,
                root_request_id=str(bound.request_id or ""),
                parent_attempt_id=real_first or str(bound.attempt_id or ""),
            )
            return result, bound
        if self.ctx is None:
            raise RuntimeError("resume round without first-round continuation context")
        # 缺口B(双席复核 seq1834): attempt_id 不传合成 ID——传空让
        # run_params_with_request_id 生成 + agent.run 内部 create_attempt
        # 发放 DB current attempt(同 run_id 续挂, 见 runtime_mixin:321-368),
        # ctx 只记录 parent 关系, attempt 链是真实 DB 链。
        bound = self.ctx.apply_to(
            self.base_params, seq=continuation_seq, attempt_id=""
        )
        # 缺口F(双席复核 seq1835): 上轮收口原因写进 params——
        # bind_cli_run_conversation 的续跑分支把它放进消息结构化 metadata
        # (typed continuation event), 不依赖提示文本解析。
        if continuation_reason:
            bound = replace(
                bound, continuation_reason=str(continuation_reason)
            )
        result = self.agent.run(
            prompt,
            params=bound,
            save=self.save,
            source="cli_run",
            resume_context=True,
            delivery_contract=self.delivery_contract,
            on_chunk=self.on_chunk,
        )
        # 缺口B: 续跑轮 attempt 由 agent.run 内部 create_attempt 发放
        # (DB current attempt, 同 run 续挂), 这里从 repo 查最新 attempt
        # 记录为真实链——不合成 attempt-{seq}。
        real_attempt = self._latest_db_attempt()
        if real_attempt:
            self.ctx = self.ctx.next(attempt_id=real_attempt)
        return result, bound

    def _task_run_id(self) -> str:
        """从 runtime.db 查当前 agent_run 的 task_run_id（账本完整链）。

        双席复核(seq1847 维护记录): continuation_budget_exhausted 事件此前
        task_run_id 恒空——attempt/agent_run 有值不等于 task/run/attempt/
        event ledger 完整闭合。预算事件补齐 task_run_id 后整链可反查。
        """
        try:
            subagents = getattr(self.agent, "subagents", None)
            repo = getattr(subagents, "runtime_db", None)
            if repo is None or self.ctx is None:
                return ""
            row = repo.agent_run_for_run_id(self.ctx.root_run_id)
            if row is None:
                return ""
            return str(row["task_run_id"] or "")
        except Exception:  # noqa: BLE001 查不到保守返回空(fail-silent)
            return ""

    def _latest_db_attempt(self) -> str:
        """从 runtime.db 查当前 agent_run 的最新 attempt（DB 权威链）。"""
        try:
            subagents = getattr(self.agent, "subagents", None)
            repo = getattr(subagents, "runtime_db", None)
            if repo is None or self.ctx is None:
                return ""
            row = repo.agent_run_for_run_id(self.ctx.root_run_id)
            if row is None:
                return ""
            latest = repo._runtime_connect().execute(
                "SELECT attempt_id FROM agent_attempts "
                "WHERE agent_run_id = ? ORDER BY started_at DESC LIMIT 1",
                (str(row["agent_run_id"]),),
            ).fetchone()
            return str(latest["attempt_id"] or "") if latest else ""
        except Exception:  # noqa: BLE001 查不到保守返回空(沿用上轮 parent)
            return ""



def _write_handoff(
    agent: object,
    runner: "ResumeRunOnce",
    *,
    seq: int,
    reason: str,
    user_prompt: str = "",
) -> None:
    """显式移交(2026-08-14 长任务首要约束 owner seq1856/steward seq1857):
    预算耗尽/不可续跑收口时写 runtime.db 待接管移交单——runtime.db 是
    owner 级共享权威(CLI 与 gateway 同库), gateway 调度器扫描接管续跑,
    不依赖进程 cwd 或入口私有 conversation store(CLI/gateway store 隔离
    真机坐实导致任务截断)。fail-silent: 移交失败不阻断执行路径。"""
    try:
        subagents = getattr(agent, "subagents", None)
        repo = getattr(subagents, "runtime_db", None)
        if repo is None or not callable(getattr(repo, "create_continuation_handoff", None)):
            return
        if runner.ctx is None:
            return
        repo.create_continuation_handoff(
            agent_run_id="",
            attempt_id=runner.ctx.parent_attempt_id,
            task_run_id=runner._task_run_id(),
            root_run_id=runner.ctx.root_run_id,
            root_request_id=runner.ctx.root_request_id,
            root_thread_id=runner.ctx.root_thread_id,
            root_task_id=runner.ctx.root_task_id,
            user_prompt=str(user_prompt or ""),
            continuation_seq=int(seq or 0),
            reason=str(reason or ""),
        )
    except Exception:  # noqa: BLE001 移交失败保守跳过
        pass



# EXEC-30: 同一收口 reason 连续续跑上限(对照 会话运行时/轻量运行时 写完即测即收的
# 天然收敛的机制等价物)。同因 3 次仍不收敛=模型在同一模式无限迭代, 强制收口。
_SAME_REASON_RESUME_LIMIT = 3


def run_with_resume(
    agent: object,
    *,
    initial_prompt: str,
    base_params: RunParams,
    max_rounds: int | None = None,
    on_chunk: object = None,
    save: bool = False,
    delivery_contract: object = None,
) -> CliResumeOutcome:
    """首轮 + 续跑轮循环（进程内，同一 task/run/thread 链路）。

    2026-08-15 3×3 真机(cell2 bs4): max_rounds 硬编码 8 对复刻类长任务
    太紧——续跑链 8 轮×resume_limit 3 次≈24 轮就预算耗尽死停等用户「继续」,
    无人值守 CLI 场景任务必死。改为读配置 cli_resume_max_rounds(默认 8
    保持现状, 普通交互任务防失控语义不变), 长任务部署可调大。
    """
    if max_rounds is None:
        max_rounds = int(
            getattr(getattr(agent, "config", None), "cli_resume_max_rounds", 0) or 8
        )
    runner = ResumeRunOnce(
        agent,
        base_params=base_params,
        on_chunk=on_chunk,
        save=save,
        delivery_contract=delivery_contract,
    )
    result, _bound = runner(initial_prompt, 0, "")
    rounds = 1
    same_reason_streak = 0
    should, reason = should_resume(result)
    if not should:
        return CliResumeOutcome(
            status="unresumable", rounds=rounds, final_result=result, reason=reason
        )
    if runner.ctx is None or not runner.ctx.root_task_id:
        return CliResumeOutcome(
            status="unresumable", rounds=rounds, final_result=result,
            reason="no_continuation_contract",
        )

    # 缺口A(双席复核 seq1834): 进入续跑循环前 claim 一次(整个循环持有,
    # lease 300s 覆盖进程内多轮)——CLI 与 gateway 同读同写同一 policy claim,
    # 已被其他 consumer 持有则整个续跑放弃(等 gateway/下次), 不双跑。
    # 缺口A真机补充(2026-08-14 testbox): try_claim 依赖
    # kind=ordinary_task_resume 的 policy, 但该 policy 由收口 finalization
    # (_schedule_typed_unfinished_continuation)创建——那是 gateway 调度器
    # 路径, CLI one-shot run 从不经过 → 找不到 policy 永远 False, 永不续跑
    # (测试里 fake agent 显式建 policy 掩盖了此洞)。修: claim 前先
    # ensure_ordinary_task_resume 建/递增同一 policy(预算单一权威),
    # 预算耗尽(返回 False) → budget_exhausted, 与 gateway 互斥语义不变。
    from ..agent.conversation.runtime import ensure_ordinary_task_resume
    from .resume_contract import try_claim_cli_resume

    store = getattr(agent, "conversation_store", None)
    if store is not None:
        ensured = ensure_ordinary_task_resume(
            agent,
            task_id=runner.ctx.root_task_id,
            thread_id=runner.ctx.root_thread_id,
            store=store,
            due_now=True,
        )
        if not ensured:
            record_budget_exhausted(
                agent=agent,
                run_id=runner.ctx.root_run_id,
                attempt_id=runner.ctx.parent_attempt_id,
                task_run_id=runner._task_run_id(),
                budget_before=0,
                budget_after=0,
                reason="resume_limit_reached",
            )
            _write_handoff(
                agent, runner, seq=rounds, reason="resume_limit_reached",
                user_prompt=initial_prompt,
            )
            return CliResumeOutcome(
                status="budget_exhausted", rounds=rounds, final_result=result,
                reason="resume_limit_reached",
            )
        if not try_claim_cli_resume(store, runner.ctx.root_task_id):
            return CliResumeOutcome(
                status="unresumable", rounds=rounds, final_result=result,
                reason="claim_held_by_other_consumer",
            )
    for seq in range(1, max_rounds + 1):
        prompt = resume_prompt_for(
            continuation_reason=reason,
            continuation_seq=seq,
            user_task=initial_prompt,
        )
        # 缺口F: 上轮收口原因随 params 传续跑轮(消息结构化 metadata)
        result, _bound = runner(
            prompt, seq, attempt_id="", continuation_reason=reason
        )
        rounds += 1
        # parent_attempt 推进在 ResumeRunOnce 内部完成（DB 真实 attempt）
        should, new_reason = should_resume(result)
        # EXEC-30 续跑收敛 gate(阶段三 gorm 真机): 同一 runtime_reason 连续
        # 续跑≥3 次说明模型在同一收口模式里无限迭代(13h/268 写操作不停,
        # 对照 会话运行时/轻量运行时 写完即测即收)。同因循环到限强制收敛, 不再续跑——
        # 与 EXEC-04'同类失败循环'防护同族, 防无人值守无限续跑。reason 变了
        # (新问题)则重新计数, 不误杀有进展的续跑链。
        if should and new_reason == reason:
            same_reason_streak += 1
            if same_reason_streak >= _SAME_REASON_RESUME_LIMIT:
                record_budget_exhausted(
                    agent=agent,
                    run_id=runner.ctx.root_run_id,
                    attempt_id=runner.ctx.parent_attempt_id,
                    task_run_id=runner._task_run_id(),
                    budget_before=rounds - 1,
                    budget_after=rounds,
                    reason="same_reason_resume_limit_reached",
                )
                _write_handoff(
                    agent, runner, seq=rounds, reason="same_reason_resume_limit_reached",
                    user_prompt=initial_prompt,
                )
                return CliResumeOutcome(
                    status="budget_exhausted", rounds=rounds,
                    final_result=result, reason="same_reason_resume_limit_reached",
                )
        elif should:
            same_reason_streak = 1
        reason = new_reason
        if not should:
            # 缺口C(双席复核 seq1835): 不可续跑族收口(blocked/协议违规/
            # UNKNOWN)必须标 unresumable, 不能误标 completed——只有
            # runtime_status=ok 的任务完成才算 completed。
            runtime_status = str(
                getattr(result, "runtime_status", "") or ""
            ).strip().lower()
            if runtime_status == "ok":
                return CliResumeOutcome(
                    status="completed", rounds=rounds, final_result=result, reason=reason
                )
            return CliResumeOutcome(
                status="unresumable", rounds=rounds, final_result=result, reason=reason
            )
        if rounds >= max_rounds + 1:
            record_budget_exhausted(
                agent=agent,
                run_id=runner.ctx.root_run_id,
                attempt_id=runner.ctx.parent_attempt_id,
                task_run_id=runner._task_run_id(),
                budget_before=rounds - 1,
                budget_after=rounds,
                reason="max_rounds_reached",
            )
            _write_handoff(
                agent, runner, seq=rounds, reason="max_rounds_reached",
                user_prompt=initial_prompt,
            )
            return CliResumeOutcome(
                status="budget_exhausted", rounds=rounds, final_result=result,
                reason="max_rounds_reached",
            )
    record_budget_exhausted(
        agent=agent,
        run_id=runner.ctx.root_run_id,
        attempt_id=runner.ctx.parent_attempt_id,
        task_run_id=runner._task_run_id(),
        budget_before=rounds,
        budget_after=rounds,
        reason="resume_limit_reached",
    )
    _write_handoff(
        agent, runner, seq=rounds, reason="resume_limit_reached",
        user_prompt=initial_prompt,
    )
    return CliResumeOutcome(
        status="budget_exhausted", rounds=rounds, final_result=result,
        reason="resume_limit_reached",
    )


# LLM: 手动续跑入口(EXEC-33, 对照 会话运行时 resume/轻量运行时 --continue/终端交互
# --continue): 非完成收口(unresumable)后任务不再死——用户可 run --resume
# <task_id|目录> 从持久事实源(task.yaml/run_workspace.json)恢复同一
# task/run/thread 链继续。续跑轮走 ResumeRunOnce 的 continuation 路径
# (bind 用 root_request_id 复用同一 ConversationStore thread), 与自动
# resume_loop 共用全部契约与 EXEC-30 收敛 gate。
# 函数用途: 恢复一个已存在 CLI 任务的执行; 找不到/不可恢复时明确报错。
def run_manual_resume(
    agent: object,
    *,
    task_ref: str,
    max_rounds: int | None = None,
    on_chunk: object = None,
    save: bool = False,
    delivery_contract: object = None,
) -> CliResumeOutcome:
    from pathlib import Path as _Path

    from ..agent.agent_core.runtime.loop_models import RunParams
    from ..agent.agent_core.runtime.run_params import run_params_with_request_id

    facts = _load_task_facts(agent, task_ref)
    if facts is None:
        return CliResumeOutcome(
            status="unresumable", rounds=0, final_result=None,
            reason="task_facts_not_found",
        )
    base_params = run_params_with_request_id(
        RunParams(
            source="cli_run",
            task_attributes={
                "run_workspace": facts["run_workspace"],
            },
            run_id=facts["run_id"],
            task_id=facts["task_id"],
            request_id=facts["request_id"],
            continuation_root_request_id=facts["request_id"],
            continuation_root_run_id=facts["run_id"],
            continuation_root_task_id=facts["task_id"],
            continuation_root_thread_id=facts.get("thread_id", ""),
            recovery_next_actions=[],
        )
    )
    runner = ResumeRunOnce(
        agent,
        base_params=base_params,
        on_chunk=on_chunk,
        save=save,
        delivery_contract=delivery_contract,
    )
    # 手动续跑没有首轮 result——直接用持久事实重建 ctx(同一 task/run/thread)。
    runner.ctx = CliContinuationContext(
        root_task_id=facts["task_id"],
        root_run_id=facts["run_id"],
        root_thread_id=facts.get("thread_id", ""),
        root_request_id=facts["request_id"],
        parent_attempt_id=facts.get("latest_attempt_id", ""),
    )
    if max_rounds is None:
        max_rounds = int(
            getattr(getattr(agent, "config", None), "cli_resume_max_rounds", 0) or 8
        )
    prompt = (
        f"继续执行之前的任务（{facts.get('title') or facts['task_id']}）。"
        "请基于现有工作目录里的进度继续推进直到完成交付。"
    )
    result, _bound = runner(
        prompt, 1, "", continuation_reason="manual_resume"
    )
    rounds = 1
    reason = "manual_resume"
    same_reason_streak = 0
    for seq in range(2, max_rounds + 2):
        should, new_reason = should_resume(result)
        if should and new_reason == reason:
            same_reason_streak += 1
            if same_reason_streak >= _SAME_REASON_RESUME_LIMIT:
                return CliResumeOutcome(
                    status="budget_exhausted", rounds=rounds,
                    final_result=result, reason="same_reason_resume_limit_reached",
                )
        elif should:
            same_reason_streak = 1
        reason = new_reason
        if not should:
            runtime_status = str(
                getattr(result, "runtime_status", "") or ""
            ).strip().lower()
            if runtime_status == "ok":
                return CliResumeOutcome(
                    status="completed", rounds=rounds, final_result=result,
                    reason=reason,
                )
            return CliResumeOutcome(
                status="unresumable", rounds=rounds, final_result=result,
                reason=reason,
            )
        next_prompt = resume_prompt_for(
            continuation_reason=reason,
            continuation_seq=seq,
            user_task=prompt,
        )
        result, _bound = runner(
            next_prompt, seq, attempt_id="", continuation_reason=reason
        )
        rounds += 1
    return CliResumeOutcome(
        status="budget_exhausted", rounds=rounds, final_result=result,
        reason="max_rounds_reached",
    )


def _load_task_facts(agent: object, task_ref: str) -> dict | None:
    """从 task.yaml/run_workspace.json 读取持久事实源(EXEC-33)。"""
    import json as _json
    from pathlib import Path as _Path

    home = getattr(agent, "home_paths", None)
    tasks_root = None
    if home is not None:
        owner_home = str(getattr(home, "owner_home_dir", "") or "").strip()
        if owner_home:
            tasks_root = _Path(owner_home) / "tasks"

    def _read_workspace_dir(candidate: _Path):
        task_yaml = candidate / "task.yaml"
        ws_json = candidate / "run_workspace.json"
        if not task_yaml.exists() or not ws_json.exists():
            return None
        try:
            # task.yaml 由 workspace 写入器生成, 是简单的 "key: JSON 值" 行格式
            # (项目不依赖第三方 yaml 库, 这里做最小解析)。
            meta: dict = {}
            for line in task_yaml.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                value = value.strip()
                if value:
                    try:
                        meta[key.strip()] = _json.loads(value)
                    except Exception:
                        meta[key.strip()] = value
        except Exception:
            return None
        try:
            ws = _json.loads(ws_json.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        return meta, ws, candidate

    ref = str(task_ref or "").strip()
    candidates: list[_Path] = []
    p = _Path(ref).expanduser()
    if p.is_dir():
        candidates.append(p / "work")
        candidates.append(p)
    if tasks_root is not None and tasks_root.exists():
        # 目录名以 task 标题命名;task_id 存于 task.yaml——扫描所有任务目录的
        # work/task.yaml 按 task_id 精确匹配(目录名含 ref 也收)。
        for day_dir in tasks_root.iterdir():
            if not day_dir.is_dir():
                continue
            for tdir in day_dir.iterdir():
                if not tdir.is_dir():
                    continue
                if ref in tdir.name or ref in str(tdir):
                    candidates.append(tdir / "work")
                    candidates.append(tdir)
                else:
                    candidates.append(tdir / "work")
    for cand in candidates:
        hit = _read_workspace_dir(cand)
        if not hit:
            continue
        meta, ws, work_dir = hit
        task_id = str(meta.get("task_id") or meta.get("request_id") or "")
        if task_id and task_id != ref and ref not in str(cand):
            # 扫描候选按 task_id 精确匹配;目录路径含 ref 的候选放行(模糊输入)
            continue
        task_root = str(work_dir.parent)
        return {
            "task_id": task_id,
            "run_id": str(meta.get("run_id") or meta.get("request_id") or ""),
            "request_id": str(meta.get("request_id") or ""),
            "thread_id": str(meta.get("thread_id") or ""),
            "title": str(meta.get("task_title") or ""),
            "latest_attempt_id": str(meta.get("latest_attempt_id") or ""),
            "run_workspace": {
                "task_root": task_root,
                "output_dir": str(ws.get("output_dir") or _Path(task_root) / "output"),
                "work_dir": str(ws.get("work_dir") or work_dir),
            },
        }
    return None

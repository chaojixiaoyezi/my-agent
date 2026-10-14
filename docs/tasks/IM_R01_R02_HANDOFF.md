# R01/R02 交接

## 基本信息

- workstream：IM 进度与状态恢复
- branch：codex/im-entry-update-assessment
- owner：进度子代理
- date：2026-10-14
- 共享同线工作树，未提交、未推送、未部署、未接触真实数据/服务。

## 本线目标与实际完成

原 ActivityHeartbeat 的单次 sink 失败不再永久消失：沿原间隔，最多连续三次失败，成功清零，取消/关闭失权。进度原 pending 保持同一 ID；已知 ID 的编辑失败按原账退避，发送最新候选，三次连续失败停止展示；未知首发停止自动重发。能力/路由拒绝是永久回执，provider bool/异常为未知结果，不解析异常正文。最终先轮询，不被展示退避卡住；未完成冻结 dispatch 仍按原安全重放合同处理。

进度完整内容先 common sanitizer 后裁剪；off 最小工具心跳保留真实 elapsed 与等待阶段，更新时间仍计入 100 字。/status 不将旧 started 宣称成当前执行，按 typed running/finished/interrupted/model_liveness/内容事件显示最近事实与时间未知。

审批基线失败的原因是旧合成请求没有 requested_at/expiry，当前合同正确 fail closed；测试补 future expires_at，新增缺失/过期负例，生产授权完全未改。

## 改动文件

- agent/tooling/liveness.py
- agent/adapter/progress_draft.py、delivery.py、manager.py（只最小 liveness renderer）
- agent/delivery/progress.py、service.py（只 deliver_progress）
- agent/gateway_parts/control_service.py（只 _recent_progress）
- tests/test_im_progress_draft.py、test_im_tool_liveness.py、test_gateway_verbose_progress.py
- docs/design/IM_PROGRESS_RECOVERY.md、本交接

## 测试命令和结果

使用项目 .venv Python/Ruff。

1. 五文件组合：test_im_progress_draft.py、test_im_tool_liveness.py、test_gateway_verbose_progress.py、test_model_wait_progress.py、test_slow_model_liveness.py，97 passed（新增两个审批负例之前）。
2. 最新 test_im_progress_draft.py：19 passed。
3. 最新 gateway_verbose_progress 的 session_approval_cache/missing_or_expired：4 passed。
4. 本线修改源码及测试 scoped Ruff：passed；git diff --check：passed。
5. strict code-size：失败，唯一 hard 为 scheduler/tool.py build_schedule_tool_model_spec 过长；该文件其它线正在编辑。命令生成 CODE_SIZE_REPORT.md，已通知根。
6. 随后全 verbose 重跑的六项失败均为 SimpleAgent 初始化触发其它线 scheduler EffectResolverPolicy 不支持 write；两项新增负例最初 fixture 不合法已修并重验。主线必须等其它线冻结后重跑组合，不能引用旧绿为当前总门通过。

测试新增 sink 故障恢复/连续失败上界、已知 ID 恢复和退避期最新候选、永久/未知/暂时编辑失败预算、未知首发防重复、final 优先及状态新鲜度。

## 需要主线重点复查与协调

根拥有 sanitizer 完整实现、主配置和根文档。无新增配置；这轮属既有恢复 bug fix。请将模块设计连到 DESIGN_LEDGER/CODEBASE_TREE/TESTS，并同步 covered module 四件套。delivery/service.py 根改其它方法、模型线改 manager 其它函数，同线不重叠。

参考检查：OpenAI Agents Python sandbox/util/retry.py 的 max attempts、成功退出和显式 retry_if；复用思想，未引第三方依赖。

## 剩余风险

真实 IM/provider 未验；provider boolean 丢失具体远端分类时必须保守未知。未知首发没有新消息 ID，不能安全恢复；已知编辑可安全替换同 ID，仍受原 claim/CAS 和进程活性约束。连续三次展示失败会停用进度，最终回复继续走原独立通道。生产原始状态/stop/final优先应由根组合验证。

建议下一步：根先等三线冻结后统一 focused/strict gate，再做授权的发布；其它 agent 可并行只读审查未知首发、同 ID 编辑和终态优先，不能碰本线文件或真实服务。

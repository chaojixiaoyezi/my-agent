# 后台认证拒绝通知交接

状态：源码候选，统一严格 gate / 新发行 / 真实验收由根代理负责；没有生产账本或平台操作。

## 原问题与合同

后台 precheck 与 NO 工具失败原来直接 suppressed；IM 新计划普通结果默认 local，LLM 也可明确 internal_only，因此原实现不能保证保存后的凭据被认证拒绝时通知原用户。参考 OpenClaw failure-alerts 将失败提醒与正常结果策略分开；本实现仍复用本仓原 Wake、BackgroundClaim、零工具 natural_user_reply 和 owner_delivery，不建立另一队列或授权。

只有实际执行的 typed `web_fetch` 首次 401→rejected 且原凭据锁确实更新当前代次的宿主事实产生义务；正文、模型字典、403/超时/网络错误、旧代或重复401均不能产生义务。归档只保留 opaque credential_ref 和严格整数 generation，HTTP正文和秘密不投影。当前凭据记录与原 active runner scope 才能证明发送资格。

precheck/NO 失败与后台LLM工具归档走同一义务。LLM 正常 finalization 在关闭准确 TaskLink 前冻结原 Wake；不能复活已完成任务。已有公开LLM回复可复用，其余沿原有界零工具表达，用户自行决定重填或停用；不自动发卡、停监控、改凭据或重试认证。

## 生命周期与权限

- 原 owner / actor / source thread / topic / task 不变，local/none 仅使普通结果静默；通知不回落管理员。
- 慢模型之后、实际运输之前再次检查原来源、active任务、job状态和当前凭据代次/撤销。
- 未发送通知缓存进入原 claim 后再重投；业务HTTP与模型都不重复。关闭通知、换代、撤销、取消/删除和来源漂移使义务退休。
- 已 external_sent 的冻结回执只补 canonical；后续换代不能使它重新外发或阻止原已发生事实收口。
- 既有重投期限耗尽沿原 quarantine 收原工作片：只取准确宿主义务和 task/thread，queued scheduler run 用原 claim/finish CAS；有其它活跃claim不夺取，终态不复活。原计划、历史和凭据不被删除或停用。
- 可选配置读取失败视作关闭；无认证事实的普通后台收尾保持原语义。

## 验证与边界

最终八文件 focused：165 passed，0 failed/error/skip，其中新通知专项44项。定向Ruff与doc_sync通过。最终原JUnit为仓外 `credential-auth-notice-final-focused3.xml`，旧红XML保留，准确SHA清单归仓外 `credential-auth-notice-frozen-handoff.json`；统一严格gate仍待根执行。

专项含真实临时凭据卡保存、原凭据当前性 helper、真实 Registry/ToolExecutor/HTTP handler、scheduler tick、finalization 和冻结重投；只有模型、末端HTTP和通道运输为合成替身。覆盖普通user私聊与group、precheck/NO/LLM、同代一次、慢表达后撤销/换代/off/stop/delete/来源变化、cold retry和typed中断；原红与最终XML见仓外交接JSON。

没有真实认证错误外发、云写入或生产凭据操作；源码通过不等于新发行或真实验收通过。普通LLM表达质量仍需最后真实验收，未保证固定措辞。

建议下一步：根代理合并已冻结TTL/HTTP合同后跑统一focused严格gate，再新发行与少量真实原用户通知验收。独立审阅可并行只读，生产部署与平台操作保持根代理独占。

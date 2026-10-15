# Skill 11—16 维护增量交接

状态：独立树本地实现；不提交、推送、部署、真实模型或真实用户投递。14已交根应用，本片以 `/tmp/skill-version-after` 精确14后快照生成增量，凭据文件全部排除。

## 生产入口与唯一事实源

- S1与S3只产候选；正式内容经现有认证 `/skills learned confirm|reject <id> <revision>` 或CLI明确确认。proposal-show为脱敏草稿；模型skill_feedback没有confirm/human评分入口。
- 原SkillLearningStore.registry持有revision、维护状态、import_operations；不建第二库。原owner quota→state锁序，候选、版本、登记与账本写入都计原owner配额。
- S1原committed receipt经原来源验证、精确hash和nofollow校验迁入registry，保留原owner布局；用户改过不接管。
- 自学正文不可变版本、单调revision与CAS继承14；序列化refs恢复可解析准确历史，删除/禁用仍撤权。无人引用版本只在自主策略且完整保护扫描后清理，保留当前版本与配置份数。
- 原TaskLink/child与scheduler job.skill_refs、scheduler_skill_refs保护活动/暂停/远期任务；读取不完整禁止归档/版本清理。固定不被自动挤掉，显式停用不自动复活，归档可恢复且区别用户删除。
- 推荐50是目录投影；总库默认不限数量，旧显式max_skills仍为原硬上限。搜索/get不被推荐投影截断。公共/手写/受管来源不自动纳管。
- skill_feedback adopted绑定当前scope固定snapshot与准确run/hash；宿主项目验证passed从原verification.runtime主链落账后反馈。钉住包检查器valid=true/false从原_run_once原ledger提交后反馈；一般工具ok/整轮success不评分，环境/权限/网络/取消错误不降分。同一独立task/job一份质量，人类纠正优先。
- 部署级每日模型预算与原owner每日预算均生效，候选积压20暂停额外总结。模型请求配置沿原enable_self_learning，默认关闭。

## 配置与根手合点

新配置位于原AgentConfig、agent_config.yaml与settings normalize：recommended_limit、maintenance_mode、cool/archive_seconds、include_background、keep_versions、global_daily_limit，均有中文说明。maintenance_mode默认observe；用户显式autonomous才能冷藏/归档/清理。max_skills默认0。

根统一注册 `capability.skill_feedback_tool.SkillFeedbackTool`，仅enable_self_learning启用时注册。core的SkillsService推荐limit provider、SkillLearningRuntime protection/lesson/deployment_root provider需局部手合；不要覆盖第一批core改动。CLI/IM parser/help与transfer独立线共享极小路由，根手合。

17/18由group_callback独占；本片提供registry.import_operations及非committed target_names隐藏规则。store.state_lock已经持quota→state，不得外套quota再进入。transfer必须同一registry事务成功计clone，保持接收稳定owner计数和双端人类授权。

## 边界

当前普通learned Skill通过原skill_search加载SKILL.md；资源读取/执行继续原package授权合同，不能声称本片新增任意learned脚本执行。源授权/群有效成员由原宿主证明，不读跨owner正文给模型。所有正式发布仍需人类确认，自主维护只调状态和推荐。

## 验证

原11文件组合158项通过；新增宿主反馈+原verification runtime12项通过；pack+维护63项通过。最终14文件组合224项通过。未使用线上CI或真实模型作为验收来源。

建议下一步：根先应用after14增量并局部手合core/config/IM共享块，再与17/18联跑，最后跑本地严格gate。可并行只读审查宿主验证归因和配额锁序；勿并行改registry schema，守住人工正式确认、源目标授权与失败不计复刻边界。

## 后续授权审查增量

按根确认移除SkillSummarizeTool原首期localadmin限制；全owner保留enable_self_learning、service.store准确owner、原有效claim/attempt和准确thread/root验证。previous只查当前thread原缓存，工具回执明确先待人类确认。新增普通/群真实owner从手动请求到fake模型候选、跨owner IM拒绝、原owner IM确认、停用/结束claim拒绝回归；模型schema没有human/confirm。宿主反馈再加call.run/attempt一致性，阻止同run旧attempt与换run晚回执给当前运行刷分。

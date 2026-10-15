# Skill transfer 独立安全审查与修复

基于C最终初稿 `/tmp/skill-transfer-s15-s16-20261014.patch` 的稳定生产代码，独立快照 `/tmp/transfer-grant-snapshot`，修复树 `/tmp/transfer-grant-review`。根与C目录未修改。仅交相对快照增量 `/tmp/skill-transfer-grant-review.patch`，不夹带B registry或此前凭据/云变更。

## 已复现问题

1. P1：accept读取source授权后释放来源锁，目标尚未写入时source revoke已成功，accept仍继续导入。原反例 `/tmp/review_transfer_race.py`：`revocation_completed_before_target_write=True`，随后`accept_after_revoke=True`且目标新增1项。
2. P1：导出正文读取按owner/skills根安全完成，但资源读取把嵌套`path.parent`晋升可信根；替换其祖先为symlink后读取owner外内容。原反例 `/tmp/review_transfer_parentlink.py`，bundle实际含`PRIVATE_OUTSIDE_OWNER_MARKER`。
3. 同一no-follow锚点缺陷也存在于导入资源去重、rollback和staging finally：`leaf.is_symlink()`不能发现祖先链接，路径rmtree可移除外部同字节树或外部用户文件。

## 最小修复合同

接收按canonical owner home字符串全局排序，依次持每个owner原quota→state锁，在两锁内重验ref期限、来源当前grant/manifest，并保持到原目标registry committed。撤回仍共用来源原锁：先完成的撤回拒绝接收；已获得双端锁的接收先提交，撤回等待后返回。没有第二grant/claim账，没有复制权限；崩溃沿原目标preparing记录恢复。反向A→B/B→A使用相同锁序。

移除无调用者的旧submit_import_batch；私有`_submit_import_locked`只供双端事务内部调用，保留原source/receiver/hash校验和整批quota/rollback。所有资源读取、去重、回滚验证和清理始终锚定原owner/skills或原store，复用`list_names_beneath`、`read_bytes_beneath`和`remove_tree_beneath`，不新造文件删除器。链接违规失败关闭，保留外部文件。

按根要求给SkillTransferTool补原ToolModelHints：category=capability、default_deferred=True及发现摘要/keywords，仍可tool_search加载；模型只有prepare/status，双端authorize/accept继续人类原slash控制。

## 变更文件与验证

生产4文件：`capability/skill_transfer.py`、`skill_transfer_import.py`、`skill_transfer_manifest.py`、`tooling/skill_transfer_tool.py`。新测试`tests/test_skill_transfer_grant_race.py`。

原`test_skill_transfer.py`19项与新7项合计26通过：两个grant时序、反向并发无死锁、父链接导出拒绝、rollback/staging不删除外部树、deferred发现。原测试继续覆盖群成员证据、错owner/未确认、clone once、memory候选、原工具权限、配额、崩溃恢复与准确去重。Ruff通过，strict code-size hard=0，增量diff check通过。

其余审查：群confirm只允许原scope准确chat的实时成员，个人必须本人/已绑定admin；模型入口无确认动作。memory只取明确approved/promoted候选ID，仍进入目标原待审核账，不复制证据正文/权限。敏感形态扫描不能识别所有秘密的既有边界继续有效；此轮没有新发现可复现的群或memory越权。

## 建议下一步

根在B+C初稿后应用此增量，先跑上述两个测试文件，再跑集成严格gate；可由C并行只读核对其恢复新片与本双端锁协议没有嵌套重入。不要在已持目标state锁时反向调用accept。未提交、推送、部署或访问真实飞书/owner。

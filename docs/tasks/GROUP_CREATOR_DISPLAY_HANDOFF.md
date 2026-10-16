# 群监控创建者姓名刷新交接

## 基本信息

- workstream: 当前群缺名创建者展示刷新
- branch: `codex/feishu-native-images`
- owner: `live_route_attribution`
- date: 2026-10-15
- baseline: `2812638850ff02c091c5bc980019f5c9474a9b30`

## 本线目标

修复 `/schedule` 和模型查询只读姓名索引、从不主动补已有群成员姓名的缺口。名字只用于展示，监控的原创建者、owner、控制及代删权限均不变。

## 实际完成

- 原 `creator_display_rows` 是两个查询入口共用的展示点；只在当前owner为飞书群且确有缺名的飞书creator时，一批最多发起一次成员读取。
- 将provider facts原完整open_id分页抽成 `_complete_group_members`，群授权仍使用原5秒预算；新增 `group_member_snapshot` REST入口给展示使用2秒总预算、最多10页。
- 展示快照只有provider、准确chat、完整成员和可选姓名，没有owner/bot证明，不能充当 `GroupAuthoritySnapshot` 或代删授权。
- 当前群来自canonical owner的 `group_chat_id`，不从job正文、名字、其它群或模型参数取群。只有完整结果内的所需creator非空名字可以补索引；部分分页、未知身份类型和超时不发布部分姓名。
- 网络在原身份索引写锁外；沿原 `remember_provider_display_names` 非阻塞写入，保留canonical/owner/扩展字段，空名不擦，已有名不查网，无变化不写。
- REST或索引失败只记录原安全固定诊断或异常类型，保留姓名未知并继续查询；不新建联系人scope、权限账、姓名缓存或第二身份存储。
- 原 `authorize_departed_creator/current_group_members` 完全未改，展示刷新不能证明离群或扩大控制权。

## 改动文件

- `agent_py_agent/agent/scheduler/creator_access.py`
- `agent_py_agent/agent/adapter/feishu_provider_facts.py`（包含已冻结诊断切片，本次共享分页后的最终SHA覆盖前次代码SHA）
- `agent_py_agent/agent/adapter/feishu_observations.py`
- `agent_py_agent/tests/test_creator_display_refresh.py`（新增）
- `agent_py_agent/tests/test_feishu_bot_observations.py`（前次诊断直接测试保留）
- 本交接文档

## 测试命令和结果

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest \
  agent_py_agent/tests/test_creator_display_refresh.py \
  agent_py_agent/tests/test_feishu_bot_observations.py \
  agent_py_agent/tests/test_home_identity_store.py \
  agent_py_agent/tests/test_schedule_owner_scope.py -q --tb=short
```

结果：174 passed，0 failed/error/skip。原XML为本轮工件 `evidence/creator-display-refresh-focused.xml`。局部Ruff和本线diff检查通过；最后只整理测试import及写交接，未再改变执行语义。

先红证据 `evidence/creator-display-refresh-red.xml`：合成群成员已有姓名，原查询仍显示姓名未知。最终组合覆盖slash/模型同一展示入口、同名不同ID、只刷新缺名creator、准确当前群、私聊/跨provider不查询、完整分页、部分/错误身份拒绝、失败无值诊断、锁外网络、原锁忙不等待，以及原canonical owner字段和真实creator不变。原代删操作矩阵仍通过。

## 影响范围

本次是既有姓名展示能力的接线修复，不增新配置或权限。缺名的当前群查询可能多一次有界原成员API请求；已知姓名、私人空间、其它provider或空列表没有新增网络。刷新失败不会阻断列表或宣称授权成功。

## 需要主线重点复查

- 将新增测试及本交接纳入文件树/测试说明，并合并本次最终SHA与前次provider诊断切片。
- 群授权新语义由另一线负责，不以展示快照替代实时授权事实；官方字段解析没有为此次现网故障做猜测放宽。
- 主线核验真实群列表显示，确认名字与原creator ID成对展示，不能只按同名判断任务归属。

## 剩余风险

- 离群创建者只能展示已有历史索引名字；没有可信历史名字时继续未知，不能向通讯录另查或猜名字。
- 空名、暂态故障或锁忙后仍缺名的后续查询会再次做一次有界刷新；没有新增持久cache或后台重试。
- 未连接真实平台或操作服务，未证明现网群核验故障的唯一根因；此修复只处理已合成证实的展示缺口。

## 后续建议

建议下一步：根将本切片与群授权、图片后半集成后运行统一严格gate，再由根独占做少量真实当前群姓名展示和群控制验收。其他agent可并行只读审阅，不应改本线冻结文件或通过姓名授予权限。

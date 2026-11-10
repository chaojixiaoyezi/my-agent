# 飞书本人及群消息订阅

状态：2026-11-10 本地源码集成，真实应用权限与现场投递尚待验收。

## 范围与授权

私人助手只能为本人和机器人共同所在的群创建消息订阅。创建从准确消息引用或供应商消息编号取得群与发送者事实；机器人和人的身份分别是 `bot/app_id`、`user/open_id`，不以昵称或正文猜测权限。创建和持续收件都核验同一应用、租户、群、本人当前成员身份及机器人在群证明。机器人不在或本人不在的群不在能力范围内。

本人规则只写本人规范 scheduler 账本，不修改群助手的规则或群授权。本人 OAuth 沿原密文账、配置绑定和刷新 CAS；后台读取以原 active job、creator、rule、generation 与 source 的反复核验作为有限宿主授权，不伪造活动 RunParams。群助手规则继续守原群功能授权与创建者权限。

## 事件与有界路由

群内指定人类消息先做活动规则预筛，没有订阅不保存正文或启动模型。机器人事件先通过准确消息 Get 取得 canonical app_id，不能把事件 open_id 当成应用编号。自身机器人及回环仍沿原抑制门。

原 `scheduler_due.sqlite3` 增加 source→owner 查找投影。唯一订阅权威仍是 owner 的规范 job/rule/generation。定义、启停、删除和资产批次按已知 job IDs 增量准备/退休投影；普通运行、pending 与 receipt 不重建来源表。

一次 HTTP 最多处理一个 owner，续点保存在原 GatewayIngress，重启后继续，原 receipt 去重避免重复登记。明确撤权或完整离群证明可拒绝当前 owner 并继续后页；网络故障、坏响应、429 等未知状态保留原重试责任，不当作成功或无订阅。WS 与 webhook 沿原失败 ACK/503 合同重投。

## 投影丢失后的自动恢复

索引带持久 UUID epoch。Count0 沿原 OwnerActivity 分页发现，Count>0 沿原 C/Root 提交门，为已发现 owner 从准确规范账本恢复投影，不创建重型 Agent 或遍历历史目录。只有同一 epoch 完成完整发现轮次且没有错误，才能发表全局完成；中途删库并被另一实例先重建也不能误签完成。

恢复未知时不能把空投影解释为无订阅。已有正面路由可继续登记；完整性尚未确认时保留原 Ingress 责任，避免后续恢复出较小排序的 owner 后被旧游标跳过。

## 默认与验收边界

`feishu_bot_subscriptions_enabled` 与 `feishu_user_subscriptions_enabled` 默认开启，实际飞书权限仍须在对应应用后台申请并配置 `im.message.receive_v1`。`feishu_auth permissions` 提供当前应用的权限页/事件页及分能力 scope。

本地回归覆盖原工具执行上下文、合成供应商事实、真实 HTTP 与持久 Ingress、两 owner 隔离、撤权/停订/离群、暂停交错、索引恢复和无额外 source 热写；这些不能替代实际飞书应用的正式权限、本人 OAuth 和现场投递验收。

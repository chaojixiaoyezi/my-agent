# 群引用附件交接

## 基本信息

- 日期：2026-10-13，父任务统一集成/提交/发布；本线不操作生产或旧安装包。
- 基线：`aa150ba0b46293df762b63ccb7ab3700d8906bd4`；最终新候选 `.20261013.9` 由主线确定。
- 需求：群里先上传文件，再引用该文件消息、明确 @机器人说明任务；不会自动消费所有未提及文件。

## 实际完成

1. 原入站门、认证与 owner 冻结之后，唯一 durable worker 的 prepared 阶段读取明确 parent；控制、私聊、配置关闭和无引用不新增查询。
2. 原 FeishuAdapter 增加准确消息 GET，复用宿主应用 token；CLI 接线原开关，无 ChannelManager 新参数或新状态。
3. 唯一 exact message ID、当前 chat、明确 deleted=false、topic/root 证明先于媒体提取。根文件缺 root/thread 可由当前 root==parent 证明；非根引用必须同锚且无冲突。
4. 父正文和父发送者不复制；原消息自己的媒体也不覆盖。逐引用资源 source_message_id 与本次 current/parent/chat 证明一致，下载资源 API 使用父 ID，当前资源使用当前 ID。
5. 下载仍解析当前 owner 的 media/input 并复用 no-follow/0600 原子文件。原 ReadFileTool 实际跨 owner 读拒绝；管理员发文件不会让当前普通群执行者成为管理员。
6. 全部资源完成才注入路径。父读取/下载不可用返回 quoted_media.v1 的有界结构化事实到原 prompt，最终正常答复由模型生成；原当前资源/文件系统硬失败保留 durable 合同。
7. 原 payload_ready 请求冻结和冷重投不再次 GET。没有新增最近消息查询、引用递归、消息/附件缓存或第二队列。
8. 消息与资源 ID 用标准 URL 参数编码。资源与 token 失败日志只记类型/数值状态，不输出异常正文或秘密；HTTPError 响应关闭且不读取正文。

## 参考与公开接口

- 参考索引及 OpenClaw 的 `extensions/feishu/src/send.ts::getMessageFeishu`、`bot.ts` 引用入口、`media.ts` 资源下载：采用群门后准确查取/资源源 ID 模式，不照搬 `items[0]` 或吞成 null 的边界。
- [飞书查询消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/get)：应用身份需要基础 `im:message` 或 `im:message:readonly` **以及 im:message.group_msg**，只收 `group_at_msg` 不够。返回无 chat_type；用当前可信群事件和返回准确 chat_id 核验，mock 没有虚构该字段。
- [飞书消息资源](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message-resource/get)：真实 message_id/resource key 必须配对，原历史可见性、撤回、保密和外部群等限制继续有效。
- 模块设计：[FEISHU_QUOTED_MEDIA.md](../design/FEISHU_QUOTED_MEDIA.md)。

## 修改范围

- `agent/adapter/feishu_quoted_media.py`：单次核验、短生命周期来源对象及失败事实；无持久状态。
- `agent/adapter/feishu.py`：GET/真实 resource 参数、开关保存、有限安全诊断。
- `agent/adapter/manager.py`：原 prepare 读取与完整集合下载；不改 actor/owner/control/progress 状态源。
- `cli/adapter.py`：宿主原开关传到原 adapter；无新进程或启动参数。
- `tests/test_feishu_quoted_media.py`：合成 API/资源，实际群门、入站记录、owner/文件系统/ReadFileTool 与冷回放。
- gateway 模块 02/04 和本设计/交接同步。YAML/dataclass/normalize、总导航、TESTS 与打包由父任务负责。

## 验证

- 首轮新 26 项 **18 失败、8 原阴性通过**，证据 `/tmp/my-agent-quoted-media-red.xml`；证明原链缺少父读取。
- 首轮实现 25/26，通过链仅剩测试误假设原 safe filename 取 basename；改为真实的目录/无路径逃逸断言，**未修改原 filename 算法**。
- 扩展测试 44/45，仅测试误用 worker.process_once 名字；改用原 run_once，产品不添加别名。
- 新 47 项与原 owner/ingress/Feishu/入站/媒体/manager/TUI preview 八文件 **214/214 通过**，XML `/tmp/my-agent-quoted-media-final-focused.xml`；不与前次计数相加。
- Ruff 直接修改范围、diff check 通过；strict size `/tmp/my-agent-quoted-media-size-final.md` 无 hard。首轮两类 352/351 超限已按原同模块 helper 模式拆出职责，并更新双层注释，没有删注释或规避 baseline。
- 最后 HTTPError 关闭补验新 **47/47**、Ruff/doc sync/diff 通过，XML `/tmp/my-agent-quoted-media-last-focused.xml`。
- 父任务独审发现混合当前附件+父 GET 失败提前 return 跳过原硬门；新增三反例 **3/3 红**（`/tmp/my-agent-quoted-media-mixed-red.xml`）。窄修复复用原当前下载成功后恢复原正文，不提交部分路径；当前资源/owner 失败仍抛。后续最终 focused 及非作者证据由下方追加。

## 最终冻结证据

- mixed 修复后，新 50 + 原 owner/ingress 三文件 **77/77** 通过；Ruff/doc sync/diff 通过，XML `/tmp/my-agent-quoted-media-mixed-final.xml`。
- 最终同源八文件 **217/217** 通过，XML `/tmp/my-agent-quoted-media-final-focused-2.xml`；不与 214/47 等历史切片相加。strict size `/tmp/my-agent-quoted-media-size-final-2.md` 无 hard，两原类均 347 实现行。
- 非作者 N01 四文件 **89/89** 通过，`/tmp/N02_QUOTED_MEDIA_INDEPENDENT_FINAL.xml`。两组真实原接口探针从适配器群门、durable ingress、当前 owner 私有 0600 文件，到 ReadFileTool 跨组 `PATH_CROSS_OWNER_BLOCKED`、原专有 TaskLink 与审批当前引用者，均通过；`/tmp/N02_QUOTED_MEDIA_INDEPENDENT_PROBE.json`。父发送者为合成 admin，恶意身份扩展不进入当前 payload/主体，未写 admin owner 附件。
- 非作者 A 新引用+原媒体+配置 **107/107** 通过，`/tmp/feishu-quoted-media-independent.xml` 与 `/tmp/feishu-quoted-media-independent-report.json`。额外官方 JSON 字符串 body 与 merge 其它返回项探针只读准确 parent、1 次 GET，当前 actor 保持、父原文不进入 prompt；公开字段和 scopes 已交叉核对。
- 所有产品/测试/本片文档冻结，交主线统一 106 文件联合与严格验收。独审没有真实飞书请求或生产利用证据，不能据此确认现场可见性、租户 scopes 或实际文件送达。

## 限制与后续

没有调用真实飞书租户、发送 IM、读取生产 payload/正文/密钥或改现场配置；不能据本地测试确认应用 scopes 与现场引用消息实际形状。飞书权限不足需要应用侧授权，软件无法绕过。未增加大文件 Range、合并转发子资源追溯、私聊引用、云文档新读写接口。prepared 阶段崩溃可再次读取同一准确父消息；payload_ready 后沿冻请求恢复。

建议下一步：先由非作者复核 exact parent/chat/topic/当前 actor 与 owner、权限/失败事实边界，再由父任务统一严格 gate、原包构建和隔离验包。实际租户引用文件验收应在明确群授权下单独进行，不发送其它群/私聊数据；非作者审查可并行于静态包准备，不并发修改本片源文件。

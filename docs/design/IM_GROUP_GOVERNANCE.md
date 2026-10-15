# 群能力授权与机器人订阅

状态：2026-10-15 实施与集成验收中，尚未发行本追加。此前 `.20261015.1` 是已交付的卡片诊断版本，不包含本追加。

## 谁能创建和配置

群内新建、扩展、恢复、手动运行监控，以及持久模型、决策、确认模式和人格配置，默认需要显式群能力授权。
当前群主或真实机器人入群事件所证明、且仍在群内的邀请人可以授予；群角色不等于本机管理员。
旧群没有可信邀请记录时，由当前群主重新明确授权，不能猜首位发言人或相信模型自报身份。

人工控制示例：

```text
/group
/group allow monitors.create
/group allow assistant.configure member:<准确open_id>
/group allow monitors.create all-current
/group revoke <grant_id>
```

缺省只授予操作者本人。all-current 冻结授予时的真人成员，新入群成员不会自动得到权限。
官方完整当前成员、群主与机器人在群状态必须可核验；缺 scope、分页不完整、策略损坏时明确拒绝。
只有认证人工 slash 入口能授予，模型工具不能自行批准。自然语言由模型说明所需人工动作，不参与机器授权。
授权块在原 owner tool_policy.json，使用原锁、revision/CAS；不建第二成员权限库。
新工作与实际配置写入均核原 HostChannelActor，原策略锁覆盖同步副作用。
嵌套租约只供同线程、同 async task、同 actor 的活事务，复制上下文到子任务不能借权。
脚本、文件工具和普通用户配置仍不能改受管策略。full-access 不隐式授予群能力。
离群/重新加入或机器人退群使相关旧授权失效；等时间戳顺序不明时失败关闭，不让退休邀请人重放复活。
离线期间没有连续事件证明时，不声称可重建完整历史；当前群主可重新授权建立基线。
撤销只限制后续新建、扩展、恢复、手动运行及配置，不暗中停止已有监控或工作。
查询、暂停、取消、删除、拒绝和死亡执行核对继续原创建者与离群代清理规则，保证止损通路可用。
部署开关 im_group_authorization_required 默认 true，只读共享 capability 配置；群局部设置不能关闭。
它不授予文件、网络、云资源、插件或跨 owner 权限。

## 指定机器人的消息订阅

沿原 scheduler job/rule/store/run/Wake/额度与投递。用户引用当前群目标机器人的真实消息并 @ 助手说明条件。
schedule 工具使用 schedule_kind=event_source、event_source.source_message_refs 原样选择当前请求引用；
多规则使用每条 rules[].schedule.kind=event_source 和相同公开字段。也可显式提供已知原供应商
source_message_ids（om_ 编号），两种输入只能选一种，诊断短引用不得冒充供应商编号。
公开 schema 不接受 app_id、tenant_key、sender_ids、proofs；宿主 Get 原消息核验发送应用后，
才转换为唯一 canonical source 并交原规则/store 校验，不能把持久身份结构直接交模型填写。
引用上下文沿原TaskLink或委派SessionTask唯一输入保存；后台主根、子孙在同owner/原HostActor/root及当前attempt核验后读取，
不从模型attrs/context_bundle恢复。新独立请求缺引用失败关闭；官方Get和当前群人工授权仍在每次订阅登记时执行。
可选 min_interval_seconds 默认60（60～86400）、max_batch_events 默认16（1～16）。
不能用昵称、正文、其它群或自报应用编号制造来源身份；event 的 bot/open_id 与 Get 的 app/app_id 不隐式互换。
自然语言条件只写规则 prompt，由 LLM 研判；路由只读准确结构身份，外部告警不构成指令或授权。
普通人无 @ 消息保持既有门；自己、未知或未订阅 bot 不产生普通 /ask，不创建真人 actor 或交互进度占位。
观察沿原认证 Ingress 重投，经宿主凭据保护的 /channel-observations 进入原群 owner。
输入不能提供人类用户、owner、path 代替宿主解析。接收应用、租户、群、来源应用和原消息共同去重。
相同身份冲突、坏订阅、容量满必须明确失败/背压，不能消费后静默丢弃；批次原引用完整保留。
模型正文视图有字节上界与截断事实，截断不能冒充已读。
用 /schedule 查看，pause/delete 取消订阅；cancel 只停当前执行并保留未来订阅，保持定时任务语义一致。
source generation 不随正常 job.version 变化；暂停、删除、取消、修改使旧代失效。
已退休代次未开工采样与未开始投递须被拦下，平台已接受或在途外发不能声称撤回。
真正模型及投递入口通过原job事件状态的有界开始租约裁决：控制先提交则拒绝新调用，begin CAS先成功则视为已准入在途。
不持store锁等待慢I/O，不承诺文件CAS与物理TCP字节同时发生；在途结果与取消继续沿原账核对，不复制副作用结果账。
来源网络预检在群策略锁外完成，最终写入重核授权，撤销期间不能借预检旧结果创建任务。
可证的本应用及引用因果回环拒绝；未知外部因果依靠原配额/有限批次，不能承诺检测一切机器人循环。
feishu_bot_subscriptions_enabled 默认 true，只读部署配置；无订阅不轮询历史、不增加模型调用。
接收其它机器人消息还须飞书对应官方 scope，普通 group_at 权限不包含它；不自动申请新 scope。
服务器仍只用 open.feishu.cn:443、msg-frontier.feishu.cn:443，不增加第三方运行依赖。
合成合同与离线 SDK 通过不代替真实租户验收，具体状态由同版发行证据记录。

## 2026-10-15 引用与规则公开输入修复（本地合同验收通过，待同版发行）

`.20261015.3` 的真实引用请求失败在副作用前：模型传入诊断短引用，并在嵌套规则使用事件计划，
旧规则表面却暴露持久 source 身份，宿主尚未执行原消息证明即拒绝；没有创建订阅。
本地修复统一单计划与 1..N 规则的公开引用输入。准确父消息 GET 的结果在原入站 payload_ready
内冻结 current-message/chat/ref→真实 ID 绑定，经认证 Gateway metadata 交本请求专有 RunParams。
不从 prompt、模型 attrs、群昵称或历史消息恢复绑定，不新建消息/订阅/授权账。
引用选择仍须同 owner/actor/entry_request_id，平台 Get 继续证明同群、接收应用与准确其他机器人；
任何来源读取得到的旧结果不能替代最后的群授权锁/CAS，撤销期间仍不创建任务。

直接前台请求只消费本次认证引用；同Goal后续Gateway有/无新引用都不会改写原根快照或借用旧引用。
`/delegate enable` 已沿原SessionTask唯一输入快照和TaskLink精确引用接通；后台主根及子孙只读恢复前，
核原HostActor、owner/thread/root、当前RuntimeDB attempt及canonical父子记录，业务role（包括main）不作为主子身份。
Gateway请求文件回收后仍可从准确原输入恢复；不把引用复制进孩子attrs，也不另建引用账本。
本地12文件集中247项通过、零失败/跳过；其后补齐非空RuntimeDB父引用缺记录的拒绝防线，最终上下文25项定向复核。
上下文测试覆盖主根/子孙、原Store重开恢复、同Goal新请求、
错误原输入引用/绑定回合/终态、伪造属性、跨owner、陈旧attempt以及main角色的伪root/parent。
单计划及1..N规则、真实工具与fake模型回放沿同一公开schema→原官方Get替身→canonical规则持久化。
这些合成合同与回放不代表生产部署或真实订阅成功；`.3`故障证据保留，真实租户须在新发行后独立重验。

建议下一步：根统一严格gate、提交及新发行，再由独占测试入口完成普通群成员、授权成员与真实引用订阅验收；可并行只读审计，主线单点部署，不自动扩大平台scope。

## 2026-10-15 话题来源群授权修复（本地合同验收，待新发行）

`.20261015.4` 的真实引用订阅已进入正确公开schema及原来源证明，但授权门将完整话题会话键与基础群编号强等，
在副作用前以GROUP_AUTHORIZATION_INVALID拒绝，已有grant尚未参与判定；该失败不算订阅成功，原发行证据保留。

`adapter/protocol.parse_feishu_conversation_address`统一解析基础群及固定`:thread:`地址结构。
话题锚为有界安全字符的opaque标识，不封闭枚举前缀、不当作API消息编号；空段、额外分隔、空白及路径字符拒绝。
归群只比较完整解析后的基础群，不按starts-with或随意拆分放宽；权限仍来自准确原ThreadStore、HostActor、owner及当前人工授权。
主群和已核同群话题可共用原群grant；原HostActor.conversation_id、source_thread、TaskLink及委派/子孙身份始终保留完整话题。
同基础群的另一个topic也不能替换原绑定，跨群/坏地址/陈旧attempt或坏原谱系仍拒绝。

原bot证明继续从group owner取准确基础群，官方Get的消息编号、发送应用、群及人工授权合同不变。
最终群通知仅在原结构化group上下文中投影为基础群运输地址；不把内部topic键放入receive_id，不增加API、scope或网络请求。
原/model群卡同样核canonical group owner后投基础群，不能因缺channel_chat_type元数据改为私聊。
私人表单回调只从签名准确thread的原origin唯一同channel绑定恢复完整topic，再复核actor与原配置grant；不猜latest binding或消费模型attrs。
源owner不符、歧义绑定、错误actor或撤销后的保存均拒绝；取消继续原止损入口，不以新增配置权为条件。

测试沿原/model、签名控制、私人表单保存/撤销、真实SessionTask/G1/子孙及DeliveryService/适配器路径，仅替换平台或模型传输。
合成通过不能外推真实飞书订阅/卡片成功；新版本仍须统一严格gate、发行及独占真实验收。逻辑topic与物理basechat分离参考OpenClaw的targets/send-target/thread-bindings职责边界，本地采用严格单一parser，不复制宽松前缀权限。

建议下一步：根统一新源码gate及发行后重验原普通授权成员引用订阅；只读审计可并行，真实服务、launcher和部署保持根单点。

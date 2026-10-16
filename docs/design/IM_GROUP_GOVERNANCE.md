# 群能力授权与机器人订阅

状态：2026-10-15 用户收敛第一期：额外群授权只管监控，使用一个中文入口；本轮源码与联合验收中，尚未发布新用法。后文历史阶段记录保留原时点。

## 群监控授权：第一期用法

```text
@机器人 /group 监控 @某人
@机器人 /group 监控 全群
@机器人 /group
```

只有当前群主，或被真实机器人入群事件证明且仍在群内的邀请人，可以人工授权。
@目标必须由原飞书text mentions或post at规范成准确open_id，不能按显示名或模型正文猜身份。
控制入口仍核验目标实时在本群；普通成员不能自行授权，引用别人的话不能冒充授权人。

授权仅控制群内监控的新建、扩展、恢复及手动运行。普通聊天、收发文件和其它原有功能不受这层额外授权限制。
`assistant.configure`退出本层能力；持久配置仍保留原owner/admin、签名卡与准确HostActor/目标身份门，不取得跨群权限。
查询、暂停、取消、删除沿原创建者和离群代清理规则，监控授权不会让B删除仍在群的A所创建的监控。
撤销仍可用`/group revoke <授权编号>`，只限制后续新工作，不暗中停掉既有监控。

“全群”是一条明确`scope=group`的原策略grant，覆盖当前及之后加入的实际成员；新成员不用逐个重新授权。
个人授权仍只覆盖准确成员。旧`all-current`是已发布成员快照，继续按原义读取并明确标注，不静默升级到未来成员。
旧English入口有已发布依赖，保留读取/控制但不再作为新用户主用法；`allow monitors.create`无目标按新的明确全群语义创建。
旧`assistant.configure`grant留原账、不再参与额外授权或展示，可按准确编号撤销。

唯一权限账仍是owner的`tool_policy.json`，采用原锁、revision/CAS、应用/租户/群代次与当前成员核验。
full-access不隐式授予监控能力，机器人退群重入、应用或租户变化和主动撤销不会借旧授权复活。
部署开关`im_group_authorization_required`仍默认true且只读共享配置，普通群配置不能自行关闭。
没有第二权限库，不通过自然语言或模型工具授予，不扩大文件、网络、云资源、插件或跨owner权限。

## 群信息失败与姓名展示

`GROUP_AUTHORITY_UNAVAILABLE`表示群信息核验失败，不能当成“重启丢授权”或一概要求用户重新授权。
原provider固定原因码、数值平台错误码和查询阶段进入控制回执与安全诊断，不包含异常正文、token、响应载荷或成员名单。
明确缺scope才提示核相应群读取权限；网络、分页未完成、身份缺失各保留准确失败，不绕过完整性硬门。
重启后的真实REST factory/owner冷实例不依赖WS热身；测试群真实chat/bot/member读取均成功，不能外推现网故障根因。

`/schedule`及模型工具共用的创建者展示入口，在当前飞书群确有缺名creator时，至多一次读取原完整成员姓名。
只向原身份索引补同一ID的显示名，不改owner/canonical/creator，不新建身份映射；网络在索引锁外，失败保留未知且不阻断查询。
姓名不是权限，不从同名、退群、分页缺失或索引失败推断代删授权。私人、跨provider、无监控和已知姓名不新增群查询。

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


## 创建者姓名展示（2026-10-15，源码已修复，待新版验收）

`.20261015.5`真实指定bot订阅已完成原消息证明、单事件运行和本群投递，随后由创建者删除；创建者ID正确，但界面仍为“姓名未知”。根用同一正式群成员API只读确认可用name，原provider身份索引却不存在。缺口属于展示资料接线，不属于创建者或群授权判断。

仅沿原认证成员分页保留`member_id_type=open_id`行的可选name，全部分页成功后才返回`member_display_names`。`current_group_authority`完成准确provider/chat/完整成员校验后，将名单内名字交原`identity_store.remember_provider_display_names`；不从模型、显示名、最近发言人或其它群推断主体，不增加contact查询、scope、平台连接或配置开关。

原provider索引同时服务关联解析，不能当作纯展示缓存随意覆盖。更新在其原文件锁内读取原字节、追加确实变化的展示快照并用原私有原子writer保存，原owner_kind/owner_id/canonical_user_id/owner_home及扩展字段完整保留；缺索引的新行仅等价原provider默认user身份，不创建owner目录或管理员关联。空名不清旧名；无可用名字在读索引前直接结束，相同名字不追加、不重写。并发更新沿同一锁排序；展示索引锁忙用原非阻塞锁明确让出，记录异常类型后继续原授权，后续原认证快照可再补；不创建后台重试。坏索引不重建。

普通用户与群创建者仍由`creator_display_rows`读取同一索引，只显示名字加准确provider/actor ID；同名、跨群、名称“管理员”都不授予权限。`creator`、HostActor、成员集、原tool_policy、租约及最终写门不改变。展示索引预期IO/结构故障仅记录固定事件与异常类型，不记录名字、ID、平台值或异常正文，也不吞原provider/授权校验错误。缺名继续原“姓名未知”，无需额外平台探测。

5个直接相关文件214项通过、零失败/错误/跳过；三层红测保留，最终严格gate和新版真实姓名展示由根执行。已有姓名记录如何长期保留/清理沿原身份索引合同，本片不新建第二索引。交接见[姓名追加](../tasks/IM_CREDENTIAL_LIVE_ENTRY_HANDOFF.md#创建者姓名投影追加2026-10-15)。

## 完整成员查询安全边界（2026-10-15，本地修复，待新版验收）

群授权、创建者缺名刷新和离群代清理共用 `feishu_provider_facts._complete_group_members` 的唯一分页及完整性判断。
官方响应若明确 `trigger_security_conf_limit=true`，或该字段有非bool非空值，不能称为完整成员；
`security_conf_limit` 非零只表示安全配置查询人数上限，不能单独认定本次已触发。
可选 `member_total` 缺失/空值保持原可选合同；有值必须准确非负int、各页稳定且最终与累计唯一ID数相同。
重复ID（页内/跨页）、成员类型错误、坏游标、超预算或中途总数变化均拒绝整组，不发布部分姓名或负面离群证明。
这基于[官方Go SDK的成员响应字段说明](https://github.com/larksuite/oapi-sdk-go/blob/v3_main/service/im/v1/model.go#L11623-L11634)，不代表已证明用户现网故障由安全限制触发。

群授权仍保留原5秒及十页；展示与原代删仍保留2秒及十页。代删仅将原有界HTTP页读取器交共用分页器，
保留实际 `read(1048577)` 并明确拒绝超过1MiB响应，不增加模型可控URL/token入口或另存身份。
当前owner、actor、scope、创建者及pause/cancel/delete授权门不变，成员查询失败沿原 `SCHEDULER_MEMBERSHIP_UNAVAILABLE` 拒绝。
受限或无法证明完整不能被当作“创建者已离群”；本次不增加查询权限、自动重试或放宽未知成员类型。

# Curator 消息定位的有界续办

状态：独立源码候选；真实服务未修改，未把现场CPU峰签为已解决。

## 原问题与范围

真实栈命中 `memory-curator_0 → after_report → _scan_cursor → json.loads`。在稳定十万行热转录中，8KiB历史ID Bloom趋于饱和，新的页末ID不能证明首次出现，因此连续换游标会再次解析旧前缀。原全局定位缓存和同次pass提示都不能消除这个安全退化。

同步 `MessageStore.after_report`、用户历史接口和消息JSONL/冷热清单保持原行为。本片仅为后台新增 `after_slice_report`，由原 `TranscriptReader` 和原解析规则提供结构化页结果；不是第二份消息或线程权威。

## 读取与来源证明

- `partial` 表示尚无可提交消息页，不是EOF；`has_more`表示已有完整可提交消息但片预算用完，未读部分仍有义务。只有真实EOF返回无错误、非partial空页。
- 定位续点仅含文件完整指纹、实际变更代次、逻辑/热偏移、8KiB已见过滤器及最多64KiB尾锚摘要，不保存消息正文、FD或执行权限。与原定位共用有界LRU，丢失/重启只会保守重定位。
- 跨拍缓存首次发布和复用均要求mtime、ctime及真实变更时钟离开原粗时间保护窗口。热文件替换、截断或指纹变化重启，近期/持续变化来源保持partial；不存在或坏行不会推进消费ID。
- 冷ID沿原HAMT定位、原段SHA和原JSON解析。跨拍只保留原目标段与最后锚段的同FD观察，复用时重新核原索引地址、实际打开FD代次及父链；冷目录变化撤销续点。删段/损坏仍未知，不能用热尾冒充完整。
- `read_bytes_snapshot_beneath`/`inspect_file_snapshot_beneath`复用原严格父链/叶核验，返回同一次打开FD的元数据，不用前后路径stat代替读取身份。便携分支保留原拒链/单链接门并显式使用O_BINARY。Windows另读同一借用FD的FILE_BASIC_INFO.ChangeTime，不拿可能是创建时间的st_ctime独自证明旧内容未变；不可核验不缓存。

## 预算、发现与公平

宿主原`retention_scan_slice_entries/bytes/ms`由`MemoryCuratorConfig.from_agent_config`原值透传；配置更小不能由batch大小抬高。同一次Curator消息发现、metadata读取和各线程读片共用总时间/字节预算，子片只借剩余量。目录发现沿原原生/便携目录页；普通服务缺少有界能力明确失败，不回退全线程list。

线程ID先分页发现，正文经原ThreadStore同FD快照读口加载。小窗口保持updated_at/ID顺序；跨页沿目录续点公平推进。元数据恰好占满字节时，只缓存单个不超过8KiB的来源资格投影和FD观察，不缓存title、summary或整份metadata；下拍先复核并服务这个已付费候选。前驱发表条件与同步load共用原判定，前驱待核时不开放学习。易变或不能保存观察的来源延期到下轮，保持partial，不永久占住后续线程。

原冷段64MiB上限作为单个受支持原子记录的唯一上限。整拍尚未处理其它消息时只可领取一次原子例外，子线程不各领一份；完成即让出。一次JSON解码/系统读取不能被合作deadline抢占。超界热行不解析、不存原文，明确partial原因并让其它线程推进；不称全部输入均有25ms硬时延。

## Curator提交

定位partial无消息时返回busy并沿原lease停止/释放，保留原pending原因和消费ID，不写空成功或日终完成。其它来源的完整有效页可按原事务提交；通过原reason generation保留未完义务，日终完成标记延后。原未知执行、租约、失败、候选CAS及完整消息引用核验不变。

## 验收与局限

覆盖十万行定位分片、同target后继片不重扫、重复ID首匹配、坏行/空白、替换/截断/粗时间、冷热大目标、冷段删除、原始字节/同FD/父链、metadata等于预算、entries=1、宿主配置与缺有界能力不全扫。计数和准确平台结果见仓外交接。

首次target总扫描仍是O(前缀)。真实换page-end ID、Bloom饱和、来源变化或缓存淘汰仍可能从头定位；分片还增加打开/元数据核验成本。这里保证单次常规消息扫描有界并可续办，不声称永久消除历史总成本、跨重启不重扫、5000用户容量或真实CPU峰全部下降。审计输入收集另由原路径负责，本片不把消息预算外推为整个LLM/审计运行的硬deadline。

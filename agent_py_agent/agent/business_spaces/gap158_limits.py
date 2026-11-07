"""业务空间扩展的共享数值上界；仅提供字面量，不导入服务或产生运行副作用。"""

# 模型网关诊断调用最多生成的输出令牌数。
LLM_GATEWAY_TEST_MAX_OUTPUT_TOKENS = 16
# 单条模型调用元数据允许保存的最大字节数。
LLM_RECORDS_METADATA_MAX_BYTES = 4096
# 模型调用聚合查询默认覆盖的时间窗口秒数。
LLM_RECORDS_AGGREGATE_DEFAULT_WINDOW_SECONDS = 604800
# 模型调用聚合查询允许的最大时间窗口秒数。
LLM_RECORDS_AGGREGATE_MAX_WINDOW_SECONDS = 2678400
# 一次模型调用聚合查询最多返回的模型数量。
LLM_RECORDS_AGGREGATE_MAX_MODELS = 200
# 模型调用记录每批最多清理的旧记录行数。
LLM_RECORDS_PRUNE_BATCH_ROWS = 500
# 单次兼容模型请求允许包含的最大消息条数。
OPENAI_COMPAT_MAX_MESSAGES = 512
# 兼容模型流式输出每片最多承载的字符数。
OPENAI_COMPAT_STREAM_CHUNK_CHARS = 2048
# 每个本人空间最多保存的策略规则条数。
POLICY_RULES_MAX_PER_OWNER = 500
# 每个本人空间最多保留的待审批条目数。
POLICY_PENDING_MAX_PER_OWNER = 200
# 策略历史日志每个分段允许占用的最大字节数。
POLICY_HISTORY_SEGMENT_BYTES = 4194304
# 策略历史日志最多保留的分段数量。
POLICY_HISTORY_MAX_SEGMENTS = 8
# 策略历史单次查询最多读取的记录条数。
POLICY_HISTORY_SCAN_MAX_RECORDS = 5000
# 策略任务摘要单次最多读取的历史记录条数。
POLICY_TASK_SUMMARY_SCAN_RECORDS = 2000
# 每个本人空间最多保存的目标范围规则条数。
SCOPE_RULES_MAX_PER_OWNER = 500
# 单个任务最多叠加的目标范围规则条数。
SCOPE_TASK_RULES_MAX_PER_TASK = 100
# 策略判断任务共享的工作线程数量。
POLICY_JUDGE_WORKERS = 2
# 单个会话最多创建的旁问数量。
SIDE_QUESTIONS_MAX_PER_CONVERSATION = 50
# 每个本人空间最多同时执行的旁问数量。
SIDE_QUESTIONS_MAX_CONCURRENT_PER_OWNER = 2
# 旁问执行器最多使用的工作线程数量。
SIDE_QUESTION_WORKERS = 4
# 每个旁问最多保留的流式事件条数。
SIDE_QUESTION_EVENT_RING = 256
# 旁问最多携带的原会话上下文消息条数。
SIDE_QUESTION_CONTEXT_MAX_MESSAGES = 40
# 旁问上下文最多携带的字符数。
SIDE_QUESTION_CONTEXT_MAX_CHARS = 120000
# 每个角色最多保留的提示词历史版本条数。
AGENT_PROMPT_HISTORY_MAX_PER_ROLE = 20
# 每个角色最多登记的提示词触发规则条数。
PROMPT_TRIGGERS_MAX_PER_ROLE = 32
# 技能归档条目允许的最大解压大小与压缩大小倍数。
SKILL_ZIP_MAX_COMPRESSION_RATIO = 100
# 技能归档中单个文件允许的最大字节数。
SKILL_PACKAGE_FILE_MAX_BYTES = 1048576
# 每个本人空间最多登记的自定义工具数量。
CUSTOM_TOOLS_MAX_PER_OWNER = 50
# 自定义工具探测允许的最大执行秒数。
CUSTOM_TOOLS_PROBE_TIMEOUT_SECONDS = 5
# 每个本人空间最多配置的通知渠道数量。
NOTIFY_CHANNELS_MAX_PER_OWNER = 50
# 通知投递队列每批最多领取的条目数。
NOTIFY_DRAIN_BATCH = 20
# 通知投递执行器共享的工作线程数量。
NOTIFY_WORKERS = 2
# 单次运维更新最多保留的诊断日志条数。
OPS_UPDATE_LOG_RING = 2000
# 每个本人空间最多登记的文件观察订阅数量。
FS_WATCH_MAX_SUBSCRIPTIONS_PER_OWNER = 8
# 文件观察每次扫描最多检查的目录条目数。
FS_WATCH_MAX_ENTRIES_PER_SCAN = 5000
# 文件观察两次扫描之间的最小间隔秒数。
FS_WATCH_MIN_SCAN_INTERVAL_SECONDS = 1.0
# 每个文件观察订阅最多保留的事件条数。
FS_WATCH_EVENT_RING = 1024
# 文件观察订阅闲置后自动过期的秒数。
FS_WATCH_IDLE_EXPIRY_SECONDS = 900
# 文件搜索允许读取的单文件最大字节数。
FS_SEARCH_MAX_FILE_BYTES = 1048576
# 文件搜索允许同时运行的最大查询数量。
FS_SEARCH_MAX_CONCURRENT = 2
# 每个本人空间最多登记的版本库观察订阅数量。
SCM_MAX_SUBSCRIPTIONS_PER_OWNER = 8
# 版本库状态查询最多返回的条目数。
SCM_MAX_STATUS_ENTRIES = 2000
# 版本库历史查询最多返回的提交条数。
SCM_MAX_LOG_ENTRIES = 50
# 单次版本库操作最多提交的路径条数。
SCM_MAX_PATHS_PER_ACTION = 200
# 版本库命令执行允许的最大等待秒数。
SCM_GIT_TIMEOUT_SECONDS = 15
# 版本库观察两次刷新之间的最小间隔秒数。
SCM_MIN_REFRESH_INTERVAL_SECONDS = 1.0
# 每个本人空间最多同时保留的外部代理桥接会话数量。
ACP_MAX_SESSIONS_PER_OWNER = 4
# 每个外部代理桥接会话最多保留的事件条数。
ACP_EVENT_RING = 2048
# 外部代理桥接探测允许的最大等待秒数。
ACP_PROBE_TIMEOUT_SECONDS = 15
# 外部代理桥接结束时允许的正常退出宽限秒数。
ACP_TERMINATE_GRACE_SECONDS = 3.0
# 每个本人空间最多同时保留的浏览器终端会话数量。
WEB_TERMINAL_MAX_SESSIONS_PER_OWNER = 4
# 浏览器终端访问授权的有效秒数。
WEB_TERMINAL_GRANT_TTL_SECONDS = 120
# 浏览器终端单次读取最多返回的字节数。
WEB_TERMINAL_READ_MAX_BYTES = 65536
# 文档成员在线状态的有效秒数。
PRESENCE_TTL_SECONDS = 30
# 单个文档最多保留的在线成员数量。
PRESENCE_MAX_MEMBERS_PER_DOC = 50
# 单份协作文档状态允许占用的最大字节数。
DOC_STATE_MAX_BYTES = 262144

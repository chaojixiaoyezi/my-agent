# C3-L LocalStore 交接（候选，未接入口）

- workstream：F3 memory C3-L；准确前像B37完整4829，七生产文件与F54初始SHA相同。
- 执行范围：独占候选source；不提交、不推送、不部署、不写根工作树。
- 目标：闭合原LocalStore目录/连接/schema/正文/SQL/事件/FTS末写，复用原global scope，保旧默认。

实际完成仅七生产路径：local_storage store/schema/records/maintenance/events与common global_write_commit/json_io。C策略不存source/新上下文；C独立0等待连接；原body/event/事务相序，FTS完整typed raw/正文代次CAS；shared jsonio一次replace精确组合独立57候选。无新第三方依赖和用户配置开关。

测试与函数级AST、精确命令/XML、全map、正反patch在本片独立工件REPORT记录，不能将相邻与新增多轮相加当覆盖。测试仅真实临时SQLite/文件与fake外层末门，未运行真实HTTP/模型/服务/native或D24/P6。旧默认和C错误合同均验证；FTS实际门持时只对样本成立，不推成恒定上界。

重点复查：缺scope是否拒绝、旧W/Count0无新guard；C未借F1/batch；body→SQL、event SQL→JSONL→commit与失败partial；CAS不符DELETE前拒绝；source错误不被清理覆盖。门外prepare连接撤权后的必要原FD close仍执行，不授新DML。

后继仍需C1工厂透传、C2真实holder/client source和C3-J candidate/formal/index bridge/vector/ops等接线；本片不改bridge，不声明cold search/remember/candidates ready。

建议下一步：非作者先审本片，再单作者串行后继source/jsonl接线。其他agent可并行只读核事务/测试，不并写七路径或根工作树。

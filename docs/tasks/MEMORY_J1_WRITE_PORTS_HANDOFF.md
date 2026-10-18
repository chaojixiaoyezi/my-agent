# J1 本地写叶交接

## 基本信息

- workstream：F3_MEMORY_J1_IMPLEMENT_65；独立仓外 source，未发布/部署。
- 输入：冻结 C3-L 60 全树；67 三路径异常纯搬迁单列前置。
- 所有权：本片六生产路径；JSONL/builder/bridge 由后继单作者串行处理。

## 实际完成

候选、原 quota/policy 锁出生、ops 原 capped 写入以及正式删除前工具账本 SQL 脱敏均增加可选 coordinator_io=False。C 只保存原对象与 IO 策略；原 global scope 在每次 borrow 安装真实 source.perform，不把来源放进容器。详细方法、事务顺序和 partial 见[合同](../design/MEMORY_J1_WRITE_PORTS.md)。

## 验证

两新增测试文件覆盖 48 项；原 promotion/file-permissions/hardening/quota 回归 78 项通过（未运行三个 Linux admission native 用例）。136 原函数默认规范化 AST 等价。最终组合 XML、准确命令、全树前后 map、静态结果和正反 patch 重建记录随仓外冻结产物交付；不以本交接文字代替机器证据。早期测试夹具缺 lease_expires_at / add(role, content) 两处失败保留原日志，没有改产品绕过。

## 需要主线重点复查

- 已有 67 不重复套前置 Gateway 整文件，只合本片六生产路径 patch，组合根核最新接口。
- quota 对象复用时在实际同 owner borrow 互斥区刷新原 max_bytes/policy_available；锁/scan/准入算法保持。
- JSONL 删除与 condense 不漏 formal 前 ledger 脱敏；原 bridge 宽 catch 必须按真实 callee 同对象透传后再进入付费后续。
- ledger 全 raw CAS 和 SQL O(N) 本地事务可随数据增长；当前样本耗时不是上界。

## 后续建议

优先由 JSONL/builder 唯一作者串行接可选参数与真实 catch，然后根做与 60/67 的组合 focused 验收；其他 agent 可只读评审 vector/text cache/lessons 后继叶子，不能并写同路径或把尚未闭合的 handler 宣称 ready。

# Delivery Closeout

本文只描述当前主链路的验收和恢复建议。

## Structured Sources

- delivery closeout 的机器判断只读结构化合同、artifact refs、工具记录、文件事实和验证结果。
- `staging_contract.source_json_ref` 是主 staged checkpoint；如果 `collection_contract.source_json_ref`
  指向另一份 JSON，它也是结构化 collection source。
- 两类 JSON source 只要存在，都要走 checkpoint quality 检查，再生成 recovery action；不能只因为
  staging source 缺失，就忽略 collection source 里已经写下的 rows、columns 和 completion evidence。
- collection source 的缺列、数量不足、占位值、证据缺失等问题都应生成结构化 recovery action，
  供模型修复真实 checkpoint 后重新验收。
- 错误正文和普通 summary 只作为审计说明；不能从自然语言文本反推出验收状态。

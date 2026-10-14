# IM 表格保护元数据合同

2026-10-14，R03 本地修复；未接触真实租户文档，未部署。

## 问题与边界

原保护门把所有缺少 protectedRange 的 metainfo 响应都判为未知。用户或群已经在资源级拥有 edit 时，合法空集合的省略仍会误挡写入。修复继续先核验当前准确 owner/主体的资源 edit/full_access，再用同一个 adapter/认证身份实时只读核验保护；应用编辑权限不代表用户或群有权。

## 官方证据

[飞书官方 Go SDK v1.1.48 model.go](https://github.com/larksuite/oapi-sdk-go/blob/v1.1.48/service/sheets/v2/model.go#L412) 的 Sheet.ProtectedRange 是带 omitempty 的集合；Sheet 还定义 sheetId、rowCount、columnCount、blockInfo，MetainfoProperties 定义 sheetCount，SpreadsheetsMetainfoResult 定义 properties 与 sheets。这是省略集合合法性的直接 schema 依据；不是对生产响应完整性的保证。

[同版本官方 api.go](https://github.com/larksuite/oapi-sdk-go/blob/v1.1.48/service/sheets/v2/api.go#L454) 定义 metainfo 的 GET 路径和 SetExtFields；允许 tenant/user token。当前实现仍发 extFields=protectedRange，不使用未申请扩展字段的元数据推断保护。

读取于 2026-10-14，完整源文件 SHA256：

- model.go：59e646376df091770172282bc32a4730f22301d9f0371891fe7fcc53e571ee94
- api.go：74ad9047420d680672548ebc21d2f938d32c62d073eb64090beba5b96a455294

[现代官方 v3 Sheet schema](https://github.com/larksuite/oapi-sdk-go/blob/v3_main/service/sheets/v3/model.go#L4488) 没有在 GetSheet 返回的 Sheet 上提供保护集合，因此不能用单独 v3 GetSheet 成功替代旧接口的保护检查。没有新增 SDK 依赖；仅读公开结构，SDK 采用 MIT 许可。

## 判定规则

- 明确 protectedRange=[] 保持原无保护行为。
- 省略 protectedRange 仅在同扩展字段响应里，全部 sheetId 非空且唯一、准确目标唯一且非 blockInfo、properties.sheetCount 等于清单数、目标 rowCount/columnCount 是正整数且无矛盾保护指标时，归为空集合。
- 裸 sheetId 重复返回仍是 protection_metadata_incomplete；错误数组、重复编号、跨工作表范围和矛盾字段归 protection_metadata_invalid；目标缺失或非表格归 protection_target_missing。公开 reason 来自固定结构字段，不返回原始 payload 或秘密。
- 有保护范围继续取每批最多五个准确 ID，核验详情、A1 相交和准确 openId 编辑者。群不能借群成员或应用的保护编辑权。v2 users schema 不能推导现代 API chats 的语义，不新增暗中跨协议兼容。
- 读取 API 被拒绝保留原 HTTP/provider code/reason；预检失败不发业务写入。写后沿原工具同身份/后端做一次机械读回，读取失败不撤销已接受写入，不重新追加；真实用户成功尚未验。

## 写入读回

原 FeishuCloudTool 写入成功后，用原 adapter 与原 cloud_request 做一个 GET；feishu_cloud_verify_sheet_writes_enabled 默认 true，关闭时明确 status=disabled。只针对 sheet_append/sheet_update，不改文档写入合同。

追加只使用官方同版本模型中的 updates.updatedRange，并核验同 sheet ID 与实际 values 的行列数；缺字段、跨表、开放范围或尺寸不一致返回 unverifiable，不根据 tableRange 或已存在内容猜空行。更新从正规 A1 起点与 values 实际行列宽生成准确读取矩形。GET 返回范围必须与请求一致。

回执 write_verification.write_accepted=true 与 status 分开：字面值一致为 verified，同类型值不一致为 mismatch，未知位置、形状、富值、公式、表示变换或读失败为 unverifiable。供应商已经接受的写入继续是成功 handler，核验不会制造 not_started 或触发重写；读取错误只放固定结构事实。回执不包含比较的私密 values。

## 验证与下一步

合成 HTTP 与 ToolExecutor 检查个人/群、合法显式空和省略、畸形/不完整/重复/跨表、当前身份权限上界、保护范围编辑者、预检无写入和 namespace 隔离。真实 IM 文档尚未验。

建议下一步：根统一云工具 focused/strict gate 后，用授权的隔离表格核验真实 SDK 响应与读回；其它 agent 可并行只读审查省略判定与保护边界，不修改生产权限或依赖应用私人权限。

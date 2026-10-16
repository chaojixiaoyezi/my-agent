# 飞书原生图片入站交接

## 基本信息

- workstream: 飞书图片下载、验证与持久入站前半链路
- branch: `codex/feishu-native-images`
- worktree: 当前共享源码工作树
- owner: `live_route_attribution`
- date: 2026-10-15
- source baseline: `2812638850ff02c091c5bc980019f5c9474a9b30`

## 本线目标

把已认证飞书图片及已证明的引用图片交给原模型媒体入口，而不是只把附件路径写进正文。所有准备、错误结算和冷重投沿原owner与入站账，不增加权限来源或重试账。

## 实际完成

- 当前和准确引用图片共用图片数量、总字节和实际解码像素预算。配置仅读已绑定宿主的同一份 `AgentConfig`。
- 图片资源HTTP实际按剩余字节 `read(limit + 1)` 有界读取，原认证及禁止重定向行为不变。普通文件保留原附件路径，使用原文件大小上限。
- 使用真实字节检查MIME和解码，再导入当前owner原内容寻址目录；纯图、图文、多图及图片混文件均不从扩展名或正文路径猜类型。
- 整组成功后一次把明确的图片refs加入原Gateway payload，`payload_ready` 冻结后冷重投不再下载。任意图片失败不提交部分refs。
- 确定的空图、坏图、格式/像素/数量/字节超限沿原 `submitted` 阶段保存 `input_error/INPUT_MEDIA_INVALID`，使用原幂等回复出口；回复失败只重投回复，不再次下载或POST模型任务。
- 下载未完成和owner/no-follow/文件系统错误保留原异常与持久退避，不伪装为坏图，不丢图继续文字任务。
- 引用权限/不存在等原证明失败仍走原结构化引用失败事实，不发布此前准备的图片refs。

## 改动文件

- `agent_py_agent/agent/adapter/inbound_images.py`：新增单次准备预算、真实图片导入和确定输入错误类型。
- `agent_py_agent/agent/adapter/feishu.py`：资源真实读取上限及预算参数贯穿。
- `agent_py_agent/agent/adapter/manager.py`：当前/引用完整准备、明确refs传递及原错误回复出口。
- `agent_py_agent/agent/adapter/ingress.py`：确定输入错误沿原状态机永久结算。
- `agent_py_agent/tests/test_feishu_native_images.py`：真实合成图片、混合附件、引用、限额、所有权及冷重投测试。
- `agent_py_agent/tests/test_adapter_ingress.py`、`test_adapter_manager.py`：将旧文件名/假PNG替身改为真实合成小图，保留原业务断言。

## 测试命令和结果

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest \
  agent_py_agent/tests/test_feishu_native_images.py \
  agent_py_agent/tests/test_adapter_ingress.py \
  agent_py_agent/tests/test_inbound_media_ownership.py \
  agent_py_agent/tests/test_feishu_quoted_media.py \
  agent_py_agent/tests/test_adapter_manager.py \
  agent_py_agent/tests/test_adapter_feishu.py -q --tb=short
```

结果：179 passed，0 failed/error/skip；原XML为本轮验收工件 `evidence/feishu-native-images-front-focused.xml`。上述7个代码文件的局部Ruff和本线diff检查通过。最后一次测试后只补注释与本交接，无执行语义变化。

开发中首次新增测试有2个缺少原入站timestamp的夹具失败，随后旧组合有3个只返回文件名/假PNG的夹具失败，均修为真实原合同和合成图片；不把这些失败算作最终通过。未运行全仓测试、真实飞书/模型、WS或服务操作，本线不提交推送。

## 影响范围

- 飞书默认启用图片入口；关闭时保持原附件路径，不附图片refs。
- 不改变当前/引用消息身份、群真实@触发、topic/owner解析、引用精确GET证明、Gateway受理或控制语义。
- 不改变监控、后台子代理或模型能力协商；这些由后半链路沿原 `input_media` 合同承接。

## 需要主线重点复查

- `feishu_image_input_enabled=True`、原 `input_media_max_bytes/files`、新增 `input_image_max_pixels=40000000` 的配置/YAML/规范化及实际注册接线。
- `conversation/input_media_images.py` 与Pillow主运行依赖必须随发行打包，不能只有开发环境可解码。
- 后半Gateway输入校验、普通群/私聊/topic主模型及自动委派必须收到同一批refs；模型不支持图片时不能静默丢图。
- 原 `input_error` 是客观协议拒绝，不是替模型生成任务结论。

## 需要其他线协调

- 根同步新增模块、测试与本交接的文件树/模块文档，并运行统一严格gate。
- 后半线负责媒体refs在委派、后台和后端供应商请求中的原链传递；不得以路径文字补权。

## 剩余风险

- 离线测试只替换供应商资源HTTP和最终外发，尚无真实飞书图片/引用验收。
- 未成功整组准备可能留下当前owner内未发布的已下载/内容寻址文件；它们不进入Gateway请求或模型输入，清理仍由原媒体生命周期负责。
- 不把作为普通文件发送的任意扩展名附件自动升级为图片；本入口只消费飞书结构化 `image_key`。

## 后续建议

- 建议下一步：主线先完成后半refs与配置/打包集成，再统一focused/strict验收；之后由根独占做少量真实飞书当前图片、引用图片与群委派验收。其他agent可并行只读审阅，不能并行改本线冻结文件或操作真实服务。

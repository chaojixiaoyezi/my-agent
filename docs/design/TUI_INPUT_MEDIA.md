# TUI 图片与视频输入

## 解决问题与状态

原输入只传字符串，即使用户粘贴文件路径，模型请求也没有媒体内容块。当前候选实现让图片和视频
沿唯一 Gateway ask、owner、native history 链路传递；已通过官网 MiniMax-M3 的图片、视频及
退出后续问验收。未替换用户默认 Gateway，也不改变用户日常 M2.7 模型。

## 输入与持久化合同

- `/attach "图片或视频路径"` 可一次添加多个文件；`--type <MIME>` 支持系统无法识别的扩展名。
  终端拖入或粘贴整个图片/视频路径块也会添加附件。包含普通句子的粘贴保持文字，不扫描其内部路径。
- 本机 Ctrl+V 显式读取截图剪贴板。macOS 使用 osascript，Linux 使用已有 wl-paste/xclip；
  无图片时沿原应用文字剪贴板。SSH 终端不能凭空读取客户端电脑的图片剪贴板，须先把文件传到 TUI 所在机器。
- 添加动作只修改草稿；占位符与 refs 一同暂存，删除占位符后不发送。普通文字不能创建附件权限。
  磁盘导入在后台，导入未完成拒绝提交并保留输入。运行中发送附件排入下一轮，不落入纯文字 steer。
- 当前支持主对话；子代理视图和 slash 控制消息携带附件时保留草稿并提示返回主对话。
- 原件唯一存放在 `<owner_home>/media/input/<sha256>`，分块复制、原子落盘，原文件后续变化不影响已提交任务。
  refs 包含路径、SHA256、MIME、字节数和文件名。owner 路由之后再次验证归属、普通文件、大小；发送时核对哈希。
- `GatewayAskExecutionOptions.input_media`、`GatewayAskParams.input_media` 和 `ChatJob.input_media`
  传递同一组 refs；请求指纹覆盖附件。运行参数以结构化 `task_attributes.input_media` 传入 `UserTurn.media`。
  canonical native history 保存 `source.type=local_file`，不保存 base64。旧记录没有媒体字段时保持纯文本，无需迁移。

## 模型与资源边界

- 图片和视频在供应商边界临时编码；Anthropic Messages 使用 image/video source，Chat 使用 image_url/video_url。
  Responses 当前支持图片，视频明确报协议不支持，禁止静默丢弃后继续回答。
- `input_media_max_bytes` 默认 16 MiB，既限制一条新消息的原始附件总量，也限制一次模型请求展开的历史媒体量。
  `input_media_max_files` 默认 8。配置从 YAML/dataclass 到 Gateway、TUI、后端真正贯通。
- 编码前从最近媒体消息向前选择预算内附件；最近一条必须完整。超预算旧媒体在本轮发送投影中明确标记为
  归档引用，磁盘原件与 canonical 历史不删。需要再次视觉阅读已归档附件时重新添加。
- 该字节预算防止长媒体历史无限展开，但不声称 50 个大视频请求与 50 个轻文本请求资源相同。
  附件磁盘生命周期跟随用户数据保留；本片不自动清理仍被历史引用的原件。
- 模型能力由选定接口决定；不按用户正文或文件扩展名自动切模型。官方 M2.7 不支持原生图片/视频，
  M3 支持。选用不支持模态的模型时必须保留真实服务拒绝，不能伪造视觉结果。

## 已核对参考

- MiniMax [Messages API](https://platform.minimax.cn/docs/api-reference/text-chat-anthropic) 与
  [Anthropic 兼容说明](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)：核对 M3 的
  image/video、base64 MIME 和请求限制，实际请求使用官网端点。未引入供应商 SDK。
- Hermes 本机参考 `cli.py::_preprocess_images_with_vision`：其辅助视觉模型转换文字方式不适合本次
  用户明确要求的 M3 原生媒体链，因此仅借鉴输入附件与原文件引用的分离。
- OpenClaw 本机参考 `src/media/store.ts`：核对私有目录、文件边界、大小限制和原子写入；独立用标准库实现，未复制源码。

真实证据和未验证边界见 [TESTS](../../TESTS.md#官网真模型与tui媒体验收)。

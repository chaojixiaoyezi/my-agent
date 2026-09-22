# 托管进程的标准字节管道

状态：第 4 步本地内部实现已完成。复用原后台启动、host 和 ProcessSessionStore，提供插件通信需要的双向字节通道。
它不单独开放插件启用，不代替 owner/activation 归属、调用准入或持久撤销。

## 解决问题

原后台命令使用空 stdin，并将 stdout/stderr 合并落日志，不能承载持续 JSON-RPC。
新模式直接继承操作系统管道，避免增加转发进程、另一套执行器或将协议内容写入普通任务日志。

## 生命周期与通道

- `BackgroundLaunchRequest.io_mode` 显式选择 `log` 或 `stdio`，默认仍是原日志模式。
- stdio 必须绑定 launcher 寿命，`log_path=None` 且日志预算为零；不伪造输出文件。
- 一次性启动信封升为 `background_process_launch.v4`，完整声明模式及寿命；旧版或缺字段拒绝，不补默认值。
- launcher 为独立 host 创建 stdin/stdout/stderr 管道，host 将三路端点直接继承给 child。
  host 不读取、解码或转发字节，随后释放自己持有的端点，不能因托管还在收尾而阻止调用方观察 EOF。
- stdout 与 stderr 分离，无 PTY、Shell 拼接或文本转换；协议帧与有界 stderr 排空仍由 MCP 传输负责。
- 成功交接后，返回的本地 Popen 管道归调用方；启动失败由 launcher 关闭尚未交出的管道。
- 原 session 预留、host/child 出生身份、交接、截止时间、停止意图和终态提交保持。
  关闭 stdin 只表示输入结束；进程是否退出仍以原账本和精确资源回执为准。
- 管道字节没有持久重放，launcher 退出后必须停止原 child；普通 log 后台的独立寿命不改变。

同一 session Store 升为 `managed_process_session.v3`，新增严格的可选 `activation_scope`。
普通任务为 null；共享连接绑定 owner ID/home、plugin ID、activation ID，不填写业务会话、任务、run/attempt 或完成通知。
旧 v2 显式按原版本读取和更新，redo 也能恢复原 v2；查询不升版、不补插件身份，同一个 session 不允许换 schema 或归属。
普通任务停止和模型进程工具排除共享连接；按激活停止只冻结同一 owner/插件/代次，继续用原批次回执在锁外精确清理。
共享连接的终态记录不参与普通历史裁剪，保留原退出证据，后续由插件管理完成收口后明确释放。
venv/pip 准备仍归原管理 operation，不改成共享插件资源。上述字段不是执行授权；
完整启用必须继续接通安装表的鲜活准入检查、MCP 发送边界和清理后收口，不以新记录代替它们。

## 参考与验证

定向读取本地 Codex `578c1b22` 的 `rmcp-client/src/stdio_server_launcher.rs`：
本地路径创建独立 stdin/stdout/stderr；executor 路径使用 `tty=false`、`pipe_stdin=true`。
只参考字节与生命周期边界，不复制实现，不借用其远端 executor，也未运行参考项目测试。

开发验证覆盖原样双向字节及大于管道容量的输出、stderr 分离、子进程关闭输出后的 EOF、
自然退出码、交接前失败、launcher 消失及精确停止与其他 session 隔离。
这些临时进程测试不代替实际 TUI 验收；完整插件链接通后，仍须按执行 Goal 做多 TUI 装卸测试。
最终 20 个相关文件 523 项通过，源码摘要无漂移；当前实际组件在 POSIX 宿主运行，未声明 Windows 实机验收。

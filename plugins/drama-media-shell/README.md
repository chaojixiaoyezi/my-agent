# drama-media-shell

标准库 Python 插件，迁移 drama-skills 的四个离线提示词检查器，并提供 fixture-only 的媒体生产作业外壳。

- 四个检查工具只读取本次宿主上下文允许的工作区文件。
- prepare、confirm、status 和 audit 的作业记录只保存在插件私有数据目录；**不会写工作区**。
- run、collect 只会写固定离线夹具，工作区写入逐项接受 Python SDK 0.2.0 上下文裁决。
- `production_collect` 的说明是"按已完成的夹具运行记录重新生成同一份夹具字节并写回输出"——
  它不消费确认、不接供应商，只是把 run 已生成的那份夹具字节再写一遍，不是"从缓存重新收集"。
- `production_prepare`、`production_confirm` 声明为 `read_only`：宿主合同里 read_only 只保证不写
  工作区（见 docs/design/PLUGIN_WORKSPACE_WRITE.md），这两个工具仍会在插件私有数据目录写入
  作业记录/一次性确认回执，说明里已写清这个副作用。
- 包内没有真实供应商适配器、网络请求、凭据读取或子进程执行。
- **夹具产物不算生成成功。** 回执中的 `fixture=true` 与 `generation_success=false` 是稳定机器字段。
- wheel 许可元数据是 `Apache-2.0 AND MIT`：包装代码 Apache-2.0，随包上游（MIT）不因打包改变许可。

上游固定提交、逐文件摘要、许可和未迁移范围见包内 `PROVENANCE.md` 与 `LICENSE.drama-skills`。
PROVENANCE 的改动说明第 7 条注明：MP4 夹具是本仓新增的最小 ftyp isom 容器字节，不是上游的。

**待定（不改行为）**：确认码由 prepare 直接返回，模型自己就能调 confirm；当前不接付费生成，
接任何真实供应商之前必须改成宿主层面的本人确认（见 DESIGN_LEDGER C14 后续段）。

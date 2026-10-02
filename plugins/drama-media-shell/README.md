# drama-media-shell

标准库 Python 插件，迁移 drama-skills 的四个离线提示词检查器，并提供 fixture-only 的媒体生产作业外壳。

- 四个检查工具只读取本次宿主上下文允许的工作区文件。
- prepare、confirm、status 和 audit 的作业记录只保存在插件私有数据目录。
- run、collect 只会写固定离线夹具，工作区写入逐项接受 Python SDK 0.2.0 上下文裁决。
- 包内没有真实供应商适配器、网络请求、凭据读取或子进程执行。
- **夹具产物不算生成成功。** 回执中的 `fixture=true` 与 `generation_success=false` 是稳定机器字段。

上游固定提交、逐文件摘要、许可和未迁移范围见包内 `PROVENANCE.md` 与 `LICENSE.drama-skills`。

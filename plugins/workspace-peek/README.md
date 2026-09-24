# workspace-peek

自有只读插件，提供 UTF-8 文件预览与有界目录分页。当前已完成本地标准包和独立 MCP 组件验证，
原宿主完整装卸组件已通过，发布部署与真实多 TUI 验收尚未完成；进度见 `docs/tasks/REFACTOR_PLUGIN_GOAL.md`。

## 开发构建

先安装主项目开发依赖，其中 `setuptools>=77` 是标准 wheel 构建后端。
构建过程离线、不下载依赖、不改用户插件安装表；以下命令从仓库根执行，输出目录由开发者选择：

```bash
python scripts/build_plugin_api.py --wheel-dir /tmp/plugin-build/wheels
python scripts/build_plugin_package.py \
  --project plugins/workspace-peek \
  --declaration workspace_peek/declaration.json \
  --wheel /tmp/plugin-build/wheels/my_agent_plugin_api-0.2.0-py3-none-any.whl \
  --output /tmp/plugin-build/workspace-peek.zip
```

输出已存在时明确失败，不静默覆盖。插件与 SDK 使用标准 setuptools 构建；归档随后通过宿主原有包和依赖校验。
SDK 无运行依赖，插件只依赖对应精确版本 SDK；不需要在插件环境安装完整 my-agent。

## 使用声明

完成宿主安装和启用后，声明提供以下入口；这些示例不是已完成真实 TUI 验收的证明：

```text
/plugins@workspace-peek show "说明 文件.md" --bytes 4096
/plugins@workspace-peek tree . --depth 2 --limit 50
```

两种动作均支持 `--cursor` 续页。文件变更或权限上下文改变时，旧游标失效，需重新从首页读。
设置 `page_bytes`、`page_entries`、`scan_entries` 控制默认页大小和扫描预算；默认值和取值范围见唯一声明
`src/workspace_peek/declaration.json`，配置不增加读取权限。

插件只使用宿主每次调用的工作区和读取授权。空权限不会退回进程 cwd；不跟随链接、不读取多链接文件，
不执行 Shell、网络请求或写用户文件。初版需要支持严格 dirfd/no-follow 的平台，能力不足明确拒绝。
目录超出扫描预算不返回伪装完整的部分结果；先缩小目录或深度，再尝试读取。

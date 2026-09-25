# hello-go：Go 示例插件

演示“随包可执行文件”（包描述 v6 的 `executable` 类型）：插件编译成一个独立的可执行文件，宿主直接运行它。
提供一个工具 `hello`：打个招呼，并说明它是为哪个平台编译的、用的哪个 Go 版本。只用 Go 标准库。

可执行文件与平台绑定，所以打包时要用 `--platform` 写明目标平台，格式是“系统-架构”：
`darwin-arm64`、`darwin-x86_64`、`linux-x86_64`、`linux-arm64`。启用时宿主会核对本机平台。

## 构建安装包

在仓库根目录执行（中间产物与输出目录自选，不要放进仓库）：

```bash
# 本机（例如 Apple 芯片的 Mac）
(cd plugins/hello-go && CGO_ENABLED=0 go build -trimpath -o /tmp/hello-go-build/bin/hello-go .)
python3 scripts/build_plugin_files_package.py \
  --declaration plugins/hello-go/declaration.json \
  --files-root /tmp/hello-go-build \
  --platform darwin-arm64 \
  --output /tmp/plugin-build/hello-go-darwin-arm64.zip

# 交叉编译给 x86_64 Linux
(cd plugins/hello-go && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o /tmp/hello-go-linux/bin/hello-go .)
python3 scripts/build_plugin_files_package.py \
  --declaration plugins/hello-go/declaration.json \
  --files-root /tmp/hello-go-linux \
  --platform linux-x86_64 \
  --output /tmp/plugin-build/hello-go-linux-x86_64.zip
```

## 安装与启用

1. `/plugins install "/tmp/plugin-build/hello-go-darwin-arm64.zip"`
2. `/plugins enable hello-go`：先列出确认回执（会运行的文件、大小、摘要、本机平台），最后一行是带确认码的命令。
3. 核对后输入 `/plugins enable hello-go --confirm <确认码>`。
4. 使用：`/plugins@hello-go hello 小明`。

装错平台的包时，第 2 步直接返回 `platform_unsupported`，不会运行任何程序。

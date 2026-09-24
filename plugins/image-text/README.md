# image-text

自有工具型插件：对工作区里的一张图片做**本地 OCR**，返回识别出的文字、逐行外框与置信度，以及图片格式、宽高、
字节数和 sha256。当前已完成本地标准包和独立 MCP 进程组件验证，真实 TUI 验收尚未完成。

## 定位：只做文字提取，不做视觉理解

宿主里图片只能由用户在 TUI 用 `/attach` 附加后，经原模型链发给当前会话模型；插件的工具结果不能把图片交给模型。
所以本插件**不调用任何模型、不联网**，只在本机跑 `tesseract` 提取文字。需要"看懂"图片内容（图表、界面、照片）时，
请在 TUI 用 `/attach` 附加图片，并把会话模型切到支持视觉的模型（如官方 MiniMax-M3）；本插件是它之外的文字提取补充，
每次返回里也带有这句提示。

## 依赖准备

插件本身只依赖精确版本 SDK `my-agent-plugin-api==0.2.0`，不打包任何 OCR 库或模型；OCR 用系统里的 `tesseract` 可执行文件：

- macOS：`brew install tesseract`（自带 `eng`；其他语言 `brew install tesseract-lang`）
- Debian/Ubuntu：`sudo apt install tesseract-ocr`，中文再装 `tesseract-ocr-chi-sim`
- Fedora：`sudo dnf install tesseract`，语言包如 `tesseract-langpack-chi_sim`

用 `tesseract --list-langs` 查看已安装语言。宿主启动插件时只透传 `PATH` 等基线环境变量；如果 Gateway 的 `PATH`
里找不到 tesseract（例如 Homebrew 的 `/opt/homebrew/bin` 不在其中），请在插件设置 `tesseract_path` 填绝对路径。
找不到时工具明确返回 `OCR_UNAVAILABLE`："OCR 不可用：未找到 tesseract（可在设置 tesseract_path 指定）"，并照常附带图片元数据。

## 开发构建

以下命令从仓库根执行，构建离线、不下载依赖、不改用户插件安装表，输出已存在时明确失败：

```bash
python scripts/build_plugin_api.py --wheel-dir /tmp/plugin-build/wheels
python scripts/build_plugin_package.py \
  --project plugins/image-text \
  --declaration image_text/declaration.json \
  --wheel /tmp/plugin-build/wheels/my_agent_plugin_api-0.2.0-py3-none-any.whl \
  --output /tmp/plugin-build/image-text.zip
```

## 使用

安装并启用后提供一个动作，与同名 `read_only` 工具一一对应：

```text
/plugins@image-text read --path screenshots/报错.png
/plugins@image-text read --path scan.jpg --lang chi_sim+eng
```

中文示例：普通对话里说"用 image-text 读一下 docs/流程图.png 里的文字"，模型会调用 `read`，拿到 `text`（按行拼接的全文）
和 `lines`（每行 `text`、平均 `confidence`、外框 `left/top/width/height`），以及 `word_count`、`mean_confidence`。

## 设置

- `tesseract_path`：tesseract 可执行文件路径，默认空（在 PATH 中查找）。
- `default_lang`：默认 OCR 语言，默认 `eng`；多语言用 `+` 连接，如 `chi_sim+eng`。
- `max_image_bytes`：单张图片读取上限，默认 10485760（10 MiB）。
- `ocr_timeout_seconds`：单次 OCR 超时，默认 30，范围 1–120；超时终止 tesseract 并返回 `OCR_TIMEOUT`。

取值范围见唯一声明 `src/image_text/declaration.json`；设置不增加任何读写权限。

## 边界

- 路径经宿主逐次读取上下文授权，用 SDK no-follow 打开；符号链接、多链接文件、`..` 上溯、越出读取范围一律拒绝。
- 只按文件头魔数识别 PNG / JPEG / GIF / WebP，扩展名不参与判断；其他格式返回 `UNSUPPORTED_FORMAT`，头部损坏返回
  `INVALID_IMAGE`。宽高用标准库解析，不依赖 Pillow。
- 语言名只允许小写字母和下划线、多语言用 `+` 连接；请求的语言包未安装时返回 `LANG_UNAVAILABLE`，并列出
  `tesseract --list-langs` 的结果（`available_langs`）。
- OCR 时把图片字节写到插件数据目录（`MY_AGENT_PLUGIN_DATA_DIR`）下 `tmp/` 的临时文件，无论成败都删除，从不写工作区；
  tesseract 子进程与插件同进程组，宿主回收插件时一并回收。

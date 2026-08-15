---
name: pdf-translate-toolchain
description: 把英文 PDF(论文/文档)翻译成中文 PDF 的可执行工具链——抽取、翻译、重排、自检。
when_to_use: 用户要求把 PDF 文档/论文翻译成中文并产出 PDF 文件时。
tags: PDF, 翻译, 论文, pdf2zh, 排版
tools_required: run_command
scope: builtin
risk_level: low
---

# PDF 翻译工具链

"翻译成中文 PDF"是一条可执行的工程链路,不是一步工具调用。三条路按优先级:

## 路线 A:pdf2zh(保版式首选)

```bash
pip install pdf2zh  # 或 uv tool install pdf2zh
pdf2zh paper.pdf -lang-out zh -o out_dir/   # 产出 paper-zh.pdf(版式/图表保留)
```
- 需要联网下载字体与调用翻译服务;离线环境会失败,失败就换路线 B。

## 路线 B:抽取→翻译→重排(标准库友好)

1. 抽正文:`pip install pymupdf` 后用 `fitz` 按页抽 text block(跳过参考文献/密集公式区)。
2. 翻译(按体量二选一):
   - **大体量(几十页/多篇)首选脚本批量翻译**:运行环境自带 `AGENT_API_KEY`、`AGENT_API_BASE`、`AGENT_MODEL_NAME` 三个环境变量,指向与本代理同款的 **anthropic 兼容** 端点——直接用它们,不要去猜 OpenAI/DeepSeek/OpenRouter 等其他服务商(key 不通用,必然 401):
     ```python
     import os, anthropic
     client = anthropic.Anthropic(api_key=os.environ["AGENT_API_KEY"], base_url=os.environ["AGENT_API_BASE"])
     resp = client.messages.create(model=os.environ["AGENT_MODEL_NAME"], max_tokens=4000,
                                   messages=[{"role": "user", "content": f"把下面英文学术段落翻译成中文,公式/代码/引用原样保留:\n{chunk}"}])
     zh = resp.content[0].text
     ```
   - 小体量(几页)可正文分段由你自己(模型)直接翻译,每段保留原 PDF 页码标记。
3. 重排出 PDF:`pip install reportlab`,注册中文字体(macOS 自带 `/System/Library/Fonts/STHeiti Light.ttc` 或 `PingFang.ttc`),按页码顺序排版;图表页可用 fitz 把原页面渲染成图片插入。
4. **写完脚本必须真的运行它并验证产物落盘**——"脚本已就绪"不等于"翻译已完成";交付前跑下方自检,缺产物的篇目如实记录,绝不在清单里声称不存在的文件。

## 路线 C:Markdown 中转(兜底)

抽取→翻译→写 Markdown→`pandoc -o out.pdf --pdf-engine=xelatex -V CJKmainfont="PingFang SC"`(需本机有 pandoc+xelatex;没有就退回交付 Markdown 并如实说明 PDF 工具缺失)。

## 产出自检(交付前自己跑)

- 文件头是 `%PDF` 且能被 fitz 打开、页数 > 0。
- 中文字符占比:抽回译文页文本,中文字符数/总字符数应明显高于 0.3——低于说明翻译没真正落进 PDF(常见原因:字体未注册导致中文渲染成空白)。
- 每篇一个独立 PDF,文件名含原文标识。处理失败的篇目如实记录原因,不要悄悄跳过。

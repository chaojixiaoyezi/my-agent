---
name: academic-word-pdf-layout
description: 把论文/报告/正式商务文档(SOW、方案、招投标、验收报告)做成排版规整、像样的 Word/PDF——尤其是翻译后的论文、要交付客户的正式文件。核心是"重建路由"(源文件→结构化抽取→Markdown 中间态→用模板/reference.docx 生成 DOCX→LibreOffice 转 PDF),优先 Pandoc/docxtemplater 而非 python-docx 手工拼版,避免排版难看。与 pdf-translate-toolchain 的区别:那个专做"把 PDF 翻成中文 PDF",本 skill 管的是"最终产出要像正常 Word/学术文档该有的样子(原生标题、可更新目录、紧凑表格)"。
when_to_use: 输出需要像正常学术论文或正式商务文档(SOW/方案/招投标/验收报告/企业报告)、要交付给客户、Word 打不开或识别不了目录大纲、多级标题排不对、表格看着太空、或翻译完的论文要重新排版成规整文件时。
tags: Word, DOCX, 排版, 论文, 学术文档, 正式文档, 商务文档, SOW, 方案, 招投标, 验收报告, pandoc, reference.docx, docxtemplater, LibreOffice, soffice, 目录, 多级标题
tools_required: run_command
scope: builtin
risk_level: low
---

# 学术 / 正式 Word·PDF 排版

要产出"看起来就是一篇正常学术论文 / 正式商务文档"的 Word 或 PDF 时用本方法——翻译后的论文、SOW、方案、招投标、采购、验收报告、结构化报告都算。

## 黄金法则:走"重建路由",别手工拼版

可读的学术/正式文档,默认走这条链:

```
源 PDF/资料 → 结构化抽取 → Markdown/HTML 中间态 → 用模板/reference.docx 生成 DOCX → LibreOffice 转 PDF
```

**例外**:用户明确要"保留原 PDF 版式、只翻正文、图表公式参考文献不动"时,改走下面第 4 条"只翻正文"路由。

## 选哪条路由

1. **首选:Pandoc + reference.docx**
   - 把内容写成干净 Markdown(标题、图注、表格、参考文献都标好)。
   - `run_command` 跑:`pandoc input.md --reference-doc reference.docx -o output.docx`。
   - 再用 LibreOffice 转 PDF:`soffice --headless --convert-to pdf output.docx`。
   - reference.docx 是一个预设好样式的空 Word(定义标题/正文/表格样式),Pandoc 按它套版。

2. **模板型文档:docxtemplater**
   - 已有稳定的 `.docx` 模板、且内容填空位明确时用。
   - 比 python-docx 更能保住设计好的 Word 版式。

3. **HTML 管线:HTML → DOCX/PDF**
   - 仅当 HTML 本身结构良好、CSS 打印版式可接受时用。
   - 转 DOCX 优先 docx4j ImportXHTML 或 Pandoc,别用小众 html-to-docx 包。

4. **保留原版式:只翻正文(body-only)**
   - 要求图表/公式/页面结构贴近原 PDF 时用。
   - 只翻正文页,然后把原始页(参考文献/附录/代码/公式密集的尾部)拼回去。
   - 目录页机翻不可信——重画目录、加书签,别信机翻的 TOC。

5. **正式 SOW / 商务 Word**
   - SOW、方案、采购、验收报告、企业报告用。
   - 必须真 Word 结构:正文用 `标题 1`/`标题 2`;目录用可更新的 Word 目录域(不是手打的假目录)。
   - 表格要紧凑、信息密,别留一堆空白显得稀。

6. **别拿 python-docx 当主排版引擎**
   - 仅简单报告、SOW 草稿、模板微调、且显式建了原生 Word 结构时可接受。
   - 复杂学术双栏论文别用,除非只是往预建模板里填空。

## 交付前质量门(逐条过)

- DOCX 能打开、章节齐全。
- PDF 是从 DOCX 转的,不是另起一份会跑偏的 HTML(除非显式说明)。
- 数过页数、肉眼看过版式样张。
- 中文字数非零、和原文大致成比例(防漏翻/串版)。
- 图表在位,或显式列出"哪些图省略了"。
- 附一张截图/contact-sheet 当版式自检证据(对照 [[verification-before-completion]]:先拿到证据,再下结论)。

## 环境前提

- 缺 `soffice`/LibreOffice 就先装上、或在做最终 PDF 前把它作为阻塞点报出来。
- Pandoc 一般已就绪;没有就先装。

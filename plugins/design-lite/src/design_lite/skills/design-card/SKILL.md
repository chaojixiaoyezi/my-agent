---
name: design-card
description: 用 design-lite 插件按模板生成卡片、海报或落地页 HTML 设计文件，并只修改标题、副标题、主色这几个字段。
when_to_use: 用户要"做一张卡片/海报/简单落地页""出一个 HTML 设计稿""把这个设计的标题或颜色改一下"，且 design-lite 插件已启用时。
tags: 设计, 卡片, 海报, 落地页, HTML 设计稿, 改标题, 换颜色, design card, poster, landing page
risk_level: low
---

# design-card：用 design-lite 生成和修改 HTML 设计文件

## 什么时候用

- 用户想快速得到一个可直接在浏览器打开的单文件设计：信息卡片、宣传海报或简单的产品落地页。
- 用户想改一个已有 design-lite 文件的标题、副标题或主色。
- 不适合：要做 PPT、视频、复杂交互页面、多页网站，或要改版式、图片、正文段落。这些超出本插件能力，
  直接告诉用户，不要硬用本插件，也不要手工往生成的文件里加结构。

## 三种模板

| 模板 | 样子 | 适合 |
| --- | --- | --- |
| `card` | 白底圆角小卡片，顶部一条主色色带，标题用主色 | 通知、名片、一句话公告 |
| `poster` | 3:4 竖版，整块主色背景，大号白字标题 | 活动海报、封面、宣传图 |
| `landing` | 顶栏 + 居中大标题 + 主色按钮 + 三个要点块 | 产品或活动的简单介绍页 |

没说清楚要哪种时，按用途挑最接近的一种；实在无法判断再问用户。

## 步骤：先 create，再 edit

1. 用 `create` 工具生成文件：必填 `template`、`output`、`title`，可选 `subtitle`、`color`。
   - `output` 必须以 `.html` 结尾，放在当前工作区内（例如 `designs/活动海报.html`）；不要写到工作区外或系统目录。
   - `color` 只接受 `#RRGGBB`，例如 `#e4572e`。用户说"红色""品牌蓝"时，先换算成具体的十六进制值。
   - 输出文件已存在会被拒绝。只有用户明确同意覆盖时才传 `overwrite: true`；否则换一个文件名。
2. 之后要改内容，用 `edit` 工具：必填 `path`，再给 `title`、`subtitle`、`color` 中至少一个。
   - 只改这三个声明过的字段，文件其余部分逐字节保留。`subtitle` 传空字符串表示清空副标题。
   - 结果会返回每个字段的旧值和新值，把它们告诉用户，方便核对。
3. 用户想换模板时，用 `create` 另建一个文件（或经用户同意后加 `overwrite` 覆盖），`edit` 不能换模板。

## 不要做的事

- 不要用通用写文件工具手工修改生成的 HTML，尤其不要改动 `data-dl-field` 标记、`<meta name="generator">`
  或 `:root{--dl-color:...}` 这一行；改坏后 `edit` 会拒绝该文件。
- 不要让 `edit` 去改不是 design-lite 生成的 HTML，它会直接拒绝。
- 不要在标题里写 HTML 代码期望被渲染，插件会把文字全部转义成纯文本显示。

## 常见错误码

- `UNKNOWN_TEMPLATE`：模板名不是 card / poster / landing。
- `INVALID_COLOR`：颜色不是 `#RRGGBB`。
- `OUTPUT_EXISTS`：输出文件已存在，需要换名或经用户同意覆盖。
- `NOT_DESIGN_FILE`：不是本插件生成的文件，或标记被手工改坏。
- `FIELD_MISSING` / `NO_FIELDS`：文件里没有该字段 / 没有给任何要改的字段。
- `PATH_WRITE_SCOPE_BLOCKED` / `PATH_READ_SCOPE_BLOCKED` / `UNSAFE_PATH`：路径越出工作区或含链接，换到工作区内的普通路径。

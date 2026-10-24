---
status: live
type: reference
module: design-studio
last_updated: 2026-10-23
---

# 站点配置（design-studio.json）

一个网站一份配置，告诉 design-studio：静态文件在哪、哪些页面能编辑、文字写回哪个文件、修改单写到哪、编辑器面板给哪些选项。工具本体不含任何站点专属的东西，站点之间的差别全在这里。

最少只要 `pages`：

```json
{
 "pages": {
  "首页": {"url": "/index.html", "html": "index.html"}
 }
}
```

完整例子：仓库里的 [examples/plain-site/design-studio.json](../examples/plain-site/design-studio.json)（最少配置），以及 [tests/regression/fixtures/blog.design-studio.json](../tests/regression/fixtures/blog.design-studio.json)（一个真实博客，几乎用到了所有字段）。

## 路径怎么算

- `base`：站点根目录，相对配置文件所在目录，默认 `.`（配置文件放在网站根目录时不用写）。配置文件放在子目录里时写 `".."` 指回去。
- 其余所有相对路径（`static_root`、`pages.*.html` 等）都相对 `base`。
- 回给编辑器、写进修改单的路径也是相对 `base` 的写法，正斜杠。

## 顶层字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `base` | `.` | 见上 |
| `static_root` | `.` | 静态文件根：浏览器访问 `/x/y.html` 对应 `<static_root>/x/y.html`。以 `.` 开头的文件和目录不对外，符号链接不能跟出这个目录 |
| `port` | 不固定 | 写了就只用这个端口，被占就报错退出，不悄悄换（浏览器里的草稿、偏好按「地址 + 端口」隔离，换了端口看起来就像全丢了）。没写、命令行也没给 `--port` 时，从 4580 往后找空闲端口 |
| `editor_mount` | `/_studio/` | 编辑器的网址。不能是 `/`。不带斜杠访问会 301 到带斜杠的 |
| `root_redirect` | 第一个页面 | 访问 `/` 时 302 跳到哪：写页面名，或以 `/` 开头的地址；写 `null` 就不跳（交给静态文件，比如根目录的 index.html） |
| `sheet_dir` | `.design-studio/修改单` | 修改单默认放这里，一个页面一份 `<页面名>.json` |
| `feedback_dir` | `<sheet_dir>/反馈` | 反馈框（`/api/feedback`）的文件放这里，一张单子一份 `<名字>.json` |
| `pages` | 必填 | 能编辑的页面，见下 |
| `after_save` | 无 | 保存后钩子，见下 |
| `text` | 见下 | 文字写回的规则 |
| `editor` | `{}` | 编辑器面板的选项，见下 |

字段名以下划线开头（如 `_说明`）的会被忽略，可以拿来写备注。

## pages

`"页面名": {...}`，页面名会出现在编辑器的下拉框和修改单文件名里。

| 字段 | 说明 |
|---|---|
| `url` | 必填。页面的站内地址，以 `/` 开头 |
| `html` | 页面的 HTML 源文件。文字在数据文件里找不到时，会在这里的正文里找原句写回 |
| `data` | 数据文件：JSON，或 `window.X = {...};` 这样包一层的 JS。文字优先在这里按字段找原句写回 |
| `sheet` | 这一页的修改单，默认 `<sheet_dir>/<页面名>.json` |
| `guide` | 这一页的修改说明文档（给 AI 看的），「复制给 AI 的指令」里会让 AI 先读它 |
| `skip` | 数据里有、但页面上没用到的顶层字段名：改了不生效，找原句时跳过 |

## 文字怎么写回

保存时每条「文字」改动按这个顺序：

1. **数据文件**（`data`）：在数据里找和原句完全相同的字符串字段。只有一处就写回；好几处时用元素所在板块挑（`text.section_keys`）；还分不清就不写。原句只是某个字段的一部分（比如页面按逗号分行显示）也行，前提是它在那个字段里只出现一次、整份数据里也只有这一个字段含它。编辑器对已知元素会带字段提示（`editor.known[].hint`），对得上就直接用。
2. **HTML 正文**（`html`）：原句（按 HTML 转义后）在 `<script>`、`<style>`、`<textarea>`、`<title>`、注释和标签以外的文本里正好出现一次，就换掉。
3. 都不行就记进修改单，状态「待 AI」，`note` 写原因。

写回只替换被改的那个字符串，文件其余字节一个不动；写完重新解析校验，对不上就撤回。

### text

| 字段 | 默认 | 说明 |
|---|---|---|
| `skip_leaf` | id、url、src、href、poster、cover、image、qr、page、page_base、avatar、domain、kind、type、repo | 不显示在页面上的字段名（链接、路径、编号），任何层级都跳过 |
| `section_keys` | `{}` | 板块名 → 优先查的顶层字段，如 `{"项目": ["projects"]}`。板块名来自 `editor.sections` |
| `follow` | `[]` | 改了要连带改别处的字段：`[["正则", "提示"], ...]`。正则匹配字段路径（如 `^person\.name$`），照样写回，但状态记「已写入，待 AI 跟进」，提示写进 `follow` 给 AI |
| `allowed_tags` | `["b", "strong", "em"]` | 文字里允许保留的行内标签，其余标签写回时去掉 |

## after_save：保存后钩子

```json
"after_save": {
 "label": "敏感信息扫描",
 "cmd": ["{python}", "-I", "scripts/scan.py", "{file}"],
 "ignore_prefixes": ["扫了"],
 "timeout": 60
}
```

文字写回后，对每个写过的文件跑一次 `cmd`（`{file}` 换成文件绝对路径，`{python}` 换成跑 design-studio 的 Python，`{base}` 换成站点根目录；工作目录是 `base`）。命令退出码不是 0、跑不起来或超时，就把这次所有写回撤回，条目状态记「没写入」，`note` 和保存回执的 `warnings` 里写上命令的输出（去掉 `ignore_prefixes` 开头的行）。

## editor：编辑器面板

| 字段 | 说明 |
|---|---|
| `title` | 编辑器顶栏和浏览器标签上的名字，默认 design-studio |
| `viewports` | 视口预设 `[{"w": 1440, "h": 900, "label": "桌面 1440"}, ...]`，默认桌面 1440、笔记本 1280、手机 390 |
| `theme` | 页面深浅色怎么切：`{"storage_key": "主题存在 localStorage 的键", "attr": "html 上的属性名", "light": "light", "dark": "dark"}`。不写就不显示切换按钮 |
| `sections` | `section` 元素的 id → 板块名，如 `{"top": "首屏"}`。没列的用 id 本身；不在任何带 id 的 section 里的叫「页面」 |
| `section_extra` | 不是 section 的区域：`[{"sel": ".topbar", "name": "顶栏"}]`，先于 `sections` 判断 |
| `known` | 有名字的元素：`[{"sel": "#zhName", "name": "中文名", "num": 3, "hint": "person.name"}]`。选中它（或它里面的东西）时显示「板块 · 编号 名字」；`hint` 是它的文字在数据文件里的字段路径 |
| `hint_rules` | 一组同类元素按位置对应数组字段：`[{"selector": ".about > p", "siblings": ":scope > p", "hint": "person.about.{i}"}]` |
| `ignore_classes` | 算选择器时忽略的状态类名（正则），默认 `^(is-|active$)`。动画加的 `in`、`on` 之类要加进来，否则选择器会随状态变 |
| `inline_format_classes` | 选元素时并入上一层的格式类名，如逐字动画的 `bt-seg`、荧光笔的 `mark` |
| `styles` | 样式面板的选项，见下 |
| `ai_prompt` | 「复制给 AI 的指令」的模板，见下 |

### editor.styles

样式面板只给站点设计规范里的值。一般只列 CSS 变量名，值从被编辑页面自己读；规范只写在文档里、CSS 没有变量时，字号、间距、圆角、行高也可以直接写值（见表下）。

| 字段 | 说明 |
|---|---|
| `sizes` | 字号，如 `["--fs-14", "--fs-16"]` |
| `spaces` | 间距（上下留白、内边距、子元素间距共用） |
| `radii` | 圆角 |
| `line_heights` | 行高 |
| `fonts` | 字体：`[["--font-body", "正文"], ...]` |
| `font_weights` | 字重：`[["400", "常规 400"], ...]`，默认 300—700 |
| `text_colors` / `bg_colors` | 文字颜色、底色可选的变量名；底色可以加 `"none"`（透明） |
| `color_names` | 色块悬停时显示的中文名：`{"--fg": "主文字"}` |

`sizes`、`spaces`、`radii`、`line_heights` 每一项可以是：

- 变量名：`"--fs-16"`，值从页面读；
- 直接写的值：`"28px"`、`"1.75rem"`、`"1.85"`，给设计规范只写在文档里、CSS 没有对应变量的站点用。写成 JSON 数字（`1.85`）也行；
- 带名字：`["28px", "H1"]`、`["--space-md", "md"]`，面板里显示成「H1 · 28px」。

判断元素「现在」是哪一档时，按浏览器算出来的值比：长度写 px、rem 都能对上（`1.75rem` 和 28px 算同一档）；行高写无单位的比例（`"1.85"`）时按比例比，差不到 0.006 算同一档，写 px 时按像素比。同一个值列了两次只留第一项。

修改单里 `token` 记选中的那一项（变量名或值），`new` 记「名字 · 变量名（值）」或「名字 · 值」，没名字就省掉名字，AI 能看出用的是规范里哪一档。

没写 `styles`（或没写某一项）时，编辑器从页面 `:root` 上的 CSS 变量按名字前缀自动归组：`--fs-` / `--font-size-` / `--text-` 是字号，`--sp-` / `--space-` / `--spacing-` / `--gap-` 是间距，`--r-` / `--radius-` / `--rounded-` 是圆角，`--lh-` / `--leading-` / `--line-height-` 是行高，`--font-*` 是字体（值不是字体的跳过，如 `--font-lineheight-1: 1.25`），值是颜色的都算颜色。字号、间距、圆角、行高、字体这几组里，附属变量不算：名字是另一个变量加 `--…`（Tailwind 的 `--text-xs--line-height` 跟着 `--text-xs`）；没有同名基础变量的（`--fs--1` 这种负数档位）照常算。颜色不做这层过滤（`--color-primary--hover` 照样是可选的颜色）。

### editor.ai_prompt

修改单页签顶上「复制给 AI 的指令」复制的那段话。可用占位：`{sheet_abs}` 修改单绝对路径、`{sheet}` 相对路径、`{count}` 等 AI 处理的条数、`{url}` 页面地址、`{page}` 页面名、`{guide}` 说明文档路径、`{guide_line}`（有说明文档时是一句「动手前先读……」，没有时为空）。不写用默认模板。

## 修改单格式

`<sheet>` 是一个 JSON 文件，只追加：

```json
{
 "page": "首页", "file": "index.html", "data": "data.js", "guide": null,
 "note": "…状态说明…",
 "items": [
  {"id": "20261023-001", "time": "2026-10-23 03:14", "type": "文字", "where": {"section": "开头", "label": "…", "selector": "h1", "num": null, "classes": "", "tag": "h1"},
   "old": "原句", "new": "新句", "status": "已写入", "file": "index.html"},
  {"id": "20261023-002", "type": "文字", "status": "已写入", "field": "doing.0.title", "value": "…"},
  {"id": "20261023-003", "type": "样式", "status": "待 AI", "prop": "font-size", "label": "字号", "old": "16px", "new": "--fs-20（20px）", "token": "--fs-20",
   "css": {"selector": "…", "prop": "font-size", "value": "20px"}},
  {"id": "20261023-004", "type": "意见", "status": "待 AI", "text": "这里想和上面拉开一点"}
 ]
}
```

`status`：已写入 / 已写入，待 AI 跟进 / 待 AI / AI 已处理 / 不改 / 没写入。AI 处理完改 `status`，在 `reply` 里写一句做了什么；编辑器会显示回复。

# design-studio

在浏览器里打开自己的网页，**点哪改哪**：双击改字，按设计规范调字号、间距、颜色，写一句意见。点保存后，能写回的文字直接写进源文件，写不回的和样式、意见一起记进「修改单」，一键复制给 Claude Code 或 Codex 去落地，它们改完写的回复也在编辑器里看得到。

- **小改自己改**：改一句文案不用找它在哪个文件。文字在 HTML 正文或数据文件（JSON、`window.X = {...}`）里能唯一找到，就精确写回，只换被改的那个字符串，文件其他字节不动。
- **大改交 AI**：换布局、加东西这种，圈出元素写意见。修改单里记着元素位置（板块、名字、CSS 选择器）和你的原话，AI 能精确定位。
- **只能选规范里的值**：样式面板的选项来自站点自己的 CSS 变量（设计 token），改不出规范外的样子。
- **零依赖**：Python 3.9+ 标准库 + 原生 JS，不用 npm、不用构建，下载就能跑。只在本机 127.0.0.1 上开服务。

两种模式，共用同一个本机服务：

- **页面模式**（`serve`）：改已经做出来的页面，下面大部分说的是它。
- **骨架模式**（`lab`）：实现之前，在页面包（骨架 HTML + `tokens.json`）上按真实 token、真实像素定比例和间距，保存直写仓库。原来是 [forge-design-lab](https://github.com/antonailog/forge-skills/tree/main/skills/forge-design-lab) Skill 自带的工作台服务，现在本体在这里，Skill 在 forge 流程里调用它。

## 三步跑起来

```bash
git clone <本仓库> design-studio
cd design-studio
python3 design_studio.py serve examples/plain-site/design-studio.json
```

打开它打印的地址（默认 http://127.0.0.1:4580/_studio/），在示例页面上双击标题改字，点「保存」，再看 `examples/plain-site/index.html` 已经变了。

换成自己的网站：在网站根目录放一份 `design-studio.json`，最少只要登记页面：

```json
{
 "pages": {
  "首页": {"url": "/index.html", "html": "index.html", "data": "data.js"}
 }
}
```

然后 `python3 /path/to/design_studio.py serve design-studio.json`。全部字段见 [docs/config.md](docs/config.md)。

## 用起来是什么样

| 想做什么 | 怎么做 |
|---|---|
| 选一个元素 | 点它，红框标出来，右边出面板。选大了点里面的文字，选小了点「选上一层」 |
| 改文字 | 双击文字，直接在页面上改；回车结束，Esc 放弃 |
| 调样式 | 面板「样式」里选字号、字重、行高、字体、颜色、底色、对齐、留白、内边距、圆角。页面上立刻变 |
| 写意见 | 不好直接改的，在面板「意见」里写一句 |
| 看页面原本的交互 | 顶栏切到「浏览」，页面照常可点 |
| 看手机宽度 | 顶栏切「手机 390」（视口预设可配置） |
| 交给 AI | 「修改单」页签点「复制给 AI 的指令」，发给 Claude Code 或 Codex。它读修改单、逐条改、把状态改成「AI 已处理」并写回复 |

保存以后：

| 改动 | 结果 |
|---|---|
| 文字，在数据文件或 HTML 正文里唯一找得到 | 直接写回，修改单记「已写入」 |
| 文字，改的字段还牵连别处（配置 `text.follow`） | 照样写回，记「已写入，待 AI 跟进」，写明要跟进什么 |
| 文字，找不到或对上好几处 | 不写，记「待 AI」，写明原因 |
| 样式、意见 | 记「待 AI」。AI 落地之前，编辑器在预览里一直替你套着这些样式改动 |
| 保存后钩子不过（如敏感信息扫描命中） | 这次的写回全部撤回，记「没写入」 |

## 框架站（Vite、Next、React……）

编辑器要直接读 iframe 里的页面，浏览器只允许同源这么做。开发服务器开着时加 `--proxy`：

```bash
python3 design_studio.py serve design-studio.json --proxy http://localhost:5173
```

编辑器路径以外的请求原样转给开发服务器（站点自己的 `/api/*` 也是），页面和编辑器同源。框架站的文字一般写在组件代码里，写不回，文字、样式、意见都进修改单交给 AI。限制：不转 WebSocket，热更新不生效，改完手动刷新。

## 命令

```bash
python3 design_studio.py serve <站点配置.json> [--port N] [--proxy URL]   # 页面模式
python3 design_studio.py check <站点配置.json>                            # 只检查配置和文件
python3 design_studio.py lab <仓库目录> [--port N] [--status | --stop]      # 骨架模式
```

骨架模式：静态根就是仓库根，页面包在 `docs/assets/visuals/<页面>/`，打开它打印的地址就是页面包索引。一个仓库一个常驻服务，状态记在 `.forge/design-lab/server.json`，已经在跑就不重复启动；端口默认 4577，被占就往后找。保存时按节点 id 合并（值、结局、反馈取页面的，结构取磁盘的），写 `tokens.json` 和由它生成的 `.tokens.js`、`.tokens.css`，回执在 `.forge/design-lab/last-save.json` 和 `saves.jsonl`。页面包的生成、校验脚本和七层审查流程在 forge-design-lab Skill 里；站点配置写 `"lab": true` 时，页面模式的服务也同时提供骨架模式的接口。

端口：命令行给了 `--port` 或配置里写了 `port`，就只用这个端口，被占就报错退出。不会悄悄换端口：浏览器里存的草稿和偏好按「地址 + 端口」隔离，换了端口看起来就像全丢了。都没给时从 4580 往后找空闲端口。

## 接口

都只对本机开放（校验 Host 和 Origin，POST 只收 JSON）。编辑器用挂载路径下的一份（如 `/_studio/api/pages`）；非代理模式时根路径 `/api/*` 也有一份，给站点里直接调接口的页面用。

| 接口 | 做什么 |
|---|---|
| `GET api/pages` | 登记的页面 |
| `GET api/config` | 编辑器面板的配置 |
| `GET api/sheet?page=` | 某页的修改单 |
| `POST api/save` | 保存一轮改动：`{"page": "首页", "items": [...]}` |
| `GET/POST api/feedback` | 反馈框：对比页、调整单里每条下面的选项和意见，一张单子一个 JSON 文件 |

修改单的格式见 [docs/config.md](docs/config.md#修改单格式)。

## 开发

```bash
python3 -m unittest discover -s tests/unit -v          # 单元测试
python3 tests/regression/run.py --target new \
  --inject tests/regression/fixtures/blog.design-studio.json=99-其他/design-studio.json \
  --cmd "{python} -I {repo}/design_studio.py serve {site}/99-其他/design-studio.json --port {port}"   # 真实博客的兼容回归（要有那个博客）
```

`docs/compat.md` 是从第一个用户（一个个人博客）的旧编辑服务迁过来时逐条核对的兼容清单；骨架模式的兼容回归是 `tests/regression/lab_run.py`（拿用 forge-design-lab 的项目做沙盒，和旧 serve.py 逐条对照）；`docs/archive/raw/discussions/` 是立项讨论。

## 目录

```
design_studio.py      命令行入口
studio/               本机服务：config（站点配置）、server（静态文件、安全、路由）、page_mode（页面模式接口）、writeback（精确写回）、proxy（代理）、lab_mode（骨架模式接口）
studio/lab/           骨架模式的 token 工具：build_tokens（json → js/css 并校验）、adapt_tokens（读项目现有 token）
web/editor/           页面模式的编辑器前端（原生 JS，界面样式自带）
web/lab/              骨架模式的前端母本：工作台引擎、零件库页面、页面包模板（forge-design-lab 从这里同步到项目）
examples/plain-site/  最小示例站
docs/                 config.md 字段说明、compat.md 兼容清单、立项讨论
tests/                unit 单元测试、regression 博客兼容回归
```

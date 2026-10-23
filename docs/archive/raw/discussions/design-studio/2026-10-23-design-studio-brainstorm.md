---
status: archive
type: brainstorm
module: design-studio
last_updated: 2026-10-23
---

# 头脑风暴：design-studio（把页面编辑器独立出来，和 forge-design-lab 合成一套）

**日期**：2026-10-23
**模式**：forge-brainstorm 构建模式
**状态**：✅ 已确认（第二轮：审查后改为分两步）
**参与者**：用户（阿東） + Claude

## 0. 如何继续这次讨论

- 读这份文档，再看 §6 决策表和 §8 对抗性审查。
- 现状代码：博客编辑器 `内容/个人博客/4-页面/编辑器/`（index.html、editor.js、editor.css、README）+ `内容/个人博客/99-其他/脚本/编辑服务.py`；design-lab 引擎 `Skill/forge-skills/skills/forge-design-lab/`（scripts/serve.py、assets/workbench/、assets/system/、scripts/adapt_tokens.py 等）。
- 进度锚点：第一轮选了「方案 B 一步到位」；对抗性审查后，第二轮改成**分两步**（§9）：这一轮只做页面模式，下一轮再搬 design-lab。名字 design-studio，真源是独立仓库 `工具/design-studio/`。下一句可以直接说「按 §9 开工」或「继续讨论 §9 第 N 条」。

## 1. 背景

用户原话：「页面编辑器可以独立为一个工具，我希望你先和我讨论怎么独立，然后放在这个目录下：<工作区>/工具」。讨论中补充：「我希望它和 forge-design-lab 是一套东西，都是在设计一个页面时能基于这个页面做调整的工具，skill 和工具不冲突，一个是独立的、一个是在 forge 流程中的」。

起因：盘点「工具」列表的候选时，页面编辑器被列为「可考虑」，因为它和博客绑死了。

## 2. 深度提问摘要（构建模式 4 问）

| 问 | 用户的回答 |
|---|---|
| 最酷的版本 | 任何本地网站点开就能改（文字写回源文件，写不回的变成修改单）；和 AI 助手打通成闭环（圈一处写意见 → AI 改 → 页面刷新、看到回复） |
| 给谁看 | 自己先天天用；开源给用 AI 建站的人；自媒体发一篇 |
| 最快路径 | 先去博客化，再加闭环 |
| 学习目标 | 不为学，要好用好装：零依赖（Python 标准库 + 原生 JS，不用构建） |

## 3. 景观感知

- [Stagewise](https://docs.stagewise.io/features/dom-context-selector)（YC S25）：浏览器里选元素、写一句话，把 DOM、CSS、截图交给 Cursor / Windsurf / 自家 IDE 里的 AI。只递上下文，不直接改文件，要接开发服务器。
- [React Grab](https://www.mintlify.com/aidenybai/react-grab/introduction)：悬停按 ⌘C 复制元素对应的 React 组件和源码位置。只支持 React 开发环境。
- [Xyle](https://devpost.com/software/xyle)：最像，静态网站上直接改、精确写回源文件、对 AI 开放接口；没查到是否开源。
- Silex、Sitepins：可视化建站 / CMS，不做 AI 交接。

我们的差异：零依赖、纯静态站也能用；小改直接写回不绕 AI；样式只能选设计规范里的值；修改单是落盘文件，带状态和 AI 回复，任何 AI 助手都能接力；同一个工具覆盖「实现之前定骨架」（design-lab）和「实现之后改真实页面」（编辑器）。不和 Stagewise 拼 IDE 集成。

## 4. 前提挑战

1. **核心问题**：两个都要——小改（字、间距）自己在页面上改、直接写回；大改（布局、加删东西）圈出来写意见，AI 精确定位去改。
2. **不做会怎样**：博客里照样能用，但别的项目用不上、没法开源、博客专属规则越堆越多。用户判断值得现在花时间。
3. **复用什么**：不从零写。用户把问题上升了一层——编辑器和 forge-design-lab 本来就是一回事：都是「设计一个页面时，基于这个页面做调整」。工具是独立的本体，Skill 是 forge 流程里调用它的用法，两者不冲突。

现状核对（AI 读代码得出）：

| | 博客编辑器 | forge-design-lab |
|---|---|---|
| 用在 | 实现之后：真实页面 | 实现之前：骨架页面包 |
| 服务 | `编辑服务.py`，Python 标准库，127.0.0.1，端口 4580 | `serve.py`，Python 标准库，127.0.0.1，端口 4577 |
| 前端 | iframe 套真实页面，选中框 + 右侧面板 | 骨架页面 + 右侧七层面板 |
| 保存 | 文字在数据文件里找原句写回，其余进修改单 JSON | 按 id 合并写回 tokens.json 三件套，回执 last-save.json |
| 给 AI 的交接 | 修改单（状态 + 回复） | 回执 + 差异表 |
| 规模 | 约 1100 行 | 引擎和脚本约 3300 行 |
| 在用的地方 | 博客首页；调整单页面的反馈框（`/api/feedback`，项目会话正在用） | info2action、forge-demo-pomodoro 的页面包（各带一份引擎副本 `docs/assets/visuals/_workbench/`） |

和博客绑死的地方：页面登记（还指向旧的 v3-6.html，现行是 v3-7，是个 bug）、字段规则（SKIP_LEAF / SECTION_KEYS / FOLLOW）、editor.js 里写死的 token 名单和颜色中文名、首屏元素编号（对应修改指南）、主题 localStorage 键 `v2-theme`、编辑器自身的界面样式引用博客的 tokens.css、脱敏扫描脚本路径、修改单路径。

## 5. 方案

### 方案 A：只独立编辑器（最小可行）
编辑器单独成工具，和 design-lab 只共用约定（回执格式、只认本机），代码各管各的。1 天。缺点：两套引擎越长越不一样。

### 方案 B：一步到位（用户选定）
一个工具、两种模式，design-lab 的引擎这次就搬进来，Skill 改成调用它。3–5 天。

### 方案 C：分两步合并（AI 当时的推荐）
第一步只独立编辑器但按两种模式设计共用部分，第二步再搬 design-lab。理由是先不动公开的 forge-skills。用户没选。

### 用户决策
选 B。名字 **design-studio**（和 diary-studio 一个系列）。真源是独立仓库 `工具/design-studio/`，forge-skills 安装时自动拉取。

## 6. 方案 B 落地：决策表（第一轮）

| # | 项 | 结论 | 来源 |
|---|---|---|---|
| 1 | 名字 | design-studio（目录、仓库、命令同名） | 用户 |
| 2 | 真源 | `工具/design-studio/`，自己的 git；工作区 `.gitignore` 加 `工具/design-studio/`（同 `工具/info2action/`），根 README 地图加一行 | 用户定位置；git 做法照工作区约定 |
| 3 | 分发 | 单独的公开 GitHub 仓库；forge-skills 的 `install.sh` 发现没装就克隆一份，design-lab 通过环境变量 `DESIGN_STUDIO_HOME`（默认安装位置）找到它。建公开仓库、推送前先问用户 | 用户 |
| 4 | 技术栈 | 零依赖：Python 标准库服务 + 原生 JS，不用构建、不用 npm | 用户（好用好装） |
| 5 | 两种模式 | **页面模式**（现编辑器：改真实页面）+ **骨架模式**（现 design-lab 工作台：在页面包上定 token）。一个服务同时提供两种模式 | 用户（一套东西） |
| 6 | 共用底子 | 本机服务（只绑 127.0.0.1、校验 Host 和 Origin、静态文件含视频分段读取、按 id 合并写回、写回执）；读设计 token 并按字号 / 间距 / 圆角 / 颜色归组；选中框和面板框架；保存后钩子（跑一条命令，失败就撤回写入） | AI 推荐 |
| 7 | 搬进工具的 design-lab 部分 | 运行时：`serve.py`、`assets/workbench/`、`assets/system/`（零件库页面）、`adapt_tokens.py`（读 token）。留在 Skill：SKILL.md 的流程（S0–S5、七层审查）、references，以及生成和校验页面包的脚本（build_tokens、scaffold_pack、merge_saved、verify_pack、extract_system、check_system、compare_impl、render_design_table、build_template）；这些脚本改成调用工具的共用部分，不再各写一份。开发中发现某个脚本两种模式都要用，再挪进工具 | AI 推荐，开发时可调 |
| 8 | 站点配置 | 博客专属的东西全部移进站点配置文件（放在被编辑的网站目录里，暂定 `design-studio.json`）：页面清单和数据文件、修改单目录、token 文件和分组、元素别名与编号、字段连带规则、跳过的字段、主题切换方式、保存后钩子。工具本体不含任何博客内容 | AI 推荐 |
| 9 | 文字写回 | 两种：① JS / JSON 数据文件里原句唯一匹配（现有）；② 新增：HTML 源文件里原句唯一匹配就直接改（纯静态站最常见）。都对不上就进修改单 | 用户（最酷版本①） |
| 10 | 兼容（不能断） | 博客：编辑器网址 `/编辑器/`、端口 4580、`/api/feedback`、修改单位置和格式不变。design-lab：`/api/lab/status`、`/api/lab/packs`、`/api/lab/save`、`/api/lab/system-rev` 不变，老页面包自带的引擎副本照样能存；`serve.py <repo> [--status\|--stop]` 保留成转调工具的薄壳 | AI 推荐（有项目在用） |
| 11 | 界面 | 第一版两个模式的界面可以先各用各的，共用底子先统一；界面统一放在合并之后 | AI 推荐 |
| 12 | 闭环（AI 自动接修改单、页面自动刷新、编辑器里看 AI 回复） | 合并完成后的下一阶段。第一版只加一个「复制给 AI 的指令」按钮（一句话指向修改单路径） | 用户（先去博客化，再加闭环） |
| 13 | 不做 | 浏览器扩展、改线上网页、React 组件定位、多人协作、npm 打包、英文界面（文案集中放，留口子） | AI 推荐 |
| 14 | 顺手修 | 博客编辑器现在登记的是 v3-6，先改成 v3-7（几行，和独立无关，越早越好） | AI 发现 |

## 7. 步骤与时间

| 步 | 做什么 | 验收 | 估时 |
|---|---|---|---|
| 0 | 博客编辑器改指 v3-7 | 打开编辑器看到的是现行首页 | 10 分钟 |
| 1 | 建 `工具/design-studio/`：git、README、站点配置格式；把编辑器搬进来、去博客化；博客改用它（加配置文件、删旧编辑器和编辑服务） | 博客上改一句字写回 site.js、写一条意见进修改单、调整单反馈框照常保存，和改之前一样 | 0.5–1 天 |
| 2 | 搬 design-lab 运行时进来，骨架模式接上；lab 接口兼容；Skill 的 `serve.py` 变薄壳；留在 Skill 的脚本改调工具 | forge-demo-pomodoro 的页面包能打开、能保存、回执正确；info2action 的页面包能打开 | 1–2 天 |
| 3 | forge-skills 接入：`install.sh` 自动拉工具；SKILL.md 工具表更新；在 forge-skills 开 PR | 新机器只装 forge-skills 也能用 design-lab | 0.5 天 |
| 4 | 开源：建公开仓库（先问用户）、README 和演示动图、放进博客工具区、自媒体一篇 | 别人照 README 三步跑起来 | 0.5 天 |
| 5 | 闭环（下一阶段） | 圈一处写意见 → AI 改好 → 页面自动刷新、编辑器里看到回复 | 另估 |

## 7.1 关键假设

- 两边的服务都是 Python 标准库写的，合并不引入依赖。
- design-lab 的老页面包只依赖 `/api/lab/*` 这几个接口，不依赖 `serve.py` 的文件位置。
- 博客的调整单页面只依赖 `/api/feedback` 和编辑服务的网址。
- forge-skills 的用户能接受「安装时自动多克隆一个仓库」。

## 8. 对抗性审查（独立审查者，Claude 已逐条对照代码核实）

**1. 最容易失败：§6 第 7 条的切分线和真实依赖对不上，第 10 条的兼容清单有漏**
- design-lab 的依赖是双向交叉的：`serve.py:45` import 留在 Skill 的 `build_tokens`，每次保存都调 `BT.validate`、`build_js`、`build_css`（284、290 行）；反过来 `extract_system.py:36、39` 依赖要搬走的 `adapt_tokens` 和 `assets/system`；`scaffold_pack.py:65` 默认从 Skill 的 `assets/workbench` 取引擎。
- 老页面包不只依赖 `/api/lab/*`，还要求静态根就是仓库根：info2action 的页面包用 `../../../../frontend-react/...` 引产品样式（如 `<页面包2>/index.html:14`）；`serve.py:84` 写死了 `_workbench/workbench.js` 的位置。
- 博客侧漏列：`posts/_样张/文章页调整台.html:127` 用 `location.port === '4580'` 判断能不能自动保存；`/api/feedback` 的调用方还有 `首页-v3/背景格子调色.html`、`posts/2026-03-29/index.html`、`99-其他/脚本/生成格子调色页.py`；`编辑服务.py:288` 的根地址跳首页被文章页的「回首页」用着。
- 还要跟着改：`evals/forge-design-lab/system/run_system_eval.py:260` 直接启动 `serve.py`；各项目里的引擎副本已和母本分叉（info2action 的 `workbench.css` 第 14 行不同）。

**2. 最大盲区：页面模式目前只适用于博客这一种站**
- 页面模式要求被编辑页面和工具同源（`editor.js:154` 直接读 `frame.contentDocument`，`编辑服务.py:297` 只出静态根里的文件）。info2action 实现后是 Vite + React，打不开；开源目标「用 AI 建站的人」多数用框架。
- HTML 原句写回有坑：博客 v3-7 的正文是脚本拼出来的（`app.innerHTML = …`），「原句唯一匹配」会命中脚本和注释里的字——「小工具」在全文只出现 1 次，就在第 136 行的注释里。

**3. 只有一半时间：砍掉步骤 2、3**
- 第 11 条已定两种模式界面先各用各的，合并后用户看到的东西不变，真正共用的只有 Host 校验和静态服务几十行。
- `install.sh` 现在只建软链、不联网；加自动克隆会带来网络依赖和两个仓库的版本错位，`--status`、`--uninstall` 也管不到。
- 审查者建议：退回「分两步」，这次只做步骤 0、1 和「复制给 AI 的指令」按钮；开源等页面模式在第二个站跑通后再做。

**Claude 的应对建议（待用户定）**
- 不管走哪条，开工第一件事是「依赖图 + 全部调用方清单 + 回归脚本」，兼容清单以它为准，不靠记忆。
- 合并 design-lab 时 `build_tokens` 必须跟服务一起进工具；静态根保持「仓库根」；`serve.py` 薄壳保留到 evals、examples 都改完。
- 页面模式补一个**代理方式**：`design-studio serve --proxy http://localhost:5173` 把框架站的开发服务器转到同源，圈选、写意见、调样式都能用；文字写不回 JSX，就全部进修改单交给 AI——这正好是「大改交 AI」那条路。决策表加「页面模式支持哪类站」：纯静态站 + 数据文件（能写回）；框架站走代理（只进修改单）。
- HTML 写回只认 `<script>`、`<style>`、注释以外的文本节点；写回后重载页面比对，对不上就撤回、改进修改单。
- `install.sh` 不自动克隆，改成「检测到没装就打印一条安装命令」，避免网络依赖和版本错位。

## 9. 第二轮：审查后修正（2026-10-23，用户确认）

用户看完 §8 后选「分两步：这次先做页面模式」。核心不变：design-studio 是本体，最终有页面模式 + 骨架模式，design-lab 调用它。

⚠️ 推翻 §5「用户决策：选 B」中「这次就把 design-lab 引擎搬进来」→ 改为下一轮再搬。
⚠️ 推翻 §6 第 3 条「forge-skills 的 install.sh 自动克隆」→ 这一轮不动公开的 forge-skills；分发方式下一轮搬 design-lab 时再定（审查建议改成「没装就打印安装命令」）。

| # | 项 | 结论 |
|---|---|---|
| 2.1 | 本轮范围 | 页面模式：编辑器独立成 design-studio，博客改用它；加代理方式；加「复制给 AI 的指令」按钮。不碰 design-lab、forge-skills |
| 2.2 | 接口按两种模式设计 | 服务的路由、回执、站点配置、读 token 的模块按「以后骨架模式也挂进来」设计：静态根可以是仓库根；`/api/lab/*` 的路由位置先留出来；`build_tokens` 以后跟服务一起进工具 |
| 2.3 | 兼容清单 | 以开工第一步 grep 出的依赖图为准，不靠记忆。已知必须不断的：编辑器网址 `/编辑器/`、端口 4580（文章页调整台按端口判断能否保存）、`/api/feedback`（调整单、背景格子调色页、文章页、格子调色页生成脚本都在用）、`/api/sheet`、根地址跳首页、修改单和反馈 JSON 的位置与格式 |
| 2.4 | 回归脚本 | 换掉旧编辑服务之前先写好：改一句字写回 site.js、写一条意见进修改单、调整单反馈保存和读回、文章页调整台在 4580 能存、根地址跳首页；旧服务和新工具各跑一遍，结果一致才切换 |
| 2.5 | 页面模式支持哪类站 | 纯静态站 + 数据文件：能写回。框架站（Vite / React 等）：`design-studio serve --proxy <开发服务器地址>` 转成同源，圈选、写意见、调样式都能用，文字写不回 JSX 就全部进修改单交给 AI |
| 2.6 | HTML 写回 | 只认 `<script>`、`<style>`、注释以外的文本节点里唯一出现的原句；写回后重载页面比对，对不上就撤回、改进修改单 |
| 2.7 | 第二个站 | 用 info2action（Vite + React）走代理方式跑通一次，证明不只适用于博客 |
| 2.8 | 本轮步骤 | ① 博客编辑器改指 v3-7 ② 依赖图 + 回归脚本 ③ 建仓库（git、README、站点配置格式、工作区 .gitignore 和根 README 地图）④ 搬编辑器、去博客化 ⑤ 博客切换（回归全过才删旧文件；切换前在会话占用表登记，提醒在用调整单的会话）⑥ 代理方式 + info2action 试跑 ⑦「复制给 AI 的指令」按钮 |
| 2.9 | 下一轮 | 搬 design-lab 运行时（`serve.py`、`build_tokens`、`adapt_tokens`、`assets/workbench`、`assets/system`），老页面包兼容，`serve.py` 留薄壳到 evals 和 examples 改完；forge-skills 接入方式；开源和自媒体；闭环 |
| 2.10 | 估时 | 本轮 1.5–2 天 |

## 10. 下一步

用户确认后：构建模式默认走 forge-dev 直接开发；实现可以派 codex（codex-delegate），Claude 验收。

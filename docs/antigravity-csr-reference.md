# Antigravity 进阶参考（手动安装、演练与排错）

日常使用请先看[Mac 一条命令安装与出稿](antigravity-csr-guide.md)。第 1、2 节供选择手动部署的团队使用。

医学写手在 Antigravity 中打开已有的研究资料夹，以该目录作为项目根目录。方案、统计分析计划（SAP）、逐例数据、定稿统计表和其他原件可保留原有的子目录。用 `/goal` 启动后，智能体调用本地工具生成 Word 内部审阅初稿和集中审阅清单。医学、统计负责人在同一份文件中答复，工具据此重算受影响的结果，并把变更写入新版正文、概要和附录。

本教程依据团队的《Antigravity 斜杠命令与 CSR 撰写实操指南》整理，命令名称与支持端已对照 Google 官方文档核实。

| 你现在要做什么 | 直接打开 |
|---|---|
| 第一次使用，验证本机能出稿 | [安装](#install) → [挂载技能](#workspace) → [用教学资料生成两版 Word](#demo) |
| 用一个真实研究资料夹开写 | [首轮提示词与资料要求](#draft) → [怎么看结果与对账](#outputs) |
| 已收到负责人意见 | [一次答复和回写](#answers) |
| 查某个斜杠命令或处理运行问题 | [九个命令速查](#commands) · [故障排查](#troubleshooting) |

<a id="install"></a>

## 1. 安装工具，确认能从研究目录调用

准备 Git、Python 3.10 或以上版本，以及团队已获准使用的 Antigravity 客户端。以下命令在终端中运行。Antigravity 中以斜杠开头的指令则发到智能体的**对话输入框**。工具运行、原件归档和负责人答复均可使用本地文件；涉及真实研究时，按团队批准的 Antigravity 账号、数据使用范围和工作区权限操作。[Antigravity 官方入门](https://antigravity.google/docs/getting-started)

**macOS / Linux**

```bash
git clone https://github.com/huanglu1987/csr-writer.git "$HOME/csr-writer"
python3 -m venv "$HOME/.venvs/csr-writer"
"$HOME/.venvs/csr-writer/bin/python" -m pip install -e "$HOME/csr-writer[source-office,models,visual]"
"$HOME/.venvs/csr-writer/bin/python" -m csr_writer draft-project --help
```

**Windows PowerShell**，以下示例使用安装了 Python 启动器的环境。

```powershell
git clone https://github.com/huanglu1987/csr-writer.git "$HOME/csr-writer"
py -3 -m venv "$HOME/.venvs/csr-writer"
$CsrPython = "$HOME/.venvs/csr-writer/Scripts/python.exe"
& $CsrPython -m pip install -e "$HOME/csr-writer[source-office,models,visual]"
& $CsrPython -m csr_writer draft-project --help
```

帮助页应显示 `--input-dir`、`--output-dir`、`--matches`、`--sap-reading` 和 `--answers`。`source-office` 用于 Excel、带文本层的 PDF 等原件，`models` 安装数值计算依赖，`visual` 安装版式预检的 Python 依赖。常规 ANCOVA 的独立 R 复算另需本机 `Rscript`，可用 `Rscript --version` 检查。需要渲染检查分页时再安装 LibreOffice。每种统计方法的实际复算覆盖以本次输出为准。[统计能力和输入规则](智能体起草指南.md)

**已有旧版**时，在工具仓库运行 `git status --short --branch` 核对当前分支和本地改动。在干净的 `master` 上运行 `git pull --ff-only origin master`，再用上面同一解释器重跑安装与帮助命令。如有团队自己的修改，保留该目录，可以另行克隆验证新版。记录 `git rev-parse HEAD` 的结果，方便复现一版报告。

<a id="workspace"></a>

## 2. 给研究建工作区，让 Antigravity 找到技能

工具装一次，各研究共用。直接打开已有研究资料夹作为工作区。下面用新建的 `CSR-教学` 演示手动部署和教学资料复制，实际研究可以沿用现有目录。

```text
用户主目录/
  csr-writer/                     工具仓库
  CSR-教学/                       在 Antigravity 中打开这一层
    .agents/skills/csr-writer/    指向仓库的 skills/csr-writer
    研究方案.md、给药.csv 等       原件也可存放在各级子目录
  CSR-教学_审阅/                  模拟答复或项目审阅记录
  CSR-教学_CSR初稿/               首次出稿时由工具创建
```

**macOS / Linux** 在新建的研究工作区挂载技能，并为第一次练习复制公开的合成原件。

```bash
study="$HOME/CSR-教学"
mkdir -p "$study/.agents/skills" "$HOME/CSR-教学_审阅"
ln -s "$HOME/csr-writer/skills/csr-writer" "$study/.agents/skills/csr-writer"
cp -R "$HOME/csr-writer/examples/antigravity-demo/输入资料/." "$study/"
cp "$HOME/csr-writer/examples/antigravity-demo/模拟答复.json" "$HOME/CSR-教学_审阅/"
cd "$study"
```

**Windows PowerShell** 可用目录联接指向同一份技能。

```powershell
$Study = "$HOME/CSR-教学"
New-Item -ItemType Directory -Force -Path "$Study/.agents/skills","$HOME/CSR-教学_审阅" | Out-Null
New-Item -ItemType Junction -Path "$Study/.agents/skills/csr-writer" -Target "$HOME/csr-writer/skills/csr-writer"
Copy-Item "$HOME/csr-writer/examples/antigravity-demo/输入资料/*" -Destination $Study -Recurse
Copy-Item "$HOME/csr-writer/examples/antigravity-demo/模拟答复.json" -Destination "$HOME/CSR-教学_审阅/"
Set-Location $Study
```

已有挂载时，检查其中能读到 `SKILL.md`。新项目只需重复建目录和挂载两步。团队使用工作区 `AGENTS.md` 时，可以把下面一段合并进去，保留原有项目要求。

> 撰写 CSR 时先读取工具仓库根目录的 AGENTS.md 和 skills/csr-writer/SKILL.md。使用安装了 csr-writer 的虚拟环境运行 draft-project。以当前打开的研究资料夹根目录作为输入，解读、答复和各版输出保存在原件目录之外。用当前项目记录确认分析人群、结果和医学决定，集中报告待判断项。

在 Antigravity 打开这个研究工作区或把该目录加入当前 Project，新建会话，在输入框输入 `/` 查找 `csr-writer`。也可直接发送“读取本工作区的 csr-writer 技能，检查能否用已安装的解释器显示 draft-project 帮助”。使用 CLI 时可在项目目录启动 `agy`，新加技能后可运行 `/skills reload`；独立 IDE 可在智能体侧栏的 Customizations 中查看技能。若工作区限制访问外部技能链接，可在对话中指定已经安装的 Python 和工具路径，或在本机终端按第 3 节调用。[官方技能发现与调用说明](https://antigravity.google/docs/skills)

<a id="demo"></a>

## 3. 用六名合成受试者跑通首稿和回写

**Mac 一键安装用户**可以新建空项目，在 Antigravity 对话框发送下面这句。工具已随安装附带模拟资料。

```text
/csr-writer 请从安装目录 current/tool/examples/antigravity-demo 把“输入资料”中的原件复制到当前空项目根目录，把“模拟答复.json”另存到项目旁的审阅目录。以当前项目根目录生成首稿，再按模拟答复另出新版，把两版 Word、清单和受影响的章节发给我。
```

以下路径和命令供手动部署用户使用。

在当前研究会话直接发送以下提示词。工具读取当前打开的目录，另在同级目录保存输出。

```text
/goal 使用 csr-writer 技能，以当前项目根目录作为输入，运行 draft-project，在同级目录生成首稿。打开生成的 Word、集中审阅清单和逐表对账，告诉我哪张用了定稿表、哪张有差异、差异在哪一格。先交付已有依据的内容；把需要负责人判断的事集中列出。
```

首轮应看到两个可手工复查的事实：

- 两组各随机 3 人，处置表与逐例数据一致，工具沿用定稿表。
- 暴露表的乙组列在前、甲组列在后。定稿表「暴露!C5」将甲组总用药写为 4 人日，逐例记录计算为 3。打开 `给药.csv`，第 2 行和第 8 行是 D01 同一天的两次半剂量。SAP 规定同一给药日计一天，首稿暂缓引用这张表。

然后在同一会话中发送：

```text
/goal 本次是 AG-DEMO-01 软件演练。读取项目旁 CSR-教学_审阅 中的模拟答复.json，用当前项目根目录和 --answers 另存新版。按负责人决定记录核对实际采用的两项模拟答复，并从 Word 的正文、概要和安全性附表核对暴露人日及待定相关性人数。列出仍未答复的事项。
```

**验收结果**为两项模拟答复已采用，甲组暴露为 3 人日，甲组相关性待定 TEAE 受试者由 1/3 变为 0/3。第二版有 7 张统计附表，仍有医学结论、统计结果和概要三项待决定。示例仅放入教学所需的原件，因此还会列出伦理、附件等资料缺口，主要疗效描述表的独立复算状态为“未覆盖”。[合成输入资料](../examples/antigravity-demo/输入资料) · [本例专用模拟答复](../examples/antigravity-demo/模拟答复.json)

**终端等价命令。** 下面两条可用于安装验证或定位客户端问题。从研究资料夹运行。默认生成同级目录，再次运行会自动编号。

```bash
cd "$HOME/CSR-教学"
csr_python="$HOME/.venvs/csr-writer/bin/python"
"$csr_python" -m csr_writer draft-project --input-dir "."
"$csr_python" -m csr_writer draft-project --input-dir "." --answers "../CSR-教学_审阅/模拟答复.json"
```

```powershell
Set-Location "$HOME/CSR-教学"
$CsrPython = "$HOME/.venvs/csr-writer/Scripts/python.exe"
& $CsrPython -m csr_writer draft-project --input-dir "."
& $CsrPython -m csr_writer draft-project --input-dir "." --answers "../CSR-教学_审阅/模拟答复.json"
```

**一条命令验收公开仓库安装。** 在工具仓库根目录，使用第 1 节的虚拟环境解释器运行下列命令。它会在临时目录内执行两次正式入口，核对定稿表选择、差异单元格及存盘 Word 中的正文、概要和附录。通过时显示 `OK`。维护者全量测试另需开发工作区上一级的四份锁定规格原件与两份历史 RTF 夹具，单独克隆公开仓库时使用本教学验收。

```bash
cd "$HOME/csr-writer"
"$HOME/.venvs/csr-writer/bin/python" -m unittest -v tests.test_antigravity_quickstart
```

```powershell
Set-Location "$HOME/csr-writer"
& "$HOME/.venvs/csr-writer/Scripts/python.exe" -m unittest -v tests.test_antigravity_quickstart
```

<a id="draft"></a>

## 4. 实际研究从资料夹生成第一版

实际项目直接在 Antigravity 打开该研究现有的资料文件夹，保留原件的名称和层级。输入目录中只放该研究的资料。上轮生成的 Word、统计复核表、答复 JSON 和历史输出均放在输入之外。不同版本保留原文件名、研究编号、版本、生效或定稿状态和数据截止日。

| 手头的资料 | 工具如何使用 | 提交前值得检查的地方 |
|---|---|---|
| 有效方案、修订案和自然语言 SAP | 匹配研究编号，提取终点、访视窗、分组、缺失处理及统计方法，记录原文位置 | 现行版本能否唯一确定，方案与 SAP 的主要终点是否一致 |
| 部分或全部定稿 TFL | 按表义、角色、分析集、行列、版本及截止日选择和对账。工作簿可有多个工作表 | 汇总表应标明研究、表题、定稿状态、分析集、单位和组别。表号或列顺序可以不同 |
| 逐例数据 | 对受支持且 SAP 已规定的方法补算缺表，保留推导过程和原始行位置 | CSV、TSV 或原生 XLSX 需有受试者标识、随机或实际组别、访视、日期等相应域。工具不能从列名推断缺失的策略 |
| 偏离、医学记录、签署资料、附录登记 | 分别用于偏离概述、事件叙述、已有医学意见及附件状态 | 严重事件的经过、转归和相关性应能回到相应记录。扫描件需要核对原页 |

**真实项目首轮提示词**

```text
/goal 用 csr-writer 读取当前打开的研究资料夹及其各级子目录，在同级目录生成 Word 首稿。先识别有效方案、SAP、已有定稿表和逐例域，逐表保留可用定稿、补算可执行的缺表，输出独立复算和 Word 实际显示的对账结果。若有待智能体匹配或解读 SAP 的任务，请依据原件处理后在新目录续跑。交付本轮 Word、审阅清单和输出路径；分别说明统计差异、缺资料的小节、未覆盖的复算和须由负责人决定的事项。
```

当输出有 `待智能体匹配.json` 或 `待智能体解读SAP.json` 时，先让智能体依任务文件定位原件并准备结构化答复。表题匹配使用 `--matches`，SAP 解读使用 `--sap-reading`。保留原句、位置和依据；只剩多种实质解释时，交统计负责人。续跑和负责人回写时仍需传入本轮依赖的解读或匹配答复。省略 `--output-dir` 时，工具在输入资料夹旁生成目录，重复运行自动编号。显式指定时，输出目录须是尚不存在的新目录。[匹配和解读文件说明](智能体起草指南.md)

<a id="outputs"></a>

## 5. 拿到 Word 后先读哪些文件

| 打开顺序 | 文件 | 要核对的问题 |
|---|---|---|
| 1 | `CSR内部审阅初稿.docx`、`集中审阅清单.md` | 正文、概要和附录有哪些结果，哪些小节缺资料，哪些事项有真实决策责任人 |
| 2 | `draft-report.json` | 查看 `selected_sources`、`source_gaps`、`source_followup`、`human_decisions`，区分有效来源、资料缺口、技术问题和人工判断 |
| 3 | `统计分析/逐表对账.json`、`统计分析/分析留痕.json` | 某张表采用哪个工作表，差异在哪个单元格，计算规则来自 SAP 哪段，能否查到逐例记录 |
| 4 | `统计分析/独立复算.json`、`统计分析/Word单元格复核.json` | 哪些指标经过独立计算，哪个 Word 附表单元格与采用来源一致。“未覆盖”应在交付说明中保留 |
| 5 | `集中答复模板.json`、`负责人决定.json`、`更新定位.json` | 每个问题依赖哪些证据，谁回答了什么，以及答复实际写入哪些章节 |

统计和答复文件随本轮形成的统计结果产生。只交定稿表、没有可用逐例域的项目，先用 `draft-report.json` 和审阅清单确认实际输出。读表时区分“Word 抄对了采用表”和“从原始资料独立算出相同结果”，两者各有检查记录。

<a id="answers"></a>

## 6. 让负责人一次答复，保留首稿并重建 Word

1. 把输出目录里的 `集中答复模板.json` 另存到资料夹旁的审阅目录，命名为 `负责人答复.json`。保持 `study`、`mode`、每项的 `id`、`basis`、`role` 和可选值。项目使用本轮生成的模板。
2. 医学、统计负责人可在一张清单上一次回答。由智能体据实际答复填写 `reviewer`、`date`（YYYY-MM-DD）、`choice` 及 `statement`（理由）。尚未决定的条目保留空白。总体医学结论如含 `applies_if`，逐项填入它所依赖的实际选择。
3. 用 `--answers`、同一批原件和需要沿用的解读重新运行，另存新版。检查 `负责人决定.json` 的 `answered`、`open`、`deferred`、`stale`，并在 `更新定位.json` 与实际 Word 中核对章节。以后更换数据时继续传入需要沿用的完整答复文件；依据改变的事项会重新打开。
4. 统计输出签认、最终概要核准和真实报告签署按项目流程完成。`draft-project --answers` 生成的文件名仍是 `CSR内部审阅初稿.docx`。正式净版属于仓库另设的受控合卷流程，可查 `python -m csr_writer compose-csr --help` 及项目规定的批准记录。

**集中收意见，可以直接发这段普通消息**

```text
请依据刚生成的首稿目录里的集中审阅清单.md 和集中答复模板，按负责人整理一张可一次回复的表。每行列出事项 ID、原始证据位置、待回答问题、可选决定和影响章节。已有原件可以澄清的问题请先核回原件。收到实际回复后在资料夹旁的审阅目录准备负责人答复.json，保留未答复项。
```

**收到答复后的执行提示词**

```text
/goal 使用刚整理的负责人答复.json 和当前研究资料夹，在新的同级目录生成修订稿，沿用本稿使用的 SAP 解读与表题匹配文件。回读 Word，列出一次答复前后正文、概要、附录和受影响统计表的变化，并交付新版 Word、剩余决定和仍缺的原件。
```

<a id="commands"></a>

## 7. 附件里的九个命令，哪些用法一样？

九个命令与团队附件《Antigravity 斜杠命令与 CSR 撰写实操指南》第 1 节逐一对应。附件把 `/goal`、`/compact`、`/grill-me`、`/learn` 串为四步。本教程保留这些用途，日常只需用 `/goal` 起草，其余按项目需要选。安装后出现的 `/csr-writer` 是本工具提供的撰写技能。命令可用端及付费条件可查 [Antigravity 官方命令表](https://antigravity.google/docs/slash-commands)。

| 命令 | 附件的分工与限制 | 本教程的用法与可复制示例 |
|---|---|---|
| `/goal` | 起草主命令。附件提到约 120 个撰写单元，要求明确交付文件、校验结果和资料缺口。 | **用途一致。** 以当前研究实际生成的 Word、逐表核查记录和清单验收。章节数按研究适用性确定，能写到什么程度取决于原件。`/goal 用 csr-writer 读取当前项目根目录及子目录，生成 Word 初稿，核对统计表，一次列齐资料缺口和待决事项。` |
| `/plan` | 改工具或复杂项目开写前先规划，盘点方案、SAP、统计表和章节。熟悉的常规项目可跳过。 | **用途一致。** `/plan 盘点当前研究资料夹及子目录，列出有效方案和 SAP、已有统计表、主要结果涉及的章节及缺口。` |
| `/teamwork` | 大型 II/III 期项目分章并行起草。附件举例为 50 张以上统计表。先固定研究事实和采用表，再分工，由主智能体合卷。 | **团队示例采用分章复核。** 当前单资料夹入口负责出稿，本次演练验证到单资料夹出稿，附件所述的多智能体并行起草与主智能体合卷仍待单独验证。“50 张”是附件举例。此命令需支持该功能的付费方案。`/teamwork 依据同一版首稿和采用表，分头核查方法、疗效、安全性章节，交回原表位置和差异，由主智能体汇总。` |
| `/boost` | 深审第 13 章、获益风险和不一致的敏感性结果，常规模板节可跳过。附件后文还建议深审后生成净版。 | **保留深审，医学定论由负责人决定。** 当前起草入口交付内部审阅稿，正式净版另有合卷流程。此命令需支持该功能的付费方案。`/boost 对照主要分析与敏感性分析的原表，核查第 13 章对差异的解释，标出需医学负责人决定的措辞。` |
| `/compact` | 数据入包或首稿已保存时，在阶段切换点压缩对话，避免在单节撰写中途执行。 | **用途一致。** 适合已有落盘成果的长对话，官方列出 App 和 CLI 支持。`/compact 保留当前研究资料夹、已采用表、最新 Word 和审阅清单的路径，以及待决事项编号。` |
| `/grill-me` | 首稿后按清单逐条问负责人，附件建议确认后回填并出净版。 | **保留逐项澄清，日常优先一次收齐回复。** 此命令每次问一个问题。意见明确时直接按[集中答复示例](antigravity-csr-guide.md#answers)更新内部审阅稿。`/grill-me 请就暴露表的差异逐问澄清，先列证据，只记录负责人明确给出的决定。` |
| `/learn` | 审阅完成后沉淀可复用的术语、句式、表注，项目数值留在本研究。 | **用途一致，补充保存步骤。** 先生成提案，经团队审阅后保存。`/learn 请提议一条下个项目也适用的表注规则，用通用例句说明，供团队审阅。` |
| `/browser` | 第 15 章需要核查公开文献、指南时使用，常规本地起草可跳过。 | **用途一致。** `/browser 请到出版者官网核对第 15 章这条公开文献的题名、年份和 DOI，并给出出处。` |
| `/schedule` | 锁库前按计划检查统计表更新并重跑预检，常规单次出稿可跳过。 | **用途一致。** 写明时间、检查对象和重跑条件。以下只是安排一次的示例。`/schedule 明天北京时间上午九点检查本研究统计表是否更新。有更新就在新的同级目录重跑初稿，列出与上版的差异。` |

客户端输入 `/` 可查看当前账号提供的命令。有关规则提案和定时唤醒，见[规则保存过程](https://antigravity.google/docs/commands/learn/)和[定时指令说明](https://antigravity.google/docs/commands/schedule)。

<a id="troubleshooting"></a>

## 8. 常见问题

| 现象 | 如何定位与处理 |
|---|---|
| 按 Mac 一键教程安装后，输入 `/` 看不到技能或仍加载旧版 | 新开会话，请智能体检查全局入口 `~/.gemini/config/skills/csr-writer/SKILL.md`。旧项目也可能带有工作区同名技能，可先在新的研究项目打开全局技能。需要重新安装时，再发一次首页的安装指令 |
| 手动部署后，输入 `/` 看不到项目技能 | 确认当前打开研究根目录；检查 `.agents/skills/csr-writer/SKILL.md` 和链接目标。CLI 可刷新技能，其他客户端可新开会话，再试输入技能名 |
| 报找不到模块、模板或旧参数 | 用第 1 节的虚拟环境解释器运行 `-m csr_writer draft-project --help`。对当前仓库重新执行可编辑安装，并保留仓库中的 `skills/csr-writer/references` 与 `assets` |
| 指定的输出目录已存在 | 保留旧稿，给新一版取新目录名，或省略输出参数让工具在输入目录旁自动编号 |
| 有定稿表仍未被采用 | 查逐表对账及审阅清单。核对研究编号、状态、版本、截止日、分析集、行名、组名和原始单元格。来源冲突由统计负责人裁定 |
| 只看到部分资料或模型未执行 | 查有效 SAP 的原文定位、逐例列名、日期、访视和缺失定义。扫描件须回到原页，陌生 SAP 表述按待解读文件续跑。对于尚未实现的方法，请提供相应定稿统计结果 |
| 重新出稿后某项仍待决定 | 查实际采用的答复、角色、`basis`、`applies_if` 及未匹配答复。新证据改变决定时，以新版模板重新核对该项 |
| Word 统计数字正确但分页有问题 | 在 Word 更新目录和交叉引用域，查看宽表、分页和页尾标题。要做本地预检，可运行 `python -m csr_writer visual-qc --help` 查看渲染参数 |

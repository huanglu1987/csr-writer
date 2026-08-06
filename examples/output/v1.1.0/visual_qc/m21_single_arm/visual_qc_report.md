# CSR 视觉检查报告

> 本检查基于 LibreOffice 渲染，与 Microsoft Word 的分页和字体回退存在差异。 本检查是前置筛查，不替代在 Word 中更新域后的人工视觉复核。

- 状态：**blocked**
- 视觉闸门：**blocked**
- 既有发布判定受影响：**否**
- 输入文件：`M21_M20_unfamiliar.clean.docx`
- 输入 SHA-256：`5fd7b9e3622c0e4643f22b797be3ddd55faf6d084729fab52ab10679c7ee1c9a`
- 配置 SHA-256：`fcfb0246089dc2984eacca06fa52f1d93a9d4d3ef13a2775a6df28af76d8046d`

## 汇总

- 页数：34
- A 类：5（可阻断视觉预检）
- B 类：32（仅警告）

## 逐项问题

| 分级 | 页码 | 代码 | 说明 | 证据 |
|---|---:|---|---|---|
| A | 3 | `A-FIELD-PAGE-NUMBER-NOT-EXPANDED` | 页码或总页数字段在渲染结果中为空。 | pages=[3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34] |
| A | 4 | `A-FIELD-DIRECTORY-NOT-EXPANDED` | 3.1 正文章节目录 预期至少 132 项，但渲染目录区没有可见条目。 | 3.1 正文章节目录 |
| A | 4 | `A-FIELD-DIRECTORY-NOT-EXPANDED` | 3.4 第 14 章表图目录 预期至少 10 项，但渲染目录区没有可见条目。 | 3.4 第 14 章表图目录 |
| A | 4 | `A-FIELD-DIRECTORY-NOT-EXPANDED` | 3.5 附录目录 预期至少 24 项，但渲染目录区没有可见条目。 | 3.5 附录目录 |
| A | 4 | `A-FIELD-DIRECTORY-NOT-EXPANDED` | 3.6 单独递交的支持性报告目录 预期至少 1 项，但渲染目录区没有可见条目。 | 3.6 单独递交的支持性报告目录 |
| B | 1 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 究 |
| B | 2 | `B-TABLE-HEADER-NOT-REPEATED` | 检测到疑似跨页表延续，但下一页未识别到相同表头。 | header_similarity=0.174 |
| B | 11 | `B-HEADING-ORPHAN` | 标题位于页尾，页内没有后续正文。 | 9.5.3 主要终点 |
| B | 14 | `B-BODY-OVERFLOW` | 非页眉页脚文本块越出当前节正文带。 | 作为独立确证性分析节呈现；适用方法须在探索性统计分析内容中依据 SAP |
| B | 15 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 架。 |
| B | 15 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 计。 |
| B | 24 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 是 |
| B | 24 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 是 |
| B | 24 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 是 |
| B | 25 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 是 |
| B | 25 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 否 |
| B | 25 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 计。 |
| B | 26 | `B-TABLE-HEADER-NOT-REPEATED` | 检测到疑似跨页表延续，但下一页未识别到相同表头。 | header_similarity=0.455 |
| B | 27 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 者） |
| B | 27 | `B-TABLE-HEADER-NOT-REPEATED` | 检测到疑似跨页表延续，但下一页未识别到相同表头。 | header_similarity=0.622 |
| B | 28 | `B-HEADING-ORPHAN` | 标题位于页尾，页内没有后续正文。 | 16.1.4 研究者及 否 TMF 研究者个 |
| B | 28 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 否 |
| B | 28 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 否 |
| B | 28 | `B-TABLE-HEADER-NOT-REPEATED` | 检测到疑似跨页表延续，但下一页未识别到相同表头。 | header_similarity=0.500 |
| B | 29 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 否 |
| B | 29 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 否 |
| B | 29 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 书。 |
| B | 29 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 否 |
| B | 29 | `B-TABLE-HEADER-NOT-REPEATED` | 检测到疑似跨页表延续，但下一页未识别到相同表头。 | header_similarity=0.100 |
| B | 30 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 用） |
| B | 30 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 法。 |
| B | 30 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 否 |
| B | 30 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 否 |
| B | 30 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 否 |
| B | 30 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 表） |
| B | 30 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 表。 |
| B | 30 | `B-TABLE-HEADER-NOT-REPEATED` | 检测到疑似跨页表延续，但下一页未识别到相同表头。 | header_similarity=0.171 |

## 缩略图墙

- `M21_M20_unfamiliar.clean.contact_sheet_001.png`：页 1–20
- `M21_M20_unfamiliar.clean.contact_sheet_002.png`：页 21–34

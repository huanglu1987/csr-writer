# CSR 视觉检查报告

> 本检查基于 LibreOffice 渲染，与 Microsoft Word 的分页和字体回退存在差异。 本检查是前置筛查，不替代在 Word 中更新域后的人工视觉复核。

- 状态：**pass_with_warnings**
- 视觉闸门：**pass**
- 既有发布判定受影响：**否**
- 输入文件：`visual_layout_holdout.deidentified.docx`
- 输入 SHA-256：`ba4fcde4391b69dcf487b31e6c21058be8b8aca4d9de8a86e9c164cdc1307518`
- 配置 SHA-256：`fcfb0246089dc2984eacca06fa52f1d93a9d4d3ef13a2775a6df28af76d8046d`

## 汇总

- 页数：7
- A 类：0（可阻断视觉预检）
- B 类：4（仅警告）

## 逐项问题

| 分级 | 页码 | 代码 | 说明 | 证据 |
|---|---:|---|---|---|
| B | 3 | `B-SINGLE-CJK-LINE` | 检测到段末单字成行。 | 题。 |
| B | 4 | `B-BODY-OVERFLOW` | 非页眉页脚文本块越出当前节正文带。 | 合法版式留出集｜合成数据 |
| B | 4 | `B-BODY-OVERFLOW` | 非页眉页脚文本块越出当前节正文带。 | 版式留出集｜仅用于视觉检查标定 |
| B | 6 | `B-TABLE-HEADER-NOT-REPEATED` | 检测到疑似跨页表延续，但下一页未识别到相同表头。 | header_similarity=0.240 |

## 缩略图墙

- `visual_layout_holdout.deidentified.contact_sheet_001.png`：页 1–7

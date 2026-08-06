# CSR 视觉检查报告

> 本检查基于 LibreOffice 渲染，与 Microsoft Word 的分页和字体回退存在差异。 本检查是前置筛查，不替代在 Word 中更新域后的人工视觉复核。

- 状态：**pass_with_warnings**
- 视觉闸门：**pass**
- 既有发布判定受影响：**否**
- 输入文件：`05_long_table_repeated_header.docx`
- 输入 SHA-256：`f255f8b63e1fd9198f5d66b0f79f22fbf170068232e986689c4ba398b99193c5`
- 配置 SHA-256：`fcfb0246089dc2984eacca06fa52f1d93a9d4d3ef13a2775a6df28af76d8046d`

## 汇总

- 页数：3
- A 类：0（可阻断视觉预检）
- B 类：1（仅警告）

## 逐项问题

| 分级 | 页码 | 代码 | 说明 | 证据 |
|---|---:|---|---|---|
| B | 2 | `B-TABLE-HEADER-NOT-REPEATED` | 检测到疑似跨页表延续，但下一页未识别到相同表头。 | header_similarity=0.250 |

## 缩略图墙

- `05_long_table_repeated_header.contact_sheet_001.png`：页 1–3

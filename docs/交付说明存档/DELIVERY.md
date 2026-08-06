# M5 交付说明

## 范围与结论

本轮完成真实 DOCX TFL shell 的端到端结构验收，并停在 M5：

1. 新增取值占位符之外的表/图/清单交叉引用占位符，装配为 Word REF 域和书签。
2. 在真实 shell 上完成对象级自动提议、置信度分层、批量确认、版本冻结及只读哈希复核。
3. 从冻结 DOCX shell 结构向两个脱敏合成 RTF 结果表套值；结构漂移硬失败。
4. 将确定性分节写手扩展至现有 playbook 对应节，并补 §10.1、§11.2 的最小 playbook。
5. 跑通 annotated/clean/Synopsis、正反向追溯、五组语义和 QC 权限护栏。
6. 增加 Codex `AGENTS.md` 入口与“删除全部平台文档后运行时仍通过”的回归测试。

未接真实 LLM，未实现 BE 统计内容，未产出完整 CSR 终稿。所有结果值均为合成值；真实来源只在外部目录原位读取。

## 1. 表号交叉引用

### 合同

- 结果值：`{{T14.2.1|POP=...|ROW=...|COL=...}}`
- 交叉引用：`{{@ref:T14.2.1}}`
- 中文映射：`表14.2.1 → T14.2.1`、`图14.2.4.1 → F14.2.4.1`、`清单1.1 → L1.1`

交叉引用不参加“每个已审取值绑定用且仅用一次”计数。A 守卫要求实体存在于本节 `binding_manifest` 或冻结 `tfl_map`；C 守卫只放行已绑定的标准 token，正文写死表号仍会命中裸数字。装配时生成 `REF _Ref_Tab_... \h` 域及唯一书签，并在 OOXML 层校验每个域都有且仅有一个目标。

既有 §11.4 与 §11.A 草稿、resolve 成品和 annotated/clean/Synopsis 已补交叉引用。证据见：

- `sections/11.4.md`、`sections/11.A.md`
- `sections/resolved/`
- `examples/output/m4/m4_assembly_report.json`
- `examples/output/m5/cross_reference_existing_sections.json`

## 2. 真实 shell 实测

### 规模与人机工效

本次实际读取结果：

| 指标 | 实测 |
|---|---:|
| 正文物理表对象 | 175 |
| 全故事表对象 | 179 |
| 全故事单元格 | 7424 |
| 唯一 TFL 实体 | 110 |
| 对象级自动提议 | 175/175（100% 覆盖） |
| 高置信实体 | 30/110（27.27%） |
| 进入批量复核的实体 | 80 |
| 人工批量动作 | 3 |
| 本次运行耗时 | 2.922 秒 |

“100%”是对象提议覆盖率，不是语义准确率。80 个低/中置信实体按同一处置策略合并为 3 个动作：1 个多对象连续表、26 个需位置消歧的行路径、53 个 shell 无可用行列网格。后 53 个只登记表级元数据，确认动作不会将其伪装成已有 cell key。

批量确认文件使用 `deterministic_demo_reviewer`，只证明流程和哈希绑定，不冒充生产人工批准。正式项目仍须由授权角色复核并签署。

### 只读与隐私

- 四份批准来源运行前后 SHA-256 均匹配，4/4 未变化。
- 产物不含真实文件名、源路径、表标题、单元格原文、人员、机构或受试者标识。
- `real_shell_datapack.deidentified.json` 只保留 ASCII 语义 ID、散列化未知行列和结构坐标。
- 逐受试者宽表继续采用 `metadata_only`，不建立受试者单元格键。

## 3. DOCX shell → RTF 结果

从真实 shell 冻结 `T14.2.4.3.1`（D1）和 `T14.2.4.3.2`（D28）结构，生成仅含冻结 ASCII ID 与合成值的两份 RTF。两次套值均通过：

- 行 ID、列 ID、物理坐标和非空值检查通过；
- 两阶段、四个浓度/频次列保持可区分；
- 套值后完整 datapack 为 3344 个语义单元格；
- 结果只改变 `v/display_value` 和来源哈希，不改变冻结的 110 实体/语义键合同；
- 将 `GM` 行改为 `GM_DRIFT` 后以 `E-RTF-ROW-DRIFT` 硬失败。

当前 RTF adapter 仍是一文件一物理表；本轮实现的是冻结实体的逐表套值，不声称支持一个 RTF 流中的任意多表自动拆分。

## 4. 分节写手覆盖

已接入并生成样例：

| 节 | 变体 | 当前完成度 |
|---|---|---|
| 9.4.3 | randomization | 结构骨架；无执行记录时保留缺口 |
| 9.4.6 | blinding | 结构骨架；不推断“未破盲” |
| 10.1 | subject_disposition | 最小结果骨架、取值与交叉引用 |
| 11.2 | demographics_baseline | 最小结果骨架、取值与交叉引用 |
| 11.4 | exploratory/confirmatory | 既有受控写手，已补 REF |
| 11.A | dose_escalation | 既有受控写手，已补 REF |
| 11.B | topical_low_systemic_exposure | 真实 shell 结构 + D1/D28 合成值 |
| 11.B | 临床药理/特殊人群示例 | 路由激活时的结构骨架 |
| 12.2/12.5 | 局部耐受/途径或特殊人群安全 | 结构骨架，结论需医学确认 |

写手仍只接收当前节 playbook、当前节审核键和最小来源片段。无审核来源时允许 `source_gap`：正文明确保持空缺、`needs_human=true`，不以其他表或自然语言推断补齐。

真实 shell 能支持外用 PK 两阶段示例；但不能单靠 shell 形成 §11.4 所需的六角色疗效绑定，也没有已审的 §11.A DLT 结果对。因此真实端到端中的这两节被正确标记为 `source_gap`。另有独立合成验收样例证明两节的取值、REF 和装配机制。

## 5. 双输出、追溯、五组与 QC

- 未关闭人工项时 clean 输出被阻断；演示决定与当前草稿哈希绑定后，annotated、clean、Synopsis 均通过。
- Synopsis 仅复制已确认的 11.B 正文段落，不存在独立写手。
- `unreferenced_tfl.md` 列出 35 个有结构化结果但尚未被本轮三个示例节绑定的表；另有 73 个 metadata-only 实体单独登记。
- 脱敏四层链 `protocol → SAP → TFL → CSR` 正向和反向均通过；它证明合同机制，不代替真实项目人工定位确认。
- 真实 shell 中五个语义组列均可区分；既有五组合同正常通过、互换故障注入被阻断。
- QC 因存在未引用 TFL 返回 `attention_required`，且 `regulatory_readiness_assessed=false`，没有输出申报或递交结论。

## 6. Codex 形态

根目录新增 `AGENTS.md`，承载原受控写手说明并更新 M5 范围。`.claude-plugin/plugin.json` 暂保留，理由是它只有兼容性元数据、不会影响 Codex，也便于旧入口识别；不在其中放逻辑。

`tests/test_codex_platform_boundary.py` 在临时副本中删除：

- `AGENTS.md`
- `.claude-plugin/`
- `skills/csr-writer/SKILL.md`
- `skills/csr-writer/agents/`

随后运行完整 unittest，并逐一验证全部 CLI 子命令可导入和显示帮助。该回归防止未来把逻辑误放进平台文档。

## 自主设计决定

1. 把 REF token 与取值 token 分开解析和计数，不用数值白名单给表号开例外。
2. 中文前缀转换集中在映射层；键、书签和域指令始终使用 ASCII。
3. 自动提议报告同时公布对象覆盖率和高置信率，避免以“有提议”冒充“已准确映射”。
4. 同一歧义策略批量确认；确认单位仍列出全部受影响实体及候选哈希，保留审计性。
5. 无行列网格的 shell 只登记元数据；宁可锁库后阻断结构漂移，也不提前猜键。
6. 跨格式适配器按冻结物理坐标套值，不在结果阶段重新做语义识别。
7. 真实项目 E2E 只使用 shell 结构；结果 RTF、源片段、人工决定和正文均为脱敏演示。
8. 继续保留 `.claude-plugin` 作为无逻辑兼容层，并用删除平台文档回归证明运行时独立。
9. 不为临床工具交付额外生成示意图，避免扩大真实资料暴露面；关系证据以机器可验 JSON/DOCX 域为主。

## 已知问题

- 高置信实体只有 27.27%；当前可用性的主要瓶颈已经从“逐对象确认”转为“3 个批量策略的质量与修订界面”。
- 26 个位置消歧行仍应由统计师抽样复核；53 个无网格实体必须等待更完整 shell 或结果结构后人工修订映射。
- 多页/多对象连续表目前按一个实体 metadata-only 处理，尚未实现跨页语义拼接。
- REF 书签当前锚定装配文档中的“支持性表、图和清单”索引项；待真实附录装配后应迁移到实际 TFL 标题锚点。
- 扩展写手多数是合规结构骨架，不是终稿级医学文字；真实模型仍未授权。
- 正反向四层 trace 使用显式清单，不从真实文件相似文本自动推断链接。

## M6 前建议

1. 先实现批量确认的可审计修订合同：允许 reviewer 对行/列 ID、分析集、章节路由和 metadata-only 策略作结构化修改，并记录角色签名与版本。
2. 与统计师抽样复核 30 个高置信和 26 个位置消歧实体，得到真实精确率/召回率；在此之前不要把 100% 覆盖率当成正确率。
3. 为多页连续表和“结果 RTF 批目录”增加显式实体边界编排，不放宽单表 parser 的失败边界。
4. 把 REF 书签迁移到实际装配的 14.x/16.x TFL 标题，并增加 Word 更新域后的人工视觉验收。
5. 再扩展局部耐受、皮肤科 ANCOVA、受试者处置和人口学写手的医学/统计内容质量；继续坚持 source_gap 和语义红线门禁。
6. 建立真实项目 protocol/SAP/TFL/CSR locator 的人工确认清单与不可变归档，再扩大跨章一致性规则。

## 可核验证据

- 总报告：`examples/output/m5/M5真实Shell端到端报告.md`
- 运行摘要：`examples/output/m5/m5_e2e_summary.json`
- 提议/批量确认/冻结：`examples/output/m5/real_shell_proposal_report.json`、`batch_confirmation.demo.json`、`tfl_map_frozen.demo.json`
- DOCX→RTF：`examples/output/m5/cross_format_apply.json`、`cross_format_drift_blocked.json`
- 写手清单：`examples/output/m5/writer_section_inventory.json`
- 装配：`examples/output/m5/real_pipeline/`
- 反向溯源/QC/哈希：`examples/output/m5/unreferenced_tfl.md`、`qc_report.md`、`source_hash_audit.json`
- 模块 5→13：`examples/output/real_material_driven/topical_module_route_proof.json`
- Codex 平台边界：`AGENTS.md`、`tests/test_codex_platform_boundary.py`、`examples/output/m5/platform_boundary_test.json`

完成 M5 后停止，等待复核，不自动开始 M6。

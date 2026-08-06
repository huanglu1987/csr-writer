# TFL shell 人工决策队列

> 本报告只含脱敏结构。确认单位是“判断”，不是解析失败批次；同一实体可能需要多项判断。

- 待确认实体：63
- 旧技术批次：3
- 实际决策组：15
- 决策成员关系：157（允许同一实体重复出现）
- 信息量：29099 字符 / 约 33.1 屏
- 预计净审阅时间：83.7 分钟
- 估算口径：500 字符/分钟 + 每组 75 秒判断 + 每类差异 12 秒；不含外部追问等待。

### DG-CODE-MEDDRA · 编码字典依赖：MedDRA

- 共同判断点：确认病史与 AE 是否使用同一版 MedDRA 及相同 SOC/PT 父子规则；若版本不同，必须推翻默认并拆分。
- 默认决定：`confirm_dictionary_and_late_bound_hierarchy`；参数 {"dictionary": "MedDRA", "domains": ["medical_history", "adverse_events"], "row_hierarchy": "SOC/PT", "same_pt_across_soc_allowed": true}
- 确认前必填：填写经 SAP/编码计划确认的 MedDRA 版本；若分域不同须改用 override
- 推翻后果：推翻后需按病史/AE 分别登记版本与分类器；§11.2 和 §12.2 的晚绑定行决定均不得沿用。
- 影响面：影响 37 个 TFL 实体、现有 0 个稳定键；37 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.3.1.3.19`：分析集 `SS`；策略 `columns_only_deferred_rows`；章节 12.2
  - 列：ACTIVE_TOTAL, ACTIVE_TOTAL_C12, C0_5PCT_BID, C0_5PCT_BID_C4, C0_75PCT_BID, C0_75PCT_BID_C6, C1_0PCT_BID, C1_0PCT_BID_C8, C1_0PCT_QD, C1_0PCT_QD_C10, PBO, PBO_C2
  - 稳定行：无；行在结果阶段生成
  - 继承：无
  - 延后行：MedDRA SOC/PT 编码结果；预计行数模式 `unknown_data_driven`；列漂移/语义碰撞硬失败
  - 结构哈希：`5fa99d130ca123dbf16193e7e3f095df8707e16c25b6cb86cf8a8494e8bcc5b7`

- `T14.1.3.2`：分析集 `FAS`；策略 `columns_only_deferred_rows`；章节 11.2
  - 列：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO, TOTAL_N_XXX_N
  - 稳定行：无；行在结果阶段生成
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "C0_5PCT_BID", "C0_75PCT_BID", "C1_0PCT_BID", "C1_0PCT_QD", "ACTIVE_TOTAL", "TOTAL_N_XXX_N"], "inherited_from": "T14.1.3.1", "pattern_id": "template-output-cn", "row_classifier_ids": ["soc-pt"], "rows": "deferred_until_result"}
  - 延后行：MedDRA SOC/PT 编码结果；预计行数模式 `unknown_data_driven`；列漂移/语义碰撞硬失败
  - 结构哈希：`18499810ebd8dd8088e0fbf9255cdb0fcb548863552db2a99fbc908dc1711fdd`

组内差异（仅列与首个代表不同处）：
- `T14.3.1.3.20`, `T14.3.1.3.21`, `T14.3.1.3.22`, `T14.3.1.3.23`, `T14.3.1.3.24`, `T14.3.1.3.25`, `T14.3.1.3.26`, `T14.3.1.3.27`, `T14.3.1.3.28`, `T14.3.1.3.29`, `T14.3.1.3.30`, `T14.3.1.3.31`, `T14.3.1.3.32`, `T14.3.1.3.33`, `T14.3.1.3.34`：layout_reference：继承自 T14.3.1.3.19；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=0/18；physical_structure：对象数=1；首对象=3行×1列
- `T14.1.3.1`：analysis_set："FAS"；ich_e3_sections：["11.2"]；column_ids：7项：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO, TOTAL_N_XXX_N；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=7/0；physical_structure：对象数=1；首对象=13行×8列
- `T14.3.1.3.35`：column_ids：6项：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=6/0；physical_structure：对象数=1；首对象=10行×7列
- `T14.3.1.3.10`, `T14.3.1.3.11`, `T14.3.1.3.12`, `T14.3.1.3.13`, `T14.3.1.3.14`, `T14.3.1.3.15`, `T14.3.1.3.16`, `T14.3.1.3.17`, `T14.3.1.3.18`, `T14.3.1.3.2`, `T14.3.1.3.3`, `T14.3.1.3.4`, `T14.3.1.3.5`, `T14.3.1.3.6`, `T14.3.1.3.7`, `T14.3.1.3.8`, `T14.3.1.3.9`：layout_reference：继承自 T14.3.1.3.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=0/7；physical_structure：对象数=1；首对象=3行×1列
- `T14.3.1.3.1`：deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=7/0；physical_structure：对象数=1；首对象=15行×13列

本组信息量：3012 字符 / 约 3.3 屏。

### DG-CODE-WHODRUG-MEDICATION · 编码字典依赖：用药

- 共同判断点：确认既往/合并用药行来自哪一版 WHODrug，以及 ATC→药物名层级是否一致。
- 默认决定：`confirm_dictionary_and_late_bound_hierarchy`；参数 {"dictionary": "WHODrug", "row_count_is_binding": false, "row_hierarchy": "ATC/drug_name"}
- 确认前必填：填写经 SAP/编码计划确认的 WHODrug 版本
- 推翻后果：推翻后需重定义用药行层级，相关 §11.2 表全部重新入队，结果键不得按现策略生成。
- 影响面：影响 4 个 TFL 实体、现有 0 个稳定键；4 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.1.4.3`：分析集 `FAS`；策略 `columns_only_deferred_rows`；章节 11.2
  - 列：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO, TOTAL_N_XXX_N
  - 稳定行：无；行在结果阶段生成
  - 继承：无
  - 延后行：WHODrug ATC/药物名编码结果；预计行数模式 `unknown_data_driven`；列漂移/语义碰撞硬失败
  - 结构哈希：`384d338a42aa9de36870258f9c4c50fa507234150ffccd9e898a632d731c2a77`

- `T14.1.4.4`：分析集 `FAS`；策略 `columns_only_deferred_rows`；章节 11.2
  - 列：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO, TOTAL_N_XXX_N
  - 稳定行：无；行在结果阶段生成
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "C0_5PCT_BID", "C0_75PCT_BID", "C1_0PCT_BID", "C1_0PCT_QD", "ACTIVE_TOTAL", "TOTAL_N_XXX_N"], "inherited_from": "T14.1.4.3", "pattern_id": "template-output-cn", "row_classifier_ids": ["soc-pt"], "rows": "deferred_until_result"}
  - 延后行：WHODrug ATC/药物名编码结果；预计行数模式 `unknown_data_driven`；列漂移/语义碰撞硬失败
  - 结构哈希：`e2856dceaa95436e5cf1685e94da954605f931a1704a366e88a611ebcbc733bf`

组内差异（仅列与首个代表不同处）：
- `T14.1.4.2`：layout_reference：继承自 T14.1.4.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=whodrug_atc_and_drug_name_from_result_tfl；shell/template 占位=0/9；physical_structure：对象数=1；首对象=3行×1列
- `T14.1.4.1`：deferred_row_expectation：来源=whodrug_atc_and_drug_name_from_result_tfl；shell/template 占位=9/0；physical_structure：对象数=1；首对象=15行×8列

本组信息量：1707 字符 / 约 1.9 屏。

### DG-ROWS-PROTOCOL-DEVIATION · 延后行来源：方案偏离

- 共同判断点：确认偏离类别的权威来源及版本；shell 行数仅作提示，不能按位置冻结。
- 默认决定：`confirm_result_driven_rows`；参数 {"authority": "SAP_or_deviation_plan_then_result_tfl", "position_keys_allowed": false}
- 确认前必填：填写偏离分类的 SAP/判定计划定位或版本
- 推翻后果：推翻后 §10.1 的方案偏离表需更换分类契约，并在结果到位前保持不可冻结。
- 影响面：影响 1 个 TFL 实体、现有 0 个稳定键；1 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.1.1.3`：分析集 `All Enrolled Set`；策略 `columns_only_deferred_rows`；章节 10.1
  - 列：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO, TOTAL_N_XXX_N
  - 稳定行：无；行在结果阶段生成
  - 继承：无
  - 延后行：方案偏离分类结果；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`53083aaa9334d7f78d77a5c3653c114e6bee4f10f24688358c75910f27404e8e`

组内差异（仅列与首个代表不同处）：
- 无其他成员差异。

本组信息量：740 字符 / 约 1.0 屏。

### DG-ROWS-PK-PARAMETERS · 延后行来源：PK 参数

- 共同判断点：确认 PK 参数名及 D1/D28 阶段来自已审 SAP/TFL 规范，而不是 shell 占位位置。
- 默认决定：`confirm_result_driven_rows`；参数 {"authority": "reviewed_SAP_TFL_specification", "position_keys_allowed": false}
- 确认前必填：填写 PK 参数及阶段定义的 SAP/TFL 规范定位或版本
- 推翻后果：推翻后 §11.B 的参数/阶段维度需重建，跨格式结果套值必须继续硬阻断。
- 影响面：影响 1 个 TFL 实体、现有 0 个稳定键；1 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.2.4.2`：分析集 `PKS`；策略 `columns_only_deferred_rows`；章节 11.B
  - 列：COL_FCA3A89E3A, SE_A, SLOPE_A
  - 稳定行：无；行在结果阶段生成
  - 继承：无
  - 延后行：PK 参数/阶段结果行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`12743a918d8fb4b4e7267c5ed5d027c1b98151577a0a60e884ce6779798df26a`

组内差异（仅列与首个代表不同处）：
- 无其他成员差异。

本组信息量：687 字符 / 约 1.0 屏。

### DG-ROWS-BASELINE-EFFICACY · 延后行来源：基线与疗效

- 共同判断点：确认基线分类与疗效统计行来自已审 SAP 终点/分析规范及结果 TFL，不按 shell 位置锁定。
- 默认决定：`confirm_result_driven_rows`；参数 {"authority": "reviewed_endpoint_analysis_specification_and_result_tfl", "position_keys_allowed": false}
- 推翻后果：推翻后 §11.2/§11.4 需分别补充分类或终点行契约，相关表保持不可冻结。
- 影响面：影响 4 个 TFL 实体、现有 304 个稳定键；4 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.1.2.2`：分析集 `FAS`；策略 `mixed_stable_and_deferred_rows`；章节 11.2
  - 列：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO, TOTAL_N_XXX
  - 稳定行：BSA/MEAN_SD, BSA/MEDIAN, BSA/MIN_MAX, BSA/N, DLQI/MEAN_SD, DLQI/MEDIAN, DLQI/MIN_MAX, DLQI/N, MON_A/MEAN_SD, MON_A/MEDIAN, MON_A/MIN_MAX, MON_A/N, WI_NRS/MEAN_SD, WI_NRS/MEDIAN, WI_NRS/MIN_MAX, WI_NRS/N
  - 继承：无
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`a90e4de0eaf8bb514c354404a80f487df57d48bc6d1dd537c03258ca6e81baaa`

- `T14.2.1.1.2`：分析集 `mFAS`；策略 `mixed_stable_and_deferred_rows`；章节 11.4
  - 列：C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO
  - 稳定行：D15/CI95, D15/N, D15/PROPORTION_DIFFERENCE_B, D15/PROPORTION_DIFFERENCE_B_CI95, D29/CI95, D29/N, D29/PROPORTION_DIFFERENCE_B, D29/PROPORTION_DIFFERENCE_B_CI95
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "C0_5PCT_BID", "C0_75PCT_BID", "C1_0PCT_BID", "C1_0PCT_QD"], "inherited_from": "T14.2.1.1.1", "pattern_id": "template-output-cn", "row_classifier_ids": [], "rows": "deferred_until_result"}
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`561ea287b0323e3dfaa4b33ca3b528a601e0354f778a3cef0b473bd7fb47adea`

组内差异（仅列与首个代表不同处）：
- `T14.1.2.1`：stable_row_ids：16项；SHA-256=3a1f83d5cdad…；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=4/0；physical_structure：对象数=1；首对象=34行×8列
- `T14.2.1.1.1`：ich_e3_sections：["11.4"]；column_ids：5项：C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO；stable_row_ids：8项：D15/CI95, D15/N, D15/PROPORTION_DIFFERENCE_B, D15/PROPORTION_DIFFERENCE_B_CI95, D29/CI95, D29/N, D29/PROPORTION_DIFFERENCE_B, D29/PROPORTION_DIFFERENCE_B_CI95；existing_semantic_key_count：40；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=2/0；physical_structure：对象数=1；首对象=18行×6列

本组信息量：2218 字符 / 约 2.5 屏。

### DG-ROWS-SAFETY-SUMMARY · 延后行来源：暴露与 AE 汇总

- 共同判断点：确认暴露和 AE 汇总中的非 SOC/PT 行来自已审安全性分析规范，而不是编码字典或占位位置。
- 默认决定：`confirm_result_driven_rows`；参数 {"authority": "reviewed_safety_summary_specification_and_result_tfl", "position_keys_allowed": false}
- 推翻后果：推翻后 §12.1/§12.2 汇总表必须各自补充行类别契约，不得套用 MedDRA SOC/PT 规则。
- 影响面：影响 2 个 TFL 实体、现有 184 个稳定键；2 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.3.1.1`：分析集 `SS`；策略 `mixed_stable_and_deferred_rows`；章节 12.1
  - 列：C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO
  - 稳定行：DAY1_MG_C/MEAN_SD, DAY1_MG_C/MEDIAN, DAY1_MG_C/MIN_MAX, DAY1_MG_C/N, DAY28_MG_D/MEAN_SD, DAY28_MG_D/MEDIAN, DAY28_MG_D/MIN_MAX, DAY28_MG_D/N, E/MEAN_SD, E/MEDIAN, E/MIN_MAX, E/N, F/MEAN_SD, F/MEDIAN, F/MIN_MAX, F/N, G/MEAN_SD, G/MEDIAN, G/MIN_MAX, G/N, G_A/MEAN_SD, G_A/MEDIAN, G_A/MIN_MAX, G_A/N, MG_B/MEAN_SD, MG_B/MEDIAN, MG_B/MIN_MAX, MG_B/N, N_F/GT_120_PCT, N_F/LT_80_PCT, N_G/GT_120_PCT, N_G/LT_80_PCT
  - 继承：无
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`f3e193217331f6fa44d6a6b75ef05d9cbe2aa38f169747634aac6a5d41ca2912`

- `T14.3.1.2`：分析集 `SS`；策略 `mixed_stable_and_deferred_rows`；章节 12.2
  - 列：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO
  - 稳定行：TEAE, TESAE, TRAE_B, TRSAE
  - 继承：无
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`8bf1cf75b0cbc4da644f40a8f60e9235f8707fdc51bfd7a88e2aace1ee82bb81`

组内差异（仅列与首个代表不同处）：
- 无其他成员差异。

本组信息量：1459 字符 / 约 1.6 屏。

### DG-ROWS-OTHER-SAFETY · 延后行来源：其他安全性域

- 共同判断点：确认实验室、生命体征等其他安全性域的行由各自 SAP/TFL 域规范及结果标签生成。
- 默认决定：`confirm_result_driven_rows`；参数 {"authority": "reviewed_domain_specification_and_result_tfl", "position_keys_allowed": false}
- 推翻后果：推翻后相应 §12 子域需分别登记参数/类别来源，并在结果到位前保持不可冻结。
- 影响面：影响 13 个 TFL 实体、现有 150 个稳定键；13 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.3.6.2`：分析集 `SS`；策略 `mixed_stable_and_deferred_rows`；章节 12
  - 列：COL_A7AC587AB5, COL_A7AC587AB5_C2, COL_A7AC587AB5_C3, COL_A7AC587AB5_C4, COL_A7AC587AB5_C5
  - 稳定行：N_XXX/CS_N, N_XXX/MISSING_N, N_XXX/N1, N_XXX/NCS_N, N_XXX/NORMAL_N
  - 继承：无
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`93287f3b774f09279bda33ae05d05e65fc9a34e5b98c0b8b1c09cab42a507489`

- `T14.3.2.4`：分析集 `SS`；策略 `columns_only_deferred_rows`；章节 12
  - 列：ACTIVE_TOTAL, ACTIVE_TOTAL_C12, C0_5PCT_BID, C0_5PCT_BID_C4, C0_75PCT_BID, C0_75PCT_BID_C6, C1_0PCT_BID, C1_0PCT_BID_C8, C1_0PCT_QD, C1_0PCT_QD_C10, PBO, PBO_C2
  - 稳定行：无；行在结果阶段生成
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "PBO_C2", "C0_5PCT_BID", "C0_5PCT_BID_C4", "C0_75PCT_BID", "C0_75PCT_BID_C6", "C1_0PCT_BID", "C1_0PCT_BID_C8", "C1_0PCT_QD", "C1_0PCT_QD_C10", "ACTIVE_TOTAL", "ACTIVE_TOTAL_C12"], "inherited_from": "T14.3.1.3.19", "pattern_id": "template-output-cn", "row_classifier_ids": ["soc-pt"], "rows": "deferred_until_result"}
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`277c7b091b03c2c94ad8666be04276fff26f01364defe990202bd0d38b310728`

组内差异（仅列与首个代表不同处）：
- `T14.3.2.3`：ingest_strategy："columns_only_deferred_rows"；column_ids：12项；SHA-256=4b5e456aee84…；stable_row_ids：0项：；existing_semantic_key_count：0；layout_reference：继承自 T14.3.1.3.19；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=0/18；physical_structure：对象数=1；首对象=3行×1列
- `T14.3.4.2.2`, `T14.3.4.2.3`, `T14.3.4.2.4`：stable_row_ids：5项：STRUCT_B3AE6ED659/N_XXX/CS_N, STRUCT_B3AE6ED659/N_XXX/MISSING_N, STRUCT_B3AE6ED659/N_XXX/N1, STRUCT_B3AE6ED659/N_XXX/NCS_N, STRUCT_B3AE6ED659/N_XXX/NORMAL_N；layout_reference：继承自 T14.3.4.2.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=0/1；physical_structure：对象数=1；首对象=3行×1列
- `T14.3.6.3`：ingest_strategy："columns_only_deferred_rows"；column_ids：6项：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO；stable_row_ids：0项：；existing_semantic_key_count：0；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=5/0；physical_structure：对象数=1；首对象=11行×7列
- `T14.3.2.1`, `T14.3.2.2`：ingest_strategy："columns_only_deferred_rows"；column_ids：12项；SHA-256=4b5e456aee84…；stable_row_ids：0项：；existing_semantic_key_count：0；layout_reference：继承自 T14.3.1.3.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=0/7；physical_structure：对象数=1；首对象=3行×1列
- `T14.3.4.2.1`：stable_row_ids：5项：STRUCT_B3AE6ED659/N_XXX/CS_N, STRUCT_B3AE6ED659/N_XXX/MISSING_N, STRUCT_B3AE6ED659/N_XXX/N1, STRUCT_B3AE6ED659/N_XXX/NCS_N, STRUCT_B3AE6ED659/N_XXX/NORMAL_N；physical_structure：对象数=1；首对象=22行×6列
- `T14.3.5.2`：stable_row_ids：5项：STRUCT_6E8AF70854/N_XXX/CS_N, STRUCT_6E8AF70854/N_XXX/MISSING_N, STRUCT_6E8AF70854/N_XXX/N1, STRUCT_6E8AF70854/N_XXX/NCS_N, STRUCT_6E8AF70854/N_XXX/NORMAL_N；physical_structure：对象数=1；首对象=22行×6列
- `T14.3.7.2.1`, `T14.3.7.2.2`：ingest_strategy："columns_only_deferred_rows"；column_ids：6项：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO；stable_row_ids：0项：；existing_semantic_key_count：0；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=12/0；physical_structure：对象数=1；首对象=21行×7列

本组信息量：3674 字符 / 约 4.1 屏。

### DG-POP-PARALLEL · 分析集平行表

- 共同判断点：确认 FAS/mFAS 等平行版本仅共享版式，POP 维度与实际结果始终各自独立。
- 默认决定：`confirm_parallel_structure_keep_population_separate`；参数 {"inherit_columns": true, "inherit_population": false, "inherit_statistic_rows": true}
- 推翻后果：推翻后平行表不能批量复用；对应分析集的映射、绑定和章节引用均需独立重审。
- 影响面：影响 2 个 TFL 实体、现有 80 个稳定键；2 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.2.1.1.1`：分析集 `FAS`；策略 `mixed_stable_and_deferred_rows`；章节 11.4
  - 列：C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO
  - 稳定行：D15/CI95, D15/N, D15/PROPORTION_DIFFERENCE_B, D15/PROPORTION_DIFFERENCE_B_CI95, D29/CI95, D29/N, D29/PROPORTION_DIFFERENCE_B, D29/PROPORTION_DIFFERENCE_B_CI95
  - 继承：无
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`4b3e3fc42610713621f948d4f3fc3c53eed7f75629ccf594b976639859eccca1`

- `T14.2.1.1.2`：分析集 `mFAS`；策略 `mixed_stable_and_deferred_rows`；章节 11.4
  - 列：C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO
  - 稳定行：D15/CI95, D15/N, D15/PROPORTION_DIFFERENCE_B, D15/PROPORTION_DIFFERENCE_B_CI95, D29/CI95, D29/N, D29/PROPORTION_DIFFERENCE_B, D29/PROPORTION_DIFFERENCE_B_CI95
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "C0_5PCT_BID", "C0_75PCT_BID", "C1_0PCT_BID", "C1_0PCT_QD"], "inherited_from": "T14.2.1.1.1", "pattern_id": "template-output-cn", "row_classifier_ids": [], "rows": "deferred_until_result"}
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`561ea287b0323e3dfaa4b33ca3b528a601e0354f778a3cef0b473bd7fb47adea`

组内差异（仅列与首个代表不同处）：
- 无其他成员差异。

本组信息量：1548 字符 / 约 1.7 屏。

### DG-LAYOUT-BASELINE · 版式继承：病史与用药

- 共同判断点：确认每个病史/用药子表的继承源正确，且只继承列；数据驱动行必须留到结果阶段。
- 默认决定：`confirm_layout_target_and_scope`；参数 {"inherit_columns": true, "inherit_data_driven_rows": false}
- 推翻后果：推翻某一继承源会使该子表结构哈希失效，并使 §11.2 的相关列映射重新入队。
- 影响面：影响 3 个 TFL 实体、现有 0 个稳定键；3 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.1.4.4`：分析集 `FAS`；策略 `columns_only_deferred_rows`；章节 11.2
  - 列：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO, TOTAL_N_XXX_N
  - 稳定行：无；行在结果阶段生成
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "C0_5PCT_BID", "C0_75PCT_BID", "C1_0PCT_BID", "C1_0PCT_QD", "ACTIVE_TOTAL", "TOTAL_N_XXX_N"], "inherited_from": "T14.1.4.3", "pattern_id": "template-output-cn", "row_classifier_ids": ["soc-pt"], "rows": "deferred_until_result"}
  - 延后行：WHODrug ATC/药物名编码结果；预计行数模式 `unknown_data_driven`；列漂移/语义碰撞硬失败
  - 结构哈希：`e2856dceaa95436e5cf1685e94da954605f931a1704a366e88a611ebcbc733bf`

- `T14.1.4.2`：分析集 `FAS`；策略 `columns_only_deferred_rows`；章节 11.2
  - 列：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO, TOTAL_N_XXX_N
  - 稳定行：无；行在结果阶段生成
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "C0_5PCT_BID", "C0_75PCT_BID", "C1_0PCT_BID", "C1_0PCT_QD", "ACTIVE_TOTAL", "TOTAL_N_XXX_N"], "inherited_from": "T14.1.4.1", "pattern_id": "template-output-cn", "row_classifier_ids": [], "rows": "deferred_until_result"}
  - 延后行：WHODrug ATC/药物名编码结果；预计行数模式 `unknown_data_driven`；列漂移/语义碰撞硬失败
  - 结构哈希：`e75618cf3d7fbb62b6c7af83348140c1e892a97fc2978d19a7ac4793c9740050`

组内差异（仅列与首个代表不同处）：
- `T14.1.3.2`：layout_reference：继承自 T14.1.3.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=0/7

本组信息量：1703 字符 / 约 1.9 屏。

### DG-LAYOUT-EFFICACY · 版式继承：疗效平行表

- 共同判断点：确认疗效平行表的模板源和继承范围；分析集标签必须从各自标题解析，绝不继承。
- 默认决定：`confirm_layout_target_and_scope`；参数 {"inherit_columns": true, "inherit_population": false, "inherit_statistic_rows": true}
- 推翻后果：推翻后 §11.4 的平行分析集表需独立建立稳定统计量行与列契约。
- 影响面：影响 1 个 TFL 实体、现有 40 个稳定键；1 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.2.1.1.2`：分析集 `mFAS`；策略 `mixed_stable_and_deferred_rows`；章节 11.4
  - 列：C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO
  - 稳定行：D15/CI95, D15/N, D15/PROPORTION_DIFFERENCE_B, D15/PROPORTION_DIFFERENCE_B_CI95, D29/CI95, D29/N, D29/PROPORTION_DIFFERENCE_B, D29/PROPORTION_DIFFERENCE_B_CI95
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "C0_5PCT_BID", "C0_75PCT_BID", "C1_0PCT_BID", "C1_0PCT_QD"], "inherited_from": "T14.2.1.1.1", "pattern_id": "template-output-cn", "row_classifier_ids": [], "rows": "deferred_until_result"}
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`561ea287b0323e3dfaa4b33ca3b528a601e0354f778a3cef0b473bd7fb47adea`

组内差异（仅列与首个代表不同处）：
- 无其他成员差异。

本组信息量：1065 字符 / 约 1.2 屏。

### DG-LAYOUT-AE-FAMILY · 版式继承：AE 表族

- 共同判断点：确认 AE 子集表引用的根模板正确；共享列结构，但 SOC/PT 或其他结果行不继承。
- 默认决定：`confirm_layout_target_and_scope`；参数 {"inherit_columns": true, "inherit_data_driven_rows": false}
- 推翻后果：推翻根模板会使所有依赖该模板的 §12/§12.2 子表决定失效并要求重新确认。
- 影响面：影响 36 个 TFL 实体、现有 0 个稳定键；36 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.3.2.4`：分析集 `SS`；策略 `columns_only_deferred_rows`；章节 12
  - 列：ACTIVE_TOTAL, ACTIVE_TOTAL_C12, C0_5PCT_BID, C0_5PCT_BID_C4, C0_75PCT_BID, C0_75PCT_BID_C6, C1_0PCT_BID, C1_0PCT_BID_C8, C1_0PCT_QD, C1_0PCT_QD_C10, PBO, PBO_C2
  - 稳定行：无；行在结果阶段生成
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "PBO_C2", "C0_5PCT_BID", "C0_5PCT_BID_C4", "C0_75PCT_BID", "C0_75PCT_BID_C6", "C1_0PCT_BID", "C1_0PCT_BID_C8", "C1_0PCT_QD", "C1_0PCT_QD_C10", "ACTIVE_TOTAL", "ACTIVE_TOTAL_C12"], "inherited_from": "T14.3.1.3.19", "pattern_id": "template-output-cn", "row_classifier_ids": ["soc-pt"], "rows": "deferred_until_result"}
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`277c7b091b03c2c94ad8666be04276fff26f01364defe990202bd0d38b310728`

- `T14.3.1.3.9`：分析集 `SS`；策略 `columns_only_deferred_rows`；章节 12.2
  - 列：ACTIVE_TOTAL, ACTIVE_TOTAL_C12, C0_5PCT_BID, C0_5PCT_BID_C4, C0_75PCT_BID, C0_75PCT_BID_C6, C1_0PCT_BID, C1_0PCT_BID_C8, C1_0PCT_QD, C1_0PCT_QD_C10, PBO, PBO_C2
  - 稳定行：无；行在结果阶段生成
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "PBO_C2", "C0_5PCT_BID", "C0_5PCT_BID_C4", "C0_75PCT_BID", "C0_75PCT_BID_C6", "C1_0PCT_BID", "C1_0PCT_BID_C8", "C1_0PCT_QD", "C1_0PCT_QD_C10", "ACTIVE_TOTAL", "ACTIVE_TOTAL_C12"], "inherited_from": "T14.3.1.3.1", "pattern_id": "template-output-cn", "row_classifier_ids": ["soc-pt"], "rows": "deferred_until_result"}
  - 延后行：MedDRA SOC/PT 编码结果；预计行数模式 `unknown_data_driven`；列漂移/语义碰撞硬失败
  - 结构哈希：`3a370bc3ff35f272439bdaa5779f0d4287d4cbe8c1e94ba86c22df5632040566`

组内差异（仅列与首个代表不同处）：
- `T14.3.1.3.10`, `T14.3.1.3.11`, `T14.3.1.3.12`, `T14.3.1.3.13`, `T14.3.1.3.14`, `T14.3.1.3.15`, `T14.3.1.3.16`, `T14.3.1.3.17`, `T14.3.1.3.18`, `T14.3.1.3.2`, `T14.3.1.3.3`, `T14.3.1.3.4`, `T14.3.1.3.5`, `T14.3.1.3.6`, `T14.3.1.3.7`, `T14.3.1.3.8`：ich_e3_sections：["12.2"]；layout_reference：继承自 T14.3.1.3.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=0/7
- `T14.3.2.3`：无结构差异
- `T14.3.1.3.20`, `T14.3.1.3.21`, `T14.3.1.3.22`, `T14.3.1.3.23`, `T14.3.1.3.24`, `T14.3.1.3.25`, `T14.3.1.3.26`, `T14.3.1.3.27`, `T14.3.1.3.28`, `T14.3.1.3.29`, `T14.3.1.3.30`, `T14.3.1.3.31`, `T14.3.1.3.32`, `T14.3.1.3.33`, `T14.3.1.3.34`：ich_e3_sections：["12.2"]；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=0/18
- `T14.3.2.1`, `T14.3.2.2`：layout_reference：继承自 T14.3.1.3.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=0/7

本组信息量：2853 字符 / 约 3.2 屏。

### DG-LAYOUT-OTHER-SAFETY · 版式继承：其他安全性表

- 共同判断点：确认其他安全性平行表的模板源，并区分可继承的稳定统计量行与延后数据行。
- 默认决定：`confirm_layout_target_and_scope`；参数 {"inherit_columns": true, "inherit_data_driven_rows": false, "inherit_stable_statistic_rows": true}
- 推翻后果：推翻后相应 §12 子表必须独立解析网格；依赖源变化时既有决定不得静默沿用。
- 影响面：影响 3 个 TFL 实体、现有 75 个稳定键；3 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.3.4.2.4`：分析集 `SS`；策略 `mixed_stable_and_deferred_rows`；章节 12
  - 列：COL_A7AC587AB5, COL_A7AC587AB5_C2, COL_A7AC587AB5_C3, COL_A7AC587AB5_C4, COL_A7AC587AB5_C5
  - 稳定行：STRUCT_B3AE6ED659/N_XXX/CS_N, STRUCT_B3AE6ED659/N_XXX/MISSING_N, STRUCT_B3AE6ED659/N_XXX/N1, STRUCT_B3AE6ED659/N_XXX/NCS_N, STRUCT_B3AE6ED659/N_XXX/NORMAL_N
  - 继承：{"columns": "inherited", "inherited_column_ids": ["COL_A7AC587AB5", "COL_A7AC587AB5_C2", "COL_A7AC587AB5_C3", "COL_A7AC587AB5_C4", "COL_A7AC587AB5_C5"], "inherited_from": "T14.3.4.2.1", "pattern_id": "template-output-cn", "row_classifier_ids": [], "rows": "deferred_until_result"}
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`a1ab44e629dd858ade313c58f0c94ab1883ab406c05934df9807d53e66a620f5`

- `T14.3.4.2.3`：分析集 `SS`；策略 `mixed_stable_and_deferred_rows`；章节 12
  - 列：COL_A7AC587AB5, COL_A7AC587AB5_C2, COL_A7AC587AB5_C3, COL_A7AC587AB5_C4, COL_A7AC587AB5_C5
  - 稳定行：STRUCT_B3AE6ED659/N_XXX/CS_N, STRUCT_B3AE6ED659/N_XXX/MISSING_N, STRUCT_B3AE6ED659/N_XXX/N1, STRUCT_B3AE6ED659/N_XXX/NCS_N, STRUCT_B3AE6ED659/N_XXX/NORMAL_N
  - 继承：{"columns": "inherited", "inherited_column_ids": ["COL_A7AC587AB5", "COL_A7AC587AB5_C2", "COL_A7AC587AB5_C3", "COL_A7AC587AB5_C4", "COL_A7AC587AB5_C5"], "inherited_from": "T14.3.4.2.1", "pattern_id": "template-output-cn", "row_classifier_ids": [], "rows": "deferred_until_result"}
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`3544f62b8acc0d3a1e489f874c91f01ab13c48ac7ff0da65c426f7357e451e33`

组内差异（仅列与首个代表不同处）：
- `T14.3.4.2.2`：无结构差异

本组信息量：1933 字符 / 约 2.1 屏。

### DG-FAMILY-AE · 同族批量：AE 及后果子集

- 共同判断点：确认 TEAE/TRAE 及导致减量、暂停、永久停药等表族使用一致的列和晚绑定行策略。
- 默认决定：`confirm_family_policy`；参数 {"column_contract_consistent_per_template": true, "rows_materialize_per_result_table": true}
- 推翻后果：推翻后不能对该表族批量确认；每个差异子族需拆分并分别重审，影响 §12/§12.2。
- 影响面：影响 39 个 TFL 实体、现有 0 个稳定键；39 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.3.1.3.19`：分析集 `SS`；策略 `columns_only_deferred_rows`；章节 12.2
  - 列：ACTIVE_TOTAL, ACTIVE_TOTAL_C12, C0_5PCT_BID, C0_5PCT_BID_C4, C0_75PCT_BID, C0_75PCT_BID_C6, C1_0PCT_BID, C1_0PCT_BID_C8, C1_0PCT_QD, C1_0PCT_QD_C10, PBO, PBO_C2
  - 稳定行：无；行在结果阶段生成
  - 继承：无
  - 延后行：MedDRA SOC/PT 编码结果；预计行数模式 `unknown_data_driven`；列漂移/语义碰撞硬失败
  - 结构哈希：`5fa99d130ca123dbf16193e7e3f095df8707e16c25b6cb86cf8a8494e8bcc5b7`

- `T14.3.2.4`：分析集 `SS`；策略 `columns_only_deferred_rows`；章节 12
  - 列：ACTIVE_TOTAL, ACTIVE_TOTAL_C12, C0_5PCT_BID, C0_5PCT_BID_C4, C0_75PCT_BID, C0_75PCT_BID_C6, C1_0PCT_BID, C1_0PCT_BID_C8, C1_0PCT_QD, C1_0PCT_QD_C10, PBO, PBO_C2
  - 稳定行：无；行在结果阶段生成
  - 继承：{"columns": "inherited", "inherited_column_ids": ["PBO", "PBO_C2", "C0_5PCT_BID", "C0_5PCT_BID_C4", "C0_75PCT_BID", "C0_75PCT_BID_C6", "C1_0PCT_BID", "C1_0PCT_BID_C8", "C1_0PCT_QD", "C1_0PCT_QD_C10", "ACTIVE_TOTAL", "ACTIVE_TOTAL_C12"], "inherited_from": "T14.3.1.3.19", "pattern_id": "template-output-cn", "row_classifier_ids": ["soc-pt"], "rows": "deferred_until_result"}
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`277c7b091b03c2c94ad8666be04276fff26f01364defe990202bd0d38b310728`

组内差异（仅列与首个代表不同处）：
- `T14.3.2.3`：ich_e3_sections：["12"]；layout_reference：继承自 T14.3.1.3.19；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=0/18；physical_structure：对象数=1；首对象=3行×1列
- `T14.3.1.3.20`, `T14.3.1.3.21`, `T14.3.1.3.22`, `T14.3.1.3.23`, `T14.3.1.3.24`, `T14.3.1.3.25`, `T14.3.1.3.26`, `T14.3.1.3.27`, `T14.3.1.3.28`, `T14.3.1.3.29`, `T14.3.1.3.30`, `T14.3.1.3.31`, `T14.3.1.3.32`, `T14.3.1.3.33`, `T14.3.1.3.34`：layout_reference：继承自 T14.3.1.3.19；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=0/18；physical_structure：对象数=1；首对象=3行×1列
- `T14.3.1.3.35`：column_ids：6项：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=6/0；physical_structure：对象数=1；首对象=10行×7列
- `T14.3.1.3.10`, `T14.3.1.3.11`, `T14.3.1.3.12`, `T14.3.1.3.13`, `T14.3.1.3.14`, `T14.3.1.3.15`, `T14.3.1.3.16`, `T14.3.1.3.17`, `T14.3.1.3.18`, `T14.3.1.3.2`, `T14.3.1.3.3`, `T14.3.1.3.4`, `T14.3.1.3.5`, `T14.3.1.3.6`, `T14.3.1.3.7`, `T14.3.1.3.8`, `T14.3.1.3.9`：layout_reference：继承自 T14.3.1.3.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=0/7；physical_structure：对象数=1；首对象=3行×1列
- `T14.3.2.1`, `T14.3.2.2`：ich_e3_sections：["12"]；layout_reference：继承自 T14.3.1.3.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=0/7；physical_structure：对象数=1；首对象=3行×1列
- `T14.3.1.3.1`：deferred_row_expectation：来源=meddra_soc_pt_from_result_tfl；shell/template 占位=7/0；physical_structure：对象数=1；首对象=15行×13列

本组信息量：3211 字符 / 约 3.6 屏。

### DG-FAMILY-OTHER-SAFETY · 同族批量：其他安全性评估

- 共同判断点：确认实验室、生命体征及其他安全性表只在相同结构子族内复用策略，差异表保持单独契约。
- 默认决定：`confirm_family_policy`；参数 {"reuse_only_within_equal_structure": true, "structure_hash_required": true}
- 推翻后果：推翻后相关 §12 表需拆成更细子族；结构异常实体仍不得转为 cell_level。
- 影响面：影响 10 个 TFL 实体、现有 150 个稳定键；9 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.3.6.2`：分析集 `SS`；策略 `mixed_stable_and_deferred_rows`；章节 12
  - 列：COL_A7AC587AB5, COL_A7AC587AB5_C2, COL_A7AC587AB5_C3, COL_A7AC587AB5_C4, COL_A7AC587AB5_C5
  - 稳定行：N_XXX/CS_N, N_XXX/MISSING_N, N_XXX/N1, N_XXX/NCS_N, N_XXX/NORMAL_N
  - 继承：无
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`93287f3b774f09279bda33ae05d05e65fc9a34e5b98c0b8b1c09cab42a507489`

- `T14.3.7.2.2`：分析集 `SS`；策略 `columns_only_deferred_rows`；章节 12
  - 列：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO
  - 稳定行：无；行在结果阶段生成
  - 继承：无
  - 延后行：结果 TFL 数据驱动行；预计行数模式 `non_binding_shell_placeholder_count`；列漂移/语义碰撞硬失败
  - 结构哈希：`d712c66826bd519cd3fecaefdf2415b544ba9979ad0181ba74f141b08bcca762`

组内差异（仅列与首个代表不同处）：
- `T14.3.4.2.2`, `T14.3.4.2.3`, `T14.3.4.2.4`：stable_row_ids：5项：STRUCT_B3AE6ED659/N_XXX/CS_N, STRUCT_B3AE6ED659/N_XXX/MISSING_N, STRUCT_B3AE6ED659/N_XXX/N1, STRUCT_B3AE6ED659/N_XXX/NCS_N, STRUCT_B3AE6ED659/N_XXX/NORMAL_N；layout_reference：继承自 T14.3.4.2.1；列=inherited；行=deferred_until_result；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=0/1；physical_structure：对象数=1；首对象=3行×1列
- `T14.3.6.3`：ingest_strategy："columns_only_deferred_rows"；column_ids：6项：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO；stable_row_ids：0项：；existing_semantic_key_count：0；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=5/0；physical_structure：对象数=1；首对象=11行×7列
- `T14.3.7.5.2`：ingest_strategy："metadata_only"；column_ids：0项：；stable_row_ids：0项：；existing_semantic_key_count：0；deferred_row_expectation：无延后行；physical_structure：对象数=63；首对象=21行×7列
- `T14.3.4.2.1`：stable_row_ids：5项：STRUCT_B3AE6ED659/N_XXX/CS_N, STRUCT_B3AE6ED659/N_XXX/MISSING_N, STRUCT_B3AE6ED659/N_XXX/N1, STRUCT_B3AE6ED659/N_XXX/NCS_N, STRUCT_B3AE6ED659/N_XXX/NORMAL_N；physical_structure：对象数=1；首对象=22行×6列
- `T14.3.5.2`：stable_row_ids：5项：STRUCT_6E8AF70854/N_XXX/CS_N, STRUCT_6E8AF70854/N_XXX/MISSING_N, STRUCT_6E8AF70854/N_XXX/N1, STRUCT_6E8AF70854/N_XXX/NCS_N, STRUCT_6E8AF70854/N_XXX/NORMAL_N；physical_structure：对象数=1；首对象=22行×6列
- `T14.3.7.2.1`：ingest_strategy："columns_only_deferred_rows"；column_ids：6项：ACTIVE_TOTAL, C0_5PCT_BID, C0_75PCT_BID, C1_0PCT_BID, C1_0PCT_QD, PBO；stable_row_ids：0项：；existing_semantic_key_count：0；deferred_row_expectation：来源=result_tfl_row_labels；shell/template 占位=12/0；physical_structure：对象数=1；首对象=21行×7列

本组信息量：2701 字符 / 约 3.0 屏。

### DG-STRUCTURE-ANOMALY · 结构异常个案

- 共同判断点：确认多 Word 表对象或无网格个案暂按表级元数据登记，直至人工定义对象边界。
- 默认决定：`confirm_metadata_only_until_subtables_classified`；参数 {"manual_subtable_boundary_required": true, "materialize_cell_keys": false}
- 确认前必填：确认保持表级元数据，或登记经审阅的子表边界方案
- 推翻后果：推翻后必须先提交每个 Word 表对象的归属与合并规则；未完成前不得生成任何单元格键。
- 影响面：影响 1 个 TFL 实体、现有 0 个稳定键；0 个实体的晚绑定键数量须等结果 TFL 到位后确定。

代表样例（完整结构）：

- `T14.3.7.5.2`：分析集 `SS`；策略 `metadata_only`；章节 12
  - 列：无单元格键（表级元数据）
  - 稳定行：无；行在结果阶段生成
  - 继承：无
  - 延后行：无
  - 结构哈希：`b5d888b29c4f32affdfa4f772e8f087cf27f5a73bf8d14d2d11656ee64809f98`

组内差异（仅列与首个代表不同处）：
- 无其他成员差异。

本组信息量：588 字符 / 约 1.0 屏。

## 审计要求

每组决定必须绑定本报告 SHA-256、组结构 SHA-256、审阅者标识和带时区时间戳；推翻默认决定必须写理由。

队列 SHA-256：`7504a393eb4c53740b05b8c73e6ee04b92e8e977c392949ca557669886d57d08`

# 条件性删节留痕

> 处置模式：`remove`。决定来自 study_facts 派生能力或显式提交地区策略，不使用期别或治疗领域固定模板。

| 节 | 标题 | 处置 | 依据字段与值 |
|---|---|---|---|
| 11.4 | 疗效结果和个体患者数据列表 | 删除 | has_confirmatory_efficacy=False |
| 11.4.1 | 疗效分析 | 删除 | has_confirmatory_efficacy=False |
| 11.4.2 | 统计/分析内容 | 删除 | has_confirmatory_efficacy=False |
| 11.4.2.1 | 协变量的调整 | 删除 | has_confirmatory_efficacy=False |
| 11.4.2.2 | 脱落或缺失数据的处理 | 删除 | has_confirmatory_efficacy=False |
| 11.4.2.3 | 试验期中分析和数据监查 | 删除 | has_confirmatory_efficacy=False；has_interim_analysis=False |
| 11.4.2.4 | 多中心研究 | 删除 | has_confirmatory_efficacy=False；is_multicenter=True |
| 11.4.2.5 | 多重比较/多重性 | 删除 | has_confirmatory_efficacy=False；has_multiplicity_control=False |
| 11.4.2.6 | 患者『疗效子集』的使用 | 删除 | has_confirmatory_efficacy=False；has_estimand_framework=False |
| 11.4.2.7 | 旨在表明等效性的阳性对照研究 | 删除 | has_confirmatory_efficacy=False；has_noninferiority=False；is_bioequivalence_study=False |
| 11.4.2.8 | 亚组的考察 | 删除 | has_confirmatory_efficacy=False；has_subgroup_analysis=False |
| 11.4.3 | 个体疗效数据列表 | 删除 | routing.active_module_ids=no supporting module |
| 11.4.4 | 药物剂量、药物浓度以及与效应之间的关系 | 删除 | has_exposure_response=False |
| 11.4.5 | 药物-药物和药物-疾病相互作用 | 删除 | is_ddi_study=False |
| 11.4.6 | 按患者列出 | 删除 | routing.active_module_ids=no supporting module |
| 11.4.7 | 疗效结论 | 删除 | has_confirmatory_efficacy=False |
| 16.1.3 | IEC 或 IRB 列表 - 代表性患者书面信息和同意书样本 | 删除 | routing.active_module_ids=no supporting module |
| 16.1.4 | 研究者及其他重要研究参与者的列表和描述，包括个人简历 | 删除 | routing.active_module_ids=no supporting module |
| 16.1.6 | 如使用 1 批以上药物，接受特定批次试验药品的患者列表 | 删除 | routing.active_module_ids=no supporting module |
| 16.1.8 | 稽查证书（如有） | 删除 | routing.active_module_ids=no supporting module |
| 16.1.10 | 实验室间标准化方法和质量保证程序的文件（如使用） | 删除 | routing.active_module_ids=no supporting module |
| 16.1.11 | 基于研究的发表文章 | 删除 | routing.active_module_ids=no supporting module |
| 16.1.12 | 报告中引用的重要发表文章 | 删除 | routing.active_module_ids=no supporting module |
| 16.3.2 | 递交的其他 CRF | 删除 | routing.active_module_ids=no supporting module |
| 16.4 | 个例患者数据列表（US 存档列表） | 删除 | render_policy.submission_region=CDE |

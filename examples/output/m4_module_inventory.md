# 内容模块清单（schema 1.2）

> 路由只使用 study_facts 五维事实；`phase` 禁止参与模块激活。

| 层级 | 模块 | 实现状态 | 激活谓词 | E3 落位 | Playbook | 预期 TFL | 所需分析集 |
|---|---|---|---|---|---|---|---|
| M3 原有 | M-CORE | 路由/内容合同 | `always true` | 1-8(add:core)、9.1-9.6(add:core)、10(add:core)、13-16(add:core) | — | disposition、demographics、protocol_deviation、safety | — |
| M3 原有 | M-DOSE-ESC | 路由/内容合同 | `design.escalation_scheme != none` | 11.A(add:dose_escalation) | s11-a-dose-escalation.md | dose_escalation、dose_limiting_toxicity、exposure | SAF |
| M3 原有 | M-DLT | 路由/内容合同 | `design.dlt_defined` | 11.A.3(extend:dlt_definition)、11.A.5(extend:dlt_results) | s11-a-dose-escalation.md | dose_limiting_toxicity | DLT_EVAL |
| M3 原有 | M-MTD-RP2D | 路由/内容合同 | `objectives.determine_mtd \|\| objectives.determine_rp2d` | 11.A.7(extend:mtd_rp2d_decision) | s11-a-dose-escalation.md | dose_escalation、dose_limiting_toxicity、exposure | DLT_EVAL、SAF |
| M3 原有 | M-EXPANSION | 路由/内容合同 | `design.expansion_cohorts > 0` | 11.A(add:expansion_cohorts)、10.1(extend:by_cohort) | — | disposition、demographics、efficacy、safety | SAF |
| M3 原有 | M-PK-NCA | 路由/内容合同 | `data_domains.pk_concentration && analyses.nca` | 11.B(add:pk_nca)、9.5.4(extend:pk_sampling) | — | pk_concentration、pk_nca | PKCS、PKPS |
| M3 原有 | M-PK-POP | 路由/内容合同 | `analyses.population_pk` | 11.B(add:population_pk) | — | population_pk | POPPK |
| M3 原有 | M-DOSE-PROP | 路由/内容合同 | `analyses.dose_proportionality` | 11.B(add:dose_proportionality) | — | pk_nca、dose_proportionality | PKPS |
| M3 原有 | M-EXPOSURE-RESP | 路由/内容合同 | `analyses.exposure_response` | 11.4.4(override:exposure_response) | — | exposure_response | PKPS |
| M3 原有 | M-ADA | 路由/内容合同 | `data_domains.ada` | 11.C(add:immunogenicity) | — | ada | ADA |
| M3 原有 | M-PD-BIOMARKER | 路由/内容合同 | `data_domains.pd_biomarker` | 11.D(add:pd_biomarker) | — | pd、biomarker | PD |
| M3 原有 | M-EFF-CONFIRM | 路由/内容合同 | `objectives.confirmatory_efficacy && analyses.hypothesis_test` | 11.4.1(override:confirmatory)、9.7.1(extend:confirmatory) | s11-4-primary-efficacy.md | primary_efficacy、hypothesis_test | FAS |
| M3 原有 | M-EFF-EXPLORE | 路由/内容合同 | `objectives.efficacy && !analyses.hypothesis_test` | 11.4.1(override:exploratory) | s11-4-primary-efficacy.md | efficacy | FAS |
| M3 原有 | M-MULTIPLICITY | 路由/内容合同 | `analyses.multiplicity_control` | 11.4.2.5(extend:multiplicity) | — | multiplicity | FAS |
| M3 原有 | M-NONINF | 路由/内容合同 | `design.noninferiority` | 9.2(extend:noninferiority)、11.4.2.7(extend:noninferiority) | — | noninferiority | FAS、PPS |
| M3 原有 | M-ESTIMAND | 路由/内容合同 | `analyses.estimand_framework` | 8(extend:estimand)、9.7.1(extend:estimand)、9.7.2(extend:estimand)、9.8(extend:estimand)、10.1(extend:estimand)、11.1(extend:estimand)、11.2(extend:estimand)、11.4.2.2(extend:estimand)、11.4.2.6(extend:estimand)、11.4.7(extend:estimand) | — | intercurrent_event、sensitivity_analysis | FAS |
| M3 原有 | M-INTERIM | 路由/内容合同 | `design.interim_analysis` | 11.4.2.3(extend:interim)、16.1.9(extend:dmc_minutes) | — | interim_analysis | FAS |
| M3 原有 | M-SUBGROUP | 路由/内容合同 | `analyses.subgroup` | 11.4.2.8(extend:subgroup) | — | subgroup | FAS |
| M3 原有 | M-MULTICENTER | 路由/内容合同 | `design.n_centers > 1` | 11.4.2.4(extend:center_effect)、10.2(extend:by_center)、16.2.2(extend:by_center) | — | center_effect | FAS |
| M3 原有 | M-CROSSOVER | 路由/内容合同 | `design.type == crossover` | 9.2(extend:crossover)、16.1.9(extend:crossover_anova) | — | crossover_anova | PKPS |
| M3 原有 | M-SURVIVAL | 路由/内容合同 | `analyses.time_to_event` | 11.4.1(extend:survival) | — | time_to_event | FAS |
| M3 原有 | M-QT | 路由/内容合同 | `data_domains.ecg_intensive` | 12.5(extend:intensive_ecg) | — | ecg、qt | SAF |
| M3 原有 | M-HV | 路由/内容合同 | `population.healthy_volunteer` | 11(override:healthy_volunteer)、12(override:healthy_volunteer) | — | pk、safety | SAF |
| M4.1 新增 | M-LOCAL-TOLERANCE | 路由/内容合同 | `data_domains.local_tolerance` | 12.2(extend:local_tolerance_ae_subset) | s12-route-specific-safety.md | teae_local_tolerance_soc_pt、trae_local_tolerance_soc_pt、skin_irritation、sensitization | SS |
| M4.1 新增 | M-AE-CONSEQUENCE | 路由/内容合同 | `data_domains.ae_consequence_strata` | 12.2(extend:ae_consequence_pairs) | s12-route-specific-safety.md | teae_dose_reduction、trae_dose_reduction、teae_interruption、trae_interruption、teae_discontinuation、trae_discontinuation | SS |
| M4.1 新增 | M-TOPICAL-PK | 路由/内容合同 | `design.route_of_administration == topical && data_domains.pk_concentration` | 11.B(add:topical_low_systemic_exposure)、9.5.4(extend:topical_pk_sampling) | s11-topical-pk.md | pk_concentration_d1、pk_concentration_d28、topical_pk、below_quantification_limit、power_model_dose_proportionality | PKCS、PKPS |
| M4.1 新增 | M-INHALATION | 路由/内容合同 | `design.route_of_administration == inhalation` | 12.5(extend:inhalation_safety) | s12-route-specific-safety.md | pulmonary_function、spirometry、inhalation_safety | SAF |
| M4.1 新增 | M-OPHTHALMIC | 路由/内容合同 | `design.route_of_administration == ophthalmic` | 12.5(extend:ophthalmic_safety) | s12-route-specific-safety.md | visual_acuity、intraocular_pressure、slit_lamp、ophthalmic_safety | SAF |
| M4.1 新增 | M-RANDOMIZED | 路由/内容合同 | `design.randomized` | 9.4.3(extend:randomization)、16.1.7(extend:randomization_schedule) | s9-randomization-blinding-controls.md | randomization、allocation、stratification_balance | FAS |
| M4.1 新增 | M-BLINDING | 路由/内容合同 | `design.blinding != open_label` | 9.4.6(extend:blinding) | s9-randomization-blinding-controls.md | blinding、unblinding、emergency_code_break | SAF |
| M4.1 新增 | M-PLACEBO-CTRL | 路由/内容合同 | `placebo in design.control_types` | 9.2(extend:placebo_control)、9.4.1(extend:placebo_matching) | s9-randomization-blinding-controls.md | treatment_assignment、placebo_control | FAS、SAF |
| M4.1 新增 | M-ACTIVE-CTRL | 路由/内容合同 | `active in design.control_types` | 9.2(extend:active_control)、9.4.1(extend:active_comparator) | s9-randomization-blinding-controls.md | treatment_assignment、active_control | FAS、SAF |
| M4.1 新增 | M-BE | 仅框架槽 | `design.study_type == bioequivalence \|\| bioequivalence in design.clinical_pharmacology_studies` |  | — | — | — |
| M4.1 新增 | M-DDI | 路由/内容合同 | `design.study_type == drug_drug_interaction \|\| drug_drug_interaction in design.clinical_pharmacology_studies` | 11.B(add:drug_drug_interaction) | s11-clinpharm-special-pop-endpoints.md | ddi_pk、geometric_mean_ratio、safety | PKPS、SAF |
| M4.1 新增 | M-FOOD-EFFECT | 路由/内容合同 | `design.study_type == food_effect \|\| food_effect in design.clinical_pharmacology_studies` | 11.B(add:food_effect) | s11-clinpharm-special-pop-endpoints.md | food_effect_pk、geometric_mean_ratio、safety | PKPS、SAF |
| M4.1 新增 | M-HEPATIC-IMPAIR | 路由/内容合同 | `(design.study_type == hepatic_impairment \|\| hepatic_impairment in population.special_populations) && data_domains.pk_concentration` | 11.B(add:hepatic_impairment_pk) | s11-clinpharm-special-pop-endpoints.md | special_population_pk、hepatic_impairment、safety | PKPS、SAF |
| M4.1 新增 | M-RENAL-IMPAIR | 路由/内容合同 | `(design.study_type == renal_impairment \|\| renal_impairment in population.special_populations) && data_domains.pk_concentration` | 11.B(add:renal_impairment_pk) | s11-clinpharm-special-pop-endpoints.md | special_population_pk、renal_impairment、safety | PKPS、SAF |
| M4.1 新增 | M-PEDIATRIC | 路由/内容合同 | `pediatric in population.special_populations` | 11.2(extend:pediatric)、12.5(extend:pediatric_safety) | s11-clinpharm-special-pop-endpoints.md | pediatric_demographics、pediatric_efficacy、pediatric_safety | FAS、SAF |
| M4.1 新增 | M-GERIATRIC | 路由/内容合同 | `older_adult in population.special_populations` | 11.2(extend:geriatric)、12.5(extend:geriatric_safety) | s11-clinpharm-special-pop-endpoints.md | geriatric_demographics、geriatric_efficacy、geriatric_safety | FAS、SAF |
| M4.1 新增 | M-INFECTION | 路由/内容合同 | `infection_microbiology in data_domains.therapeutic_area_endpoints` | 11.4.1(extend:infection_microbiology) | s11-clinpharm-special-pop-endpoints.md | microbiological_cure、pathogen_eradication、composite_cure | mITT、mPPS |
| M4.1 新增 | M-DERMATOLOGY | 路由/内容合同 | `dermatology_scales in data_domains.therapeutic_area_endpoints` | 11.4.1(extend:dermatology_scales)、11.4.2.1(extend:dermatology_ancova) | s11-clinpharm-special-pop-endpoints.md | iga_sd_success、iga_sd_change_from_baseline、erythema_total_score、scaling_total_score、wi_nrs、dlqi_total、ancova_adjusted_effect | FAS、mFAS |

## 治疗领域扩展槽

- 已实现：`infection_microbiology`、`dermatology_scales`。
- 仅声明、未实现内容模块：`oncology_response`、`cardiovascular_events`。

# M3 内容模块条件覆盖证据

> 根据规格 v1.2 §2.3.2 固化。每个模块与一个布尔 capability flag 一对一；`phase` 不出现在任何条件中。
> 字母扩展章的 `extend` 贡献只在存在同根 `add` 锚点时进入 `materialized_sections`；否则原样保留在 `unmaterialized_contributions` 并告警。

| # | 模块 | Capability | 五维事实条件 | 覆盖 |
|---:|---|---|---|---|
| 1 | M-CORE | `core` | 恒为真 | PASS |
| 2 | M-DOSE-ESC | `has_dose_escalation` | `design.escalation_scheme != none` | PASS |
| 3 | M-DLT | `has_dlt` | `design.dlt_defined` | PASS |
| 4 | M-MTD-RP2D | `determines_mtd_or_rp2d` | `objectives.determine_mtd \|\| objectives.determine_rp2d` | PASS |
| 5 | M-EXPANSION | `has_expansion_cohorts` | `design.expansion_cohorts > 0` | PASS |
| 6 | M-PK-NCA | `has_pk_nca` | `data_domains.pk_concentration && analyses.nca` | PASS |
| 7 | M-PK-POP | `has_population_pk` | `analyses.population_pk` | PASS |
| 8 | M-DOSE-PROP | `has_dose_proportionality` | `analyses.dose_proportionality` | PASS |
| 9 | M-EXPOSURE-RESP | `has_exposure_response` | `analyses.exposure_response` | PASS |
| 10 | M-ADA | `has_ada` | `data_domains.ada` | PASS |
| 11 | M-PD-BIOMARKER | `has_pd_biomarker` | `data_domains.pd_biomarker` | PASS |
| 12 | M-EFF-CONFIRM | `has_confirmatory_efficacy` | `objectives.confirmatory_efficacy && analyses.hypothesis_test` | PASS |
| 13 | M-EFF-EXPLORE | `has_exploratory_efficacy` | `objectives.efficacy && !analyses.hypothesis_test` | PASS |
| 14 | M-MULTIPLICITY | `has_multiplicity_control` | `analyses.multiplicity_control` | PASS |
| 15 | M-NONINF | `has_noninferiority` | `design.noninferiority` | PASS |
| 16 | M-ESTIMAND | `has_estimand_framework` | `analyses.estimand_framework` | PASS |
| 17 | M-INTERIM | `has_interim_analysis` | `design.interim_analysis` | PASS |
| 18 | M-SUBGROUP | `has_subgroup_analysis` | `analyses.subgroup` | PASS |
| 19 | M-MULTICENTER | `is_multicenter` | `design.n_centers > 1` | PASS |
| 20 | M-CROSSOVER | `is_crossover` | `design.type == crossover` | PASS |
| 21 | M-SURVIVAL | `has_time_to_event_analysis` | `analyses.time_to_event` | PASS |
| 22 | M-QT | `has_intensive_ecg` | `data_domains.ecg_intensive` | PASS |
| 23 | M-HV | `is_healthy_volunteer` | `population.healthy_volunteer` | PASS |

机械验证：`tests/test_m3_routing.py` 用两组合法事实的激活并集证明 23/23 个模块条件均可表达，并检查模块、flag 和条件文本的一对一契约。

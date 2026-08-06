# 研究事实卡：DEID-TOPICAL-PROJECT

> 状态：**待人工确认**。本卡是模块路由与方案事实白名单的确认入口；期别仅作元数据和合理性提示，不参与结构路由。

| 项目 | 已抽取事实 |
|---|---|
| 研究标题 | 脱敏外用制剂随机双盲安慰剂对照浓度递增研究 |
| 期别标签（非路由） | early-development |
| 研究目的 | 安全性/耐受性、PK 表征、剂量探索、疗效 |
| 设计 | parallel；研究类型=general_interventional；给药途径=topical；随机=是；对照=是；盲法=double_blind |
| 人群 | 患者；目标疾病=脱敏皮肤科适应症 |
| 数据域 | 安全性、疗效、PK 浓度、ECG、局部耐受性、AE 后果分层 |
| 分析模块 | 仅描述性、NCA |

## 关键方案事实

- 剂量递增：`cohort_concentration_escalation`；剂量水平：1 deidentified_level、2 deidentified_level、3 deidentified_level、4 deidentified_level；扩展队列：0。
- 对照类型：placebo；临床药理专项：无。
- 周期：未设置 天；DLT 窗：无。
- 主要终点：局部安全性评价。
- 分析集：FAS、mFAS、SS、PKCS、PKPS。
- PK 采样时点（h）：0、1、2、4、8、24。
- 治疗领域端点标签：dermatology_scales。
- AE 后果分层（减量/暂停/永久停药，TEAE/TRAE 成对）：是。

## 确定性路由结果

- 激活模块（13）：M-CORE、M-DOSE-ESC、M-PK-NCA、M-EFF-EXPLORE、M-MULTICENTER、M-LOCAL-TOLERANCE、M-AE-CONSEQUENCE、M-TOPICAL-PK、M-RANDOMIZED、M-BLINDING、M-PLACEBO-CTRL、M-DERMATOLOGY、M-ECG。
- 路由警告：无。

## 人工确认

- [ ] 已对照当前有效研究方案/SAP 核对五维事实。
- [ ] 已核对终点、分析集、剂量/周期/DLT 窗及 PK 时点。
- [ ] 已确认激活模块与预期 CSR 结构一致。

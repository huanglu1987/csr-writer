# 外用 Ib 期等价结构的模块路由对比

> 边界：本证据只使用脱敏合成的等价 `study_facts`，不含真实方案文本、抽取记录或可识别信息。

| 指标 | 扩展前（原 23 模块） | 扩展后（当前 41 模块） |
|---|---:|---:|
| 该结构激活数 | 5 | 13 |
| 新正确落位的模块 | 0 | 8 |

## 扩展前激活的 5 个模块

M-CORE、M-DOSE-ESC、M-PK-NCA、M-EFF-EXPLORE、M-MULTICENTER

## 新正确落位的模块

M-LOCAL-TOLERANCE、M-AE-CONSEQUENCE、M-TOPICAL-PK、M-RANDOMIZED、M-BLINDING、M-PLACEBO-CTRL、M-INFECTION、M-DERMATOLOGY

其中局部耐受性作为 14.3.1.3.x 的 AE 子集输入 CSR `12.2`，AE 后果分层覆盖减量/暂停/永久停药的 TEAE/TRAE 成对表；随机化落位 `9.4.3` 及 `16.1.7`，盲法/揭盲落位 `9.4.6`。

## 只在外用等价结构中激活

M-AE-CONSEQUENCE、M-BLINDING、M-DERMATOLOGY、M-INFECTION、M-LOCAL-TOLERANCE、M-PLACEBO-CTRL、M-RANDOMIZED、M-TOPICAL-PK

## 期别不参与路由

- 只改 `Ib` → `III`：结构完全相等 = `true`。
- 前后 SHA-256：`cd5a9a96eee2d7582294ece9bba89d8e1fd9c571ea6ee6027728f2ea470dfa38` / `cd5a9a96eee2d7582294ece9bba89d8e1fd9c571ea6ee6027728f2ea470dfa38`。

## 明确的扩展槽

已实现感染/微生物学与皮肤科量表两个示例。肿瘤缓解和心血管事件只在 schema 中声明为扩展槽，本轮不生成对应内容模块。

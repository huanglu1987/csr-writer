#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成脱敏合成「结果 TFL」——结构对标真实 shell，数值与术语全部合成。

设计目标：能测出 shell 阶段延后的行在结果阶段能否正确建键。
覆盖四类真实形态：
  A 统计量行表（ANCOVA）        —— shell 已建键，结果应一一对应
  B SOC/PT 层级表               —— shell 延后，结果按真实层级建键；含跨 SOC 同名 PT
  C 用药表（ATC/药物名）         —— shell 延后，行数与 shell 占位行不同
  D PK 参数表（参数行 × 剂量组列）—— 含 spanner 表头
"""
from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import random

random.seed(20260805)   # 固定种子，可复现

GROUPS = ["安慰剂", "0.5% BID", "0.75% BID", "1.0% BID", "1.0% QD", "试验药Total", "Total"]
N = [12, 12, 12, 12, 12, 48, 60]

doc = Document()

def style(run, size=9, bold=False):
    run.font.name = "Times New Roman"; run.font.size = Pt(size); run.font.bold = bold
    rPr = run._element.get_or_add_rPr()
    rF = rPr.find(qn('w:rFonts'))
    if rF is None:
        rF = OxmlElement('w:rFonts'); rPr.insert(0, rF)
    for a in ('w:ascii','w:hAnsi','w:cs'): rF.set(qn(a), "Times New Roman")
    rF.set(qn('w:eastAsia'), "宋体")

def title(text, level=0):
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    style(p.add_run(text), 10.5, True)

def note(text):
    p = doc.add_paragraph(); style(p.add_run(text), 8)

def table(header_rows, body_rows):
    ncol = len(header_rows[0])
    t = doc.add_table(rows=0, cols=ncol); t.style = 'Table Grid'
    for hr in header_rows:
        cells = t.add_row().cells
        for i, v in enumerate(hr):
            cells[i].text = ""; style(cells[i].paragraphs[0].add_run(v), 8, True)
    for br in body_rows:
        cells = t.add_row().cells
        for i, v in enumerate(br):
            cells[i].text = ""; style(cells[i].paragraphs[0].add_run(v), 8)
    doc.add_paragraph()

def npct(n, d):
    return f"{n} ({100*n/d:.1f})" if d else "0"

# ══════════════ A. 统计量行表：ANCOVA（shell 已建键，应一一对应）══════════════
for tid, pop, ep in [("表14.2.2.1.1", "FAS", "IGA-SD评分"), ("表14.2.2.1.2", "mFAS", "IGA-SD评分")]:
    title(f"{tid} {ep}较基线变化值-ANCOVA分析 ({pop})")
    hdr = [["统计量"] + GROUPS[:5]]
    rows = []
    for lab, fmt in [("N", "{:d}"), ("基线 Mean (SD)", None), ("第28天 Mean (SD)", None),
                     ("较基线变化值 Mean (SD)", None), ("LS Mean (SE)", None),
                     ("LS Mean Difference (SE)", None), ("95% CI", None), ("P 值", None)]:
        r = [lab]
        for gi in range(5):
            if lab == "N": r.append(str(N[gi]))
            elif lab == "95% CI":
                r.append("—" if gi == 0 else f"(-{random.uniform(0.4,0.9):.2f}, -{random.uniform(0.05,0.35):.2f})")
            elif lab == "P 值":
                r.append("—" if gi == 0 else f"{random.uniform(0.008,0.14):.4f}")
            elif "Difference" in lab:
                r.append("—" if gi == 0 else f"-{random.uniform(0.3,0.7):.2f} ({random.uniform(0.1,0.2):.2f})")
            else:
                r.append(f"{random.uniform(1.8,3.2):.2f} ({random.uniform(0.4,0.9):.2f})")
        rows.append(r)
    table(hdr, rows)
    note("注：ANCOVA 模型包含试验组别为固定效应，基线值作为协变量。LS Mean Difference 与 P 值为试验药相较于安慰剂的组间差异。")
    note("【合成数据｜非临床证据】")

# ══════════════ B. SOC/PT 层级表：含跨 SOC 同名 PT（shell 延后）══════════════
SOC_PT = {
    "皮肤及皮下组织类疾病": ["用药部位红斑", "用药部位瘙痒", "用药部位烧灼感", "接触性皮炎"],
    "各类神经系统疾病": ["头痛", "头晕"],
    "全身性疾病及给药部位各种反应": ["用药部位红斑", "乏力"],   # ← 跨 SOC 同名 PT
    "胃肠系统疾病": ["恶心"],
}
for tid, name in [("表14.3.1.3.1", "TEAE按SOC和PT分析"), ("表14.3.1.3.3", "局部耐受性TEAE按SOC和PT分析")]:
    title(f"{tid} {name} (SS)")
    hdr = [["系统器官分类\n  首选术语"] + [f"{g}\n(N={n})\nn (%)" for g, n in zip(GROUPS, N)]]
    rows = [["汇总"] + [npct(random.randint(int(n*0.5), int(n*0.9)), n) for n in N]]
    for soc, pts in SOC_PT.items():
        if "局部耐受" in name and "皮肤" not in soc and "给药部位" not in soc:
            continue
        rows.append([soc] + [npct(random.randint(1, max(2, int(n*0.5))), n) for n in N])
        for pt in pts:
            rows.append([f"  {pt}"] + [npct(random.randint(0, max(1, int(n*0.3))), n) for n in N])
    table(hdr, rows)
    note("注：在SOC层级里，1个参与者可能有1条以上PT记录，此参与者在每一条PT和SOC层级下均算作1次。")
    note("注：不良事件采用MedDRA 28.1版本进行编码。")
    note("【合成数据｜非临床证据】")

# ══════════════ C. 用药表：ATC/药物名（shell 延后，行数与占位不同）══════════════
ATC = {
    "D01 皮肤病用抗真菌药": ["酮康唑乳膏", "特比萘芬乳膏"],
    "N02 镇痛药": ["对乙酰氨基酚"],
    "R06 抗组胺药": ["氯雷他定", "西替利嗪"],
}
title("表14.1.4.2 合并用药 (FAS)")
hdr = [["ATC分类\n  药物名称"] + [f"{g}\n(N={n})\nn (%)" for g, n in zip(GROUPS, N)]]
rows = [["任何合并用药"] + [npct(random.randint(int(n*0.3), int(n*0.7)), n) for n in N]]
for atc, drugs in ATC.items():
    rows.append([atc] + [npct(random.randint(1, max(2, int(n*0.4))), n) for n in N])
    for d in drugs:
        rows.append([f"  {d}"] + [npct(random.randint(0, max(1, int(n*0.25))), n) for n in N])
table(hdr, rows)
note("注：合并用药采用WHODrug Global B3 2025年3月版编码。")
note("【合成数据｜非临床证据】")

# ══════════════ D. PK 参数表：spanner 表头 ══════════════
title("表14.2.4.3.1 ZYG24002单次给药 (D1)血浆PK参数 (PKS)")
hdr = [
    ["参数", "单位", "0.5% BID", "", "1.0% BID", ""],
    ["", "", "N", "Geo Mean (CV%)", "N", "Geo Mean (CV%)"],
]
rows = []
for p, u, lo, hi in [("Cmax", "ng/mL", 0.30, 0.95), ("Tmax", "h", 4.0, 8.0),
                     ("AUC0-t", "h*ng/mL", 4.0, 12.0), ("AUC0-inf", "h*ng/mL", 4.5, 13.0),
                     ("t1/2", "h", 10.0, 20.0)]:
    r = [p, u]
    for _ in range(2):
        r += ["12", f"{random.uniform(lo,hi):.2f} ({random.uniform(18,42):.1f})"]
    rows.append(r)
table(hdr, rows)
note("注：Tmax 描述中位数（最小值，最大值）。低于定量下限的浓度按 0 处理。")
note("注：分析方法定量下限（LLOQ）为 0.10 ng/mL。")
note("【合成数据｜非临床证据】")

# ══════════════ 受试者分布（宽表）══════════════
title("表14.1.1.1 参与者分布 (All Screened Set)")
hdr = [["", "例数 (%)"]]
rows = [["筛选例数", "76"], ["筛选失败例数", "16 (21.1)"], ["  不符合入选标准", "9 (56.3)"],
        ["  符合排除标准", "5 (31.3)"], ["  受试者撤回知情", "2 (12.5)"],
        ["随机例数", "60"], ["接受研究治疗例数", "60 (100.0)"],
        ["完成研究例数", "56 (93.3)"], ["提前退出例数", "4 (6.7)"],
        ["  不良事件", "2 (50.0)"], ["  失访", "1 (25.0)"], ["  受试者撤回知情", "1 (25.0)"]]
table(hdr, rows)
note("【合成数据｜非临床证据】")

out = "/private/tmp/claude-501/-Users-huanglu-----------/0e122a14-5137-46e8-8550-faaea58479fb/scratchpad/合成结果TFL.docx"
doc.save(out)
print("已生成:", out)
print(f"表格数: {len(doc.tables)}")

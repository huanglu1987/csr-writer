# M3 最终验证摘要

验证日期：2026-08-04  
范围：仅 M1 + M2 + M3；未执行 M4。

## 结果

- 完整自动回归：73/73 PASS。
- M3 一键端到端：PASS。
- 中文数量表述：12/12 PASS；`failed = 0`。
- `study_facts` Draft 2020-12 schema 和演示事实：PASS。
- 内容模块：23/23 激活条件可表达。
- 四条判定路由：4/4 PASS。
- 仅改 `phase`：结构 diff 为 0 字节；前后 SHA-256 均为
  `7ad29d8a7c0ce6f3d2e7abd70274e8a211d756196c033b2317f9f205744e849f`。
- DOCX 风险 fixture：ZIP 包完整，独立 OOXML 校验
  `All validations PASSED!`。源文件前后 SHA-256 均为
  `a5bd8d1cac5957eb6fc33ad84c568bf3d3a558231e4b75717f9bbbea08a15881`。
- 深度抽取：7 条非空记录、3 个 story、1 张表、2 个单元格、
  1 个文本框、1 处插入、2 处删除、6 个批注标记、3 个隐藏 run；
  正确产生未接受修订、审阅批注、隐藏文本三类警告。
- §11.4：6 个占位符，A/B/C 守卫后 resolve PASS。
- §11.A：2 个占位符，A/B/C 守卫后 resolve PASS；
  使用明确标记的合成 DLT 验收夹具，非临床证据。
- Python 源码编译检查：PASS。

## 附加环境发现

全局 `pip check` 报告本机现有 `typer-slim 0.24.0` 要求
`typer >= 0.24.0`，而环境中为 `typer 0.21.2`。CSR Writer 未依赖 Typer，
因此没有修改用户的全局环境；本项目的 73 项测试、CLI 和端到端运行不受影响。

## 复现入口

```bash
cd "/Users/huanglu/临床试验总结报告撰写/csr-writer"
tests/run_m3_e2e.sh
```

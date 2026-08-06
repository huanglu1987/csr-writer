# CSR Writer v1.1.0：Codex 安装与首次运行

## 1. 运行环境

- macOS；Python 3.10 或更高版本。
- 依赖：`python-docx`、`lxml`、`PyYAML`。
- 不需要外部模型 API 或 pandoc。
- **LibreOffice 为可选**：装了才能跑 v1.1.0 的排版视觉预检（渲染 + 缩略图墙）；
  没装时流水线照常跑完，视觉检查记为 `not_run`，不影响其余判定。
- 真实方案、SAP、TFL 和安全性资料始终放在项目目录之外，以只读路径和 SHA-256
  绑定；不要复制进代码仓库。

## 2. 从零安装

```bash
cd csr-writer   # clone 后进入仓库目录
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .
python3 -m csr_writer --help
```

安装后可确认版本：

```bash
python3 -c 'import csr_writer; print(csr_writer.__version__)'
```

预期为 `1.1.0`。

## 3. 目录用途

| 路径 | 用途 |
|---|---|
| `csr_writer/` | 全部运行时逻辑 |
| `skills/csr-writer/references/` | schema、模块、标题覆盖、产出方式等受控配置 |
| `skills/csr-writer/section-playbooks/` | 结果类章节 playbook |
| `skills/csr-writer/assets/` | DOCX 母版和静态资产 |
| `scripts/` | 回归和受控演示入口 |
| `tests/` | 自动化回归 |
| `examples/fixtures/` | 脱敏或合成夹具，只读 |
| `examples/output/m21/` | 本次统一合卷、追溯、QC 和收尾证据 |

项目根目录上一级的四份规格源文件是只读真相源。标题本地化只能写入覆盖层，不能
直接修改 ICH E3 章节树。

## 4. 首次验证

```bash
cd csr-writer   # clone 后进入仓库目录
python3 scripts/delivery_self_check.py
python3 -m unittest tests.test_codex_platform_boundary
```

本次冻结基线为 312 项测试全部通过；平台边界测试独立通过。测试数量将随后续新增
测试变化，判断标准是退出码为 0，而不是固定数量。

## 5. 首次脱敏演练

输出目录必须不存在。不要输出到 `/private/tmp` 或系统临时目录——
某些终端安全软件会在那里加密落盘文件，导致后续读取失败：

```bash
python3 scripts/run_m21_dual_e2e.py \
  --output ~/csr-writer-m21-first-run
```

成功后重点查看：

- `M21_dual_e2e_report.json`：总闸门与两项目逐步结果；
- `M21端到端断点清单.md`：阻断性断点；
- `m20_unfamiliar/07_unified_composition/`：单臂 clean/annotated；
- `ib_randomized_double_blind/08_unified_composition/`：Ib clean/annotated。

该命令只使用仓库内脱敏材料，不访问真实临床文件。

## 6. 常见阻断及含义

| 错误 | 含义 | 处理人 |
|---|---|---|
| `E-M21-READ-ONLY-HASH-DRIFT` | 只读真相源或夹具发生变化 | 文档控制/开发维护人 |
| `E-TFL-MAP-DRIFT` | 结果 TFL 与冻结 shell 语义结构不一致 | 统计编程与医学写作共同裁决 |
| `E-TFL-DRIFT-DECISION-INCOMPLETE` | 漂移队列尚有未裁决项 | 统计负责人 |
| `E-COMPOSE-GUARD-PROOF` | 正文没有有效守卫通过证明 | 当前节起草与复核人 |
| `E-COMPOSE-UNRESOLVED` | 仍有占位符或未解析值 | 统计编程/数据管理 |
| `E-CHAPTER14-UNROUTED` | 表图无法由明确映射归入第 14 章 | 统计编程与医学写作共同确认 |
| clean 输出被阻断 | 尚有待医学确认、待补资料或未关闭决定 | 医学负责人/申办方 |

不要通过修改夹具、删除待确认标记或降低守卫严格度来消除错误。

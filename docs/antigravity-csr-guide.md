# 在 Mac 上用 Antigravity 写 CSR

以下命令都发到 **Antigravity 对话框**。安装一次，以后每个研究直接从第 2 步开始。

<a id="install"></a>

## 1. 第一次用，复制这一条安装

```text
/goal 请在这台 Mac 上安装 CSR Writer。请将 https://github.com/huanglu1987/csr-writer/archive/refs/heads/master.zip 下载到临时目录并解压，用 /bin/bash 运行其中的 scripts/install-antigravity-mac.sh，看到“安装成功”后告诉我。
```

如果 Antigravity 提示允许运行安装脚本，点允许。看到 **安装成功** 后，新开一个会话，输入 `/csr-writer` 就能看到撰写技能。以后更新也发同一条安装指令。

<a id="workspace"></a>
<a id="draft"></a>

## 2. 给一个资料夹，拿到 Word 初稿

在 Antigravity 打开这个研究的项目文件夹。把方案、统计分析计划（SAP）、逐例数据和手头已有的定稿表放在其中的 `输入资料` 文件夹。复制下句，输入 `@` 时选中该文件夹。如果它叫别的名字，选实际的文件夹即可。

```text
/csr-writer 请用 @输入资料 生成 CSR 的 Word 初稿，核对统计表，把 Word 和集中审阅清单的路径发给我。
```

<a id="outputs"></a>

打开 **CSR内部审阅初稿.docx** 看报告，打开 **集中审阅清单.md** 看缺哪些原件、哪些问题需要负责人决定。默认两份文件都在输入资料夹旁边新建的 `输入资料_CSR初稿` 中。

<a id="answers"></a>

## 3. 负责人答复后，更新这一稿

在同一会话粘贴下句，把方括号换成按清单编号整理的实际答复，附上答复人和日期。

```text
/csr-writer 请按以下负责人答复更新上一稿，同步核对正文、概要和附录，把新版 Word 和剩余待决事项发给我。[在此粘贴答复]
```

<a id="demo"></a>
<a id="commands"></a>
<a id="troubleshooting"></a>

需要时再查[模拟项目演练](antigravity-csr-reference.md#demo)、[其他斜杠命令](antigravity-csr-reference.md#commands)或[故障排查](antigravity-csr-reference.md#troubleshooting)。

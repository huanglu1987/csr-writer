"""Install the public drafting runtime and its Antigravity skill in the user's folders."""
import fcntl
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONTENTS = (
    "csr_writer", "skills/csr-writer", "examples/antigravity-demo",
    "tests/__init__.py", "tests/test_antigravity_quickstart.py",
    "README.md", "AGENTS.md", "pyproject.toml", "docs/智能体起草指南.md",
)


def package_files():
    for name in CONTENTS:
        source = ROOT / name
        paths = sorted(source.rglob("*")) if source.is_dir() else [source]
        for f in paths:
            if f.is_file() and "__pycache__" not in f.parts and f.name != ".DS_Store" and f.suffix != ".pyc":
                yield f.relative_to(ROOT)


def entry_text(current):
    python = shlex.quote(str(current / "env/bin/python"))
    tool = current / "tool"
    return f"""---
name: csr-writer
description: 根据研究资料文件夹生成中文 CSR Word 初稿、统计对账和集中审阅清单，也可按负责人答复生成修订稿。
---

先读取 `{tool}/AGENTS.md` 和 `{tool}/skills/csr-writer/SKILL.md` 的起草规则。Mac 安装版的运行入口如下，输入路径使用用户指定的研究资料夹，默认在其旁边新建输出目录。

```bash
{python} -m csr_writer draft-project --input-dir "研究资料夹的实际路径"
```

使用当前资料、当次选用的 SAP 解读与表题匹配。遇到有原件可解决的问题按 `{tool}/docs/智能体起草指南.md` 处理。
首稿完成后提供 Word 和集中审阅清单的可打开路径。用户给出负责人意见时，依据原模板记录实际答复，加 --answers 另生成一版，并核对正文、概要和附录。尚未答复的事项保留在清单中。
"""


def verify(folder):
    subprocess.run(
        [folder / "env/bin/python", "-m", "unittest", "-v", "tests.test_antigravity_quickstart"],
        cwd=folder / "tool", check=True,
    )


def install(user_dir):
    app = user_dir / "Library/Application Support/CSR Writer"
    releases = app / "installs"
    releases.mkdir(parents=True, exist_ok=True)
    current = app / "current"
    skill = user_dir / ".gemini/config/skills/csr-writer/SKILL.md"
    content = entry_text(current).encode("utf-8")

    with (app / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if current.exists() and not current.is_symlink():
            raise SystemExit(f"安装位置已有同名文件或目录，请检查 {current}")
        previous_entry = current / "installed-skill.md"
        if skill.parent.is_symlink() or skill.is_symlink() or (skill.exists() and not skill.is_file()):
            raise SystemExit(f"已有同名自定义技能，请先检查或备份 {skill.parent} 再重试。")
        existing = skill.read_bytes() if skill.exists() else None
        if existing is not None and existing != content and (
            not previous_entry.is_file() or previous_entry.read_bytes() != existing
        ):
            raise SystemExit(f"已有同名自定义技能，请先检查或备份 {skill.parent} 再重试。")
        files = list(package_files())
        fingerprint = hashlib.sha256(content)
        for rel in files:
            fingerprint.update(os.fsencode(rel) + b"\0" + hashlib.sha256((ROOT / rel).read_bytes()).digest())
        version = fingerprint.hexdigest()
        prior = current / "package.sha256"
        if prior.is_file() and prior.read_text() == version and (current / "env/bin/python").is_file():
            folder = current.resolve()
            try:
                verify(folder)
            except (OSError, subprocess.CalledProcessError):
                folder = None  # Build a replacement, leaving the existing installation intact.
        else:
            folder = None

        if folder is None:
            folder = Path(tempfile.mkdtemp(prefix="csr-", dir=releases))
            try:
                tool = folder / "tool"
                for rel in files:
                    (tool / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ROOT / rel, tool / rel)
                subprocess.run([sys.executable, "-m", "venv", folder / "env"], check=True)
                subprocess.run([folder / "env/bin/python", "-m", "pip", "--disable-pip-version-check",
                                "install", "-e", ".[source-office,models,visual]"], cwd=tool, check=True)
                verify(folder)
                (folder / "installed-skill.md").write_bytes(content)
                (folder / "package.sha256").write_text(version)
            except BaseException:
                shutil.rmtree(folder)
                raise

        # Verification precedes activation. The active environment has a stable path across upgrades.
        skill.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=app) as staging:
            staged = Path(staging) / "current"
            staged.symlink_to(folder, target_is_directory=True)
            if existing != content:
                with tempfile.TemporaryDirectory(dir=skill.parent) as temp:
                    prepared = Path(temp) / "SKILL.md"
                    prepared.write_bytes(content)
                    if existing is None:
                        os.link(prepared, skill)
                    else:
                        os.replace(prepared, skill)
            os.replace(staged, current)
    print("安装成功。请在 Antigravity 新会话中输入 /csr-writer 开始撰写。")


if __name__ == "__main__":
    install(Path(sys.argv[1]).expanduser().resolve())

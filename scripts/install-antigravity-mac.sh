#!/bin/bash
# Bootstrap a user-local Python only when the Mac lacks a suitable interpreter.
set -euo pipefail
if [[ "$(uname -s)" != Darwin ]]; then
    echo '这个安装入口适用于 macOS。' >&2
    exit 1
fi
script_dir="$(cd "$(dirname "$0")" && pwd)"
user_dir="${CSR_WRITER_USER_DIR:-$HOME}"
app_dir="$user_dir/Library/Application Support/CSR Writer"
# Finder-launched applications may not inherit the shell's Homebrew or Python.org PATH.
if [[ "${CSR_WRITER_MANAGED_PYTHON:-0}" != 1 ]]; then
    for candidate in "$(command -v python3 || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
        [[ -x "$candidate" ]] || continue
        if [[ "$candidate" == /usr/bin/python3 ]] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
            continue
        fi
        if "$candidate" -c 'import ensurepip, sys, venv; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))' >/dev/null 2>&1; then
            exec "$candidate" "$script_dir/install_antigravity_mac.py" "$user_dir"
        fi
    done
fi

echo '正在为 CSR Writer 准备独立的 Python 3.12…'
mkdir -p "$app_dir/bootstrap"
uv="$app_dir/bootstrap/uv"
if [[ ! -x "$uv" ]]; then
    installer="$(mktemp "$app_dir/bootstrap/uv-installer.XXXXXXXX")"
    trap 'rm -f "$installer"' EXIT
    /usr/bin/curl --fail --silent --show-error --location --retry 2 --proto '=https' --proto-redir '=https' https://astral.sh/uv/install.sh -o "$installer"
    UV_UNMANAGED_INSTALL="$app_dir/bootstrap" /bin/sh "$installer"
    rm -f "$installer"
    trap - EXIT
fi
export UV_PYTHON_INSTALL_DIR="$app_dir/bootstrap/python"
export UV_PYTHON_BIN_DIR="$app_dir/bootstrap"
export UV_CACHE_DIR="$app_dir/bootstrap/cache"
(cd "$app_dir/bootstrap" && "$uv" --no-config python install 3.12)
managed_python="$(cd "$app_dir/bootstrap" && "$uv" --no-config python find --managed-python 3.12)"
exec "$managed_python" "$script_dir/install_antigravity_mac.py" "$user_dir"

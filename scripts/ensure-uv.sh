#!/bin/bash
# 确保 uv 可用：先尝试常见安装路径，仍缺失则打印安装引导并退出。
# 被各 bin 入口（agents-remote / cla / cl / cx / cdx）source。

if ! command -v uv &>/dev/null; then
    for _p in "$HOME/.local/bin" "$HOME/.cargo/bin" "/opt/homebrew/bin" "/usr/local/bin"; do
        [ -x "$_p/uv" ] && export PATH="$_p:$PATH" && break
    done
fi

if ! command -v uv &>/dev/null; then
    echo "" >&2
    echo "❌ 缺少依赖：uv（agents-remote 运行时用它来管理 Python 依赖并启动）" >&2
    echo "" >&2
    echo "安装方式（任选其一）：" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh   # 官方脚本（推荐）" >&2
    echo "  brew install uv                                    # macOS Homebrew" >&2
    echo "  pip install uv                                     # 已有 pip 时" >&2
    echo "" >&2
    echo "装好后重开终端（或 source ~/.zshrc / ~/.bashrc）再运行。" >&2
    exit 1
fi

#!/bin/bash
# 升级前尝试停止旧的 lark 客户端（失败不报错）
agents-remote lark stop 2>/dev/null || true
exit 0

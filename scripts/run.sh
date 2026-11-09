#!/usr/bin/env bash
# 直播源更新入口（GitHub Actions / 本机 cron 通用）
# 职责只有一个：跑检测 → 生成 output/ 下三个文件。
# 提交回仓库由 .github/workflows/update.yml 的「提交结果」步骤完成，
# 用的是仓库自带的 GITHUB_TOKEN，不需要任何额外令牌。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PY="$(command -v python3 || command -v python)"
if [ -z "${PY}" ]; then
  echo "[错误] 找不到 python3 / python"
  exit 1
fi

echo "== Python =="
"${PY}" -V

echo "== 开始抓取与检测 =="
"${PY}" scripts/update.py "$@" || exit 1

echo "== 完成，产物清单 =="
ls -lh output 2>/dev/null || echo "（output 目录为空，可能本轮全部源失效）"

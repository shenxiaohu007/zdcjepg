#!/usr/bin/env bash
# 直播源更新入口（Gitee Go / 本机 cron 通用）
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

echo "== Python =="
python3 -V || exit 1
echo "== 开始抓取与检测 =="
python3 scripts/update.py || exit 1

# 可选：把结果提交回仓库（配置了 GITEE_TOKEN 才执行）
if [ -n "${GITEE_TOKEN:-}" ]; then
  echo "== 提交结果 =="
  git config user.name "${GIT_WHO:-iptv-bot}"
  git config user.email "${GIT_MAIL:-iptv-bot@gitee.com}"
  git add output
  if git commit -m "auto: 更新直播源 $(date -u +%Y-%m-%d_%H%M)"; then
    git push "https://oauth2:${GITEE_TOKEN}@gitee.com/${GITEE_REPO}.git" "HEAD:${GITEE_BRANCH:-master}" \
      && echo "推送成功" || echo "推送失败，检查 GITEE_TOKEN / GITEE_REPO 权限"
  else
    echo "输出无变化，跳过提交"
  fi
else
  echo "未配置 GITEE_TOKEN，仅生成文件不提交"
fi

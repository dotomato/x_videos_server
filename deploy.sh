#!/usr/bin/env bash
# deploy.sh — 将本地最新代码推送到 GitHub 并部署到生产服务器
#
# 用法：
#   ./deploy.sh            # 仅部署（不提交）
#   ./deploy.sh "提交说明"  # 先提交当前变更，再部署
#
# 服务器信息：
#   主机：h1.tomatochen.top
#   用户：ubuntu
#   端口：22
#   项目路径：~/x_videos_server
#   服务名：x_videos_server.service

set -euo pipefail

SERVER_USER="ubuntu"
SERVER_HOST="h1.tomatochen.top"
SERVER_PORT="22"
SERVER_DIR="~/x_videos_server"
SERVICE_NAME="x_videos_server"

SSH="ssh -p ${SERVER_PORT} ${SERVER_USER}@${SERVER_HOST}"

# ── 1. 本地：可选提交 ────────────────────────────────────────────────────────
if [[ $# -ge 1 && -n "$1" ]]; then
    echo ">>> 提交本地变更：$1"
    git add -A
    git commit -m "$1" || echo "(无新变更，跳过提交)"
fi

# ── 2. 本地：推送到 GitHub ───────────────────────────────────────────────────
echo ">>> 推送到 GitHub..."
git push origin main

# ── 3. 远程：拉取代码 ────────────────────────────────────────────────────────
echo ">>> 在服务器上拉取最新代码..."
$SSH "cd ${SERVER_DIR} && git pull origin main"

# ── 4. 远程：重启服务 ────────────────────────────────────────────────────────
echo ">>> 重启服务 ${SERVICE_NAME}..."
$SSH "sudo systemctl restart ${SERVICE_NAME}"

# ── 5. 检查服务状态 ──────────────────────────────────────────────────────────
STATUS=$($SSH "sudo systemctl is-active ${SERVICE_NAME}")
if [[ "$STATUS" == "active" ]]; then
    echo ">>> 部署成功，服务状态：active"
else
    echo "!!! 服务状态异常：${STATUS}"
    $SSH "sudo journalctl -u ${SERVICE_NAME} --no-pager -n 20"
    exit 1
fi

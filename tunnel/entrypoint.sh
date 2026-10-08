#!/bin/sh
# ssh -L: <порт> в контейнере -> 127.0.0.1:<порт> на VPS (там слушает aaPanel).
# Без ключа/known_hosts НЕ стартуем молча: выходим с кодом 78 и понятной причиной в логах
# (docker compose logs aapanel-tunnel); /diag при этом показывает «SSH-туннель не отвечает».
set -eu

die() { echo "aapanel-tunnel: $1" >&2; exit 78; }

[ -n "${TUNNEL_VPS_HOST:-}" ] || die "не задан TUNNEL_VPS_HOST (IP/имя VPS с aaPanel) в .env"
[ -f /ssh-src/id_ed25519 ] || die "нет ключа secrets/tunnel/id_ed25519 (создай по docs/v2/aapanel-tunnel-runbook.md)"
[ -f /ssh-src/known_hosts ] || die "нет secrets/tunnel/known_hosts (ssh-keyscan VPS, сверь отпечаток — см. runbook)"

PORT="${TUNNEL_PORT:-18839}"
USER_="${TUNNEL_VPS_USER:-tunnel}"
SSH_PORT="${TUNNEL_VPS_SSH_PORT:-22}"

# bind-mount с Windows даёт ключу права 0777 — ssh такой ключ отвергает; копируем и ужесточаем
mkdir -p /root/.ssh
cp /ssh-src/id_ed25519 /root/.ssh/id_ed25519
cp /ssh-src/known_hosts /root/.ssh/known_hosts
chmod 700 /root/.ssh
chmod 600 /root/.ssh/id_ed25519 /root/.ssh/known_hosts

echo "aapanel-tunnel: 0.0.0.0:${PORT} -> ${USER_}@${TUNNEL_VPS_HOST}:${SSH_PORT} -> 127.0.0.1:${PORT}"
# -M 0: автоперезапуск по ServerAlive (без отдельного порта мониторинга); StrictHostKeyChecking=yes:
# подмена хоста = отказ, а не молчаливое «принять новый ключ»; BatchMode: без интерактивных вопросов.
export AUTOSSH_GATETIME=30
exec autossh -M 0 -N \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/.ssh/known_hosts \
  -o BatchMode=yes -o IdentitiesOnly=yes -i /root/.ssh/id_ed25519 \
  -p "${SSH_PORT}" \
  -L "0.0.0.0:${PORT}:127.0.0.1:${PORT}" \
  "${USER_}@${TUNNEL_VPS_HOST}"

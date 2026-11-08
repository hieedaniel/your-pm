#!/usr/bin/env bash
# 一键部署 / 更新：在服务器上的 qieshen-live/ 目录里执行 ./deploy.sh
# 只做四件事：检查前置条件 → 构建 → 起容器 → 自检。不做任何删除动作。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${HOST_PORT:-19535}"
NAME="qieshen-live"
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

echo "== 1/4 前置检查 =="

if ! command -v docker >/dev/null 2>&1; then
  bad "没有 docker"; exit 1
fi
ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  bad "既没有 docker compose 插件，也没有 docker-compose"; exit 1
fi
ok "compose 可用（$DC）"

if [ ! -f config.json ]; then
  bad "缺少 config.json（里面是豆包 Key / WMA 凭据，需手动放好）"; exit 1
fi
ok "config.json 存在"

# 容器里以 uid 10001 运行，宿主机文件至少要让 others 可读
MODE=$(stat -c '%a' config.json 2>/dev/null || stat -f '%Lp' config.json 2>/dev/null || echo "")
if [ -n "$MODE" ]; then
  if [ "${MODE: -1}" -ge 4 ] 2>/dev/null; then
    ok "config.json 权限 $MODE（容器内可读）"
  else
    warn "config.json 权限是 $MODE，容器内 uid 10001 可能读不到 → chmod 644 config.json"
  fi
fi

# 端口占用：服务器上服务多，先拦住「端口已被别人占了」这种最难排查的故障
if command -v ss >/dev/null 2>&1; then
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"; then
    bad "端口 ${PORT} 已被占用："
    ss -lntp 2>/dev/null | grep -E "[:.]${PORT}$" || true
    echo "    换个端口：HOST_PORT=19536 ./deploy.sh"
    exit 1
  fi
  ok "端口 ${PORT} 空闲"
else
  warn "没有 ss 命令，跳过端口占用检查"
fi

echo "== 2/4 构建镜像 =="
$DC build
ok "镜像构建完成（$(docker images qieshen-live:1.0 --format '{{.Size}}' | head -1)）"

echo "== 3/4 启动 =="
$DC up -d --remove-orphans
ok "容器已启动"

echo "== 4/4 自检（最多等 40 秒）=="
for i in $(seq 1 40); do
  st=$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo unknown)
  if [ "$st" = "healthy" ]; then ok "健康检查通过"; break; fi
  if [ "$i" = "40" ]; then
    warn "40 秒内未变 healthy（当前 $st），最近日志："
    $DC logs --tail=40 || true
  fi
  sleep 1
done

echo
echo "本机探活："
python3 - "$PORT" <<'PY' || true
import json, sys, urllib.request
port = sys.argv[1]
try:
    d = json.load(urllib.request.urlopen("http://127.0.0.1:%s/api/state" % port, timeout=3))
    print("  HTTP 200   ASR=%s   WMA=%s   文档 %s 篇" % (
        (d.get("asr") or {}).get("provider"),
        (d.get("wma") or {}).get("provider"),
        len(d.get("docs") or [])))
except Exception as e:
    print("  探活失败：%s" % e)
PY

echo
echo "访问：http://127.0.0.1:${PORT}/"
echo "提示：浏览器麦克风只在 https 或 localhost 下可用；公网访问请用 Nginx 反代 + 证书"
echo "      （参考 nginx-qieshen-live.conf）。"

#!/bin/sh
# 启动前检查配置：镜像里刻意不打包 config.json（里面是豆包 Key 和 WMA 凭据），
# 所以没有挂载时要立刻失败并说清怎么挂，而不是让 server.py 抛一个看不懂的 FileNotFoundError。
set -e

: "${QS_CONFIG:=/app/config.json}"

if [ ! -f "$QS_CONFIG" ]; then
  cat >&2 <<EOF
======================================================================
[启动失败] 找不到配置文件：$QS_CONFIG
----------------------------------------------------------------------
配置文件里有豆包 ASR 的 X-Api-Key 和 WMA 的 ck_ 凭据，出于安全考虑
没有打进镜像。请用卷挂载进来，例如 docker-compose.yml 里：

  volumes:
    - ./config.json:/app/config.json:ro

注意宿主机上这份文件要能被容器里的 uid 10001 读到：
  chmod 644 config.json
======================================================================
EOF
  exit 1
fi

# 文件在但读不了：宿主上常见的 600 权限。这里拦住，
# 比让 server.py 抛 PermissionError 更好定位。
if [ ! -r "$QS_CONFIG" ]; then
  cat >&2 <<EOF
======================================================================
[启动失败] 配置文件存在但没有读权限：$QS_CONFIG
----------------------------------------------------------------------
容器内以 uid $(id -u 2>/dev/null || echo 10001) 运行，宿主机上执行：
  chmod 644 $(basename "$QS_CONFIG")
或把属主改成 10001：
  chown 10001:10001 $(basename "$QS_CONFIG")
======================================================================
EOF
  exit 1
fi

# 端口改了但健康检查还探旧端口？用同一个 QS_PORT，别让两处取值打架。
: "${QS_PORT:=19535}"
export QS_PORT
: "${QS_HOST:=0.0.0.0}"
export QS_HOST

echo "[entrypoint] 配置=$QS_CONFIG  监听=$QS_HOST:$QS_PORT  用户=$(id -un 2>/dev/null || echo app)"

exec "$@"

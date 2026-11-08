# syntax=docker/dockerfile:1
# 切身产品经理 · 实时音频链路
# 纯标准库实现（asyncio + 手写 WS/SSE），没有任何 pip 依赖，
# 所以不需要 pip install、不需要编译工具链，基础镜像即可跑。
#
# 国内服务器拉 Docker Hub 慢的话，构建时换一个镜像源即可，不用改文件：
#   docker compose build --build-arg BASE_IMAGE=registry.cn-hangzhou.aliyuncs.com/library/python:3.13-slim
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

# PYTHONUNBUFFERED：日志实时进 docker logs（不加会卡在缓冲区里）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QS_HOST=0.0.0.0 \
    QS_PORT=19535 \
    QS_CONFIG=/app/config.json \
    TZ=Asia/Shanghai

WORKDIR /app

# 只复制运行时真正会读的文件。测试脚本、探测脚本、构建脚本一概不进镜像。
COPY server.py wsproto.py doubao_asr.py orchestrator.py \
     mcp_client.py mcp_docs.py mcp_multi.py mcp_perf.py wma_agent.py ./
COPY index.html audio_worklet.js sample.wav ./
COPY docs-kaiyi/ ./docs-kaiyi/
COPY prompts/ ./prompts/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# 非 root 运行：容器被拿下时不给宿主留后门。
# uid/gid 10001 固定，方便宿主机上的 config.json 对齐权限。
# 显式建组：useradd --system 在部分基础镜像里不会自动建同名组，chown 会直接失败。
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app \
 && chown -R app:app /app
USER app

EXPOSE 19535

# 用 python 自己探，不装 curl（省一层镜像体积与依赖）。
# 壳式写法才能让 QS_PORT 生效。
HEALTHCHECK --interval=30s --timeout=3s --start-period=25s --retries=3 \
  CMD python -c "import os,urllib.request;u='http://127.0.0.1:%s/api/state'%os.environ.get('QS_PORT','19535');urllib.request.urlopen(u,timeout=2)" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-u", "server.py"]

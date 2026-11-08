# 部署到 Linux 服务器（Docker）

服务本身是纯标准库 Python（asyncio + 手写 WebSocket/SSE），没有 pip 依赖，
所以镜像很轻，构建也不需要外网装包（只需要能拉到 `python:3.13-slim`）。

- 容器内端口：**19535**
- 宿主机端口：**19535**（可用 `HOST_PORT` 改）
- 默认只绑 `127.0.0.1`：你机器上服务多，前面用现有的 Nginx 反代即可；直连测试见 §3

---

## 1. 要拷到服务器的东西

镜像由 GitHub Actions 构建（`.github/workflows/docker.yml`），服务器上**不需要 Dockerfile，
也不需要构建环境**。只需要两个小文件：

```
/opt/qieshen-live/
├── docker-compose.server.yml   ← 只拉镜像的编排（端口 19535）
└── config.json                 ← 凭据（豆包 Key / WMA ck_），不进镜像、不进仓库，手填
```

`config.json` 从仓库里的 `config.example.json` 复制一份改：

```bash
# 仓库名 your-pm，这里落到 /opt/qieshen-live 目录，下文路径都按这个目录写
git clone https://github.com/hieedaniel/your-pm.git /opt/qieshen-live
cd /opt/qieshen-live
cp config.example.json config.json
vi config.json                 # 填 asr.api_key、wma.acp.user_key 等
chmod 644 config.json          # 容器内以 uid 10001 运行，必须能读
```

> 只想改凭据、不想动镜像：改完 `config.json` 执行 `docker compose -f docker-compose.server.yml restart` 即可。

需要本地构建（老办法）时，整个目录都可以拷过去，运行必需的是：

```
qieshen-live/
├── Dockerfile              ← 镜像定义
├── docker-compose.yml      ← 本地构建版编排（端口 19535）
├── docker-compose.server.yml ← 服务器只拉镜像版编排
├── docker-entrypoint.sh    ← 启动前校验配置
├── deploy.sh               ← 一键部署脚本（构建路线用）
├── .dockerignore           ← 保证凭据/测试脚本不进镜像
├── config.json             ← 凭据，不打进镜像，靠挂载
├── server.py  wsproto.py  doubao_asr.py  orchestrator.py
├── mcp_client.py  mcp_docs.py  mcp_multi.py  mcp_perf.py  wma_agent.py
├── index.html  audio_worklet.js  sample.wav
├── docs-kaiyi/             ← 19 篇知识库
└── prompts/                ← 提示词 + 主题表（label 强校验要用）
```

---

## 2. 部署：四条路，按你的情况选

### 路线 A：从 GHCR 拉镜像（推荐，服务器零构建）

镜像地址：`ghcr.io/hieedaniel/your-pm:latest`，每次 push 到 `main` 自动重建。

GitHub 包默认是**私有**的，服务器要先登录一次（密码用 PAT，不是账号密码）：

```bash
# GitHub → Settings → Developer settings → Personal access tokens
#   勾 read:packages 即可
echo "<你的PAT>" | docker login ghcr.io -u hieedaniel --password-stdin

cd /opt/qieshen-live
docker compose -f docker-compose.server.yml pull
docker compose -f docker-compose.server.yml up -d
docker compose -f docker-compose.server.yml ps        # STATUS 应为 healthy
curl -s localhost:19535/api/state | head -c 200; echo
```

页面地址：

| 地址 | 用途 |
|---|---|
| `https://你的域名/` | 电脑端调试台（手机访问会自动切到 `/m`） |
| `https://你的域名/m` | 手机端精简版，APK 直接用这个 |
| `https://你的域名/?desk=1` | 手机上强制看电脑版 |
| `https://你的域名/kb` | 知识库管理（要口令） |

想公开拉取（不用登录）：GitHub → 你的头像 → Packages → `your-pm` → Package settings →
Change visibility → Public。

常用变体：

```bash
# 换端口（19535 被占时）
HOST_PORT=29535 docker compose -f docker-compose.server.yml up -d

# 临时直连测试（默认只绑 127.0.0.1）
BIND_ADDR=0.0.0.0 docker compose -f docker-compose.server.yml up -d

# 升级到新版本
docker compose -f docker-compose.server.yml pull && \
docker compose -f docker-compose.server.yml up -d

# 回滚到某次提交的镜像（Actions 里每个 commit 都有 sha- 标签）
IMAGE=ghcr.io/hieedaniel/your-pm:sha-1a2b3c4 \
  docker compose -f docker-compose.server.yml up -d
```

### 路线 B：在服务器上构建（仓库拉下来、不想用 GHCR）

```bash
cd /opt/qieshen-live
chmod 644 config.json          # 容器内以 uid 10001 运行，必须能读
bash deploy.sh                 # 检查 → 构建 → 启动 → 自检
```

`deploy.sh` 会依次做：docker/compose 是否可用 → `config.json` 是否存在且可读 →
**19535 是否被别的服务占了** → 构建 → `up -d` → 等健康检查 → 打一次 `/api/state`。

成功的话最后会打印：

```
  ✓ 健康检查通过
  HTTP 200   ASR=doubao   WMA=rule   文档 19 篇
```

国内服务器拉 Docker Hub 慢或超时，换镜像源（不用改文件）：

```bash
BASE_IMAGE=registry.cn-hangzhou.aliyuncs.com/library/python:3.13-slim bash deploy.sh
```

### 路线 C：本地构建，导出 tar 传上去（服务器不给外网时）

```bash
# 本机（有 Docker 的机器）
docker compose build
docker save qieshen-live:1.0 | gzip > qieshen-live-1.0.tar.gz

# 服务器
gunzip -c qieshen-live-1.0.tar.gz | docker load
cd /opt/qieshen-live && docker compose up -d        # 已有镜像不会重复构建
```

### 路线 D：推自己的私有 Registry（内网多机）

```bash
docker tag qieshen-live:1.0 registry.example.com/infra/qieshen-live:1.0
docker push registry.example.com/infra/qieshen-live:1.0
# 服务器上把 compose 里的 build: 删掉，image: 改成上面这个地址，然后 up -d
```

---

## 2.1 GitHub Actions 构建是怎么跑的

`.github/workflows/docker.yml`：

- **触发**：push 到 `main`、打 `v*` 标签、以及 Actions 页面手动 Run workflow；PR 只构建不推送
- **权限**：用仓库自带的 `GITHUB_TOKEN`（`packages: write`），不需要配任何密钥
- **标签策略**：
  - `latest` —— 只在 main 分支更新（PR / tag 不会把它覆盖成旧代码）
  - `sha-1a2b3c4` —— 每个提交一个，回滚用
  - `1.2` / `1.2.3` —— 打了 `v1.2.3` 标签时自动生成
- **架构**：默认 `linux/amd64`。要 ARM 就把文件里的 `PLATFORMS` 改成 `linux/amd64,linux/arm64`
- **冒烟检查**：构建完会用 `config.example.json` 起一次容器探 `/api/state`，
  这一步 `continue-on-error`，不阻塞镜像发布，但能在 Actions 日志里看到镜像到底能不能启动

看构建结果：仓库 → **Actions** → 左边 `docker` → 最近一次 Run → 底部 **Artifacts / Summary**
里有镜像地址和 digest。

---

## 2.2 知识库管理页与 MCP 接入

服务起来后访问 **`https://你的域名/kb`**（或 `http://127.0.0.1:19535/kb`）。

### 口令从哪来

三级来源，优先级从高到低：

1. 环境变量 `QS_ADMIN_TOKEN`（推荐，写进 `.env` 或 compose）
2. `config.json` 的 `admin.token`
3. 都没配 → 首次启动自动生成一个，落到 `data/.admin_token`，**重启不变**

自动生成的话，从日志里取：

```bash
docker logs qieshen-live | grep 管理页
# 管理页      : http://0.0.0.0:19535/kb   口令 xxxxxxxxxxxxxx（首次自动生成）
```

> `/api/kb/*` 全部要求 `X-Admin-Token` 请求头（或 `?token=`）。前端页面自己会存 localStorage，
> 但接口层是硬校验的——公网上没口令改不了知识库、也改不了 MCP 地址。

### 能导入什么

| 格式 | 处理方式 | 依赖 |
|---|---|---|
| `.md` / `.markdown` / `.txt` | 原文入库，`#` 标题自动切块 | 无 |
| `.docx` | 解 `word/document.xml`，标题样式转 `#` 分节 | **无**（不用 python-docx） |
| `.pdf` | pypdf 抽文字，按页分节 | `pypdf`（镜像已装） |
| `.html` | 去导航/页脚/脚本，保留正文 | 无 |
| 网页 URL | 同上，抓取后入库 | 无 |

- 单文件上限 **25MB**，单文档截断 **40 万字符**（防止一份大手册拖慢检索）
- 扫描版 PDF 抽不出文字，会明确返回「大概率是扫描件，需要 OCR」
- `data/kb/` 是唯一可写目录；`docs-kaiyi/` 内置 19 篇只读，删不掉
- 导入完成后**索引立即重建**，不需要重启，正在进行的会话下一句就能召回

### 数据放哪、怎么备份

```bash
/opt/qieshen-live/data/
├── kb/                # 上传的文档（*.md）+ _mcp_sources.json
└── .admin_token       # 自动生成的管理口令
```

compose 里已经挂了 `./data:/app/data`。**不挂这个卷的话，重建容器 = 上传的文档全丢。**

⚠️ 首次部署要先把目录建好并交给容器内的 uid（10001），否则容器写不进去，
表现是「导入失败」或「管理口令每次重启都变」：

```bash
mkdir -p /opt/qieshen-live/data
chown -R 10001:10001 /opt/qieshen-live/data
```

备份就一句：

```bash
tar -czf kb-backup-$(date +%F).tgz -C /opt/qieshen-live data/
```

### 加 MCP 知识源

管理页最下面「MCP 知识源」→ 填名称 / URL（streamable HTTP）/ Token / 权重 → 先点「测试」→
再「保存并生效」。

- 保存后立即重建检索入口，**不用重启容器**
- 多个源按 `weight` 合并排序；关掉某个源等于把它从检索里摘掉
- **本地库永远在列**：配了几个 MCP 源，本地 19 篇 + 上传文档都还参与检索并合并排序。
  远程源连不上时卡片由本地兜住，不会「一配 MCP 就什么都不出」
- 一个源都没配时，只用本地文档库，行为与之前完全一致
- 配置落在 `data/kb/_mcp_sources.json`，优先级高于 `config.json` 里的 `mcp.sources`

**工具名不用猜**：各家知识库暴露的工具名不一样（`docs.search` / `knowledge_search` /
`kb_query`…）。客户端会自动枚举 `tools/list`，挑出最像「检索」的那个，并按它的
`inputSchema` 把 `query` / `top_k` / `lang` 映射成对方要的字段名；返回结构也做了收敛
（`hits` / `results` / `items` / `data.list`…，标题取 `title`/`doc_name`，正文取
`snippet`/`content`/`chunk`）。所以接一个新源通常不用改代码。

两个按钮的分工：

| 按钮 | 验证什么 | 什么时候用 |
|---|---|---|
| 测试 | 握手 + `tools/list`：这个地址是不是 MCP、要什么凭据、有哪些工具 | 填完 URL 先点这个 |
| 试检索 | 拿一句真实客户话走完整检索：认到哪个工具、命中什么 | 「测试通过但没卡片」时点这个 |

以资料中心为例（内网地址，需公司网络/VPN）：

```text
URL   : https://newdims.uniview.com/request/mcp-svc/mcp
Token : 资料中心签发的访问令牌（走 Authorization: Bearer，不是 URL 参数、不是 X-Api-Key）
```

命令行等价：

```bash
T=$(cat /opt/qieshen-live/data/.admin_token)
curl -s -H "X-Admin-Token: $T" -H 'Content-Type: application/json' \
  -d '{"url":"https://newdims.uniview.com/request/mcp-svc/mcp","token":"<令牌>","query":"访客怎么登记"}' \
  localhost:19535/api/kb/sources/search-test | python3 -m json.tool
```

### 配了 MCP 但主题识别不出来 / 不给提示？

先跑自检，别猜：

```bash
curl -s localhost:19535/api/selfcheck | python3 -m json.tool
```

页面左侧也有 **「全链路自检」** 按钮，结论直接显示在按钮下方（绿=通，红=有问题）。
自检按「配置 → 词典 → 主题 → 检索」四层依次跑，每条都带可执行的下一步：

| 字段 | 看什么 |
|---|---|
| `build.sha` | 跑的是哪一版镜像。**为空 = 旧镜像**，先拉新镜像 |
| `taxonomy.topics` | 主题表里几个主题。`0` = 表是空的 |
| `rule.terms` | 本地规则词典条数。`0` = 表没灌进去，必然识别不出任何主题 |
| `samples` | 三句真实客户话实测出的主题，比任何配置都好判断 |
| `probe` | 用样例问题真检索一次；`hits: 0` 且 `error` 为空 = 资料里确实没有 |
| `verdict` / `actions` | 结论与下一步（页面上直接照着显示） |

常见组合与处理：

| 自检表现 | 含义 | 处理 |
|---|---|---|
| `build.sha` 为空 | 服务器跑的还是旧镜像（改的代码根本没上去） | `docker compose pull && docker compose up -d` |
| `rule.terms: 0` | 主题表没加载 | 重建主题表；重建后仍为 0 就是镜像旧 |
| `samples` 全空、`rule.terms` 很大 | 主题表里没有这些说法 | 把客户原话补进主题表的 `say` |
| `probe.ok: false` + `error` 有内容 | 远程源不通，已自动回退本地库 | 查该源 URL/Token，或临时停用 |

主题空另一个常见原因是**主题表不匹配**：主题表决定「一句话归到哪一类」，
模型和本地规则只能从表里选。导入自己的资料或接 MCP 之后，原来那张照 `docs-kaiyi`
写的 19 行表往往一个都对不上，于是主题空、卡片也跟着少。

管理页最下面「主题表」→ 点 **「按知识库重建」**：文档标题成为主题，front-matter 里的
`say` / `tags` 成为别名，手写表里的客户说法会保留。点完立即生效，不用重启。

命令行等价操作：

```bash
T=$(cat /opt/qieshen-live/data/.admin_token)
curl -s -H "X-Admin-Token: $T" -X POST -H 'Content-Type: application/json' \
  -d '{}' localhost:19535/api/kb/taxonomy/rebuild
```

重建结果落在 `data/kb/taxonomy.json`，下次启动优先用它。想回到仓库里那份手写的，删掉即可：

```bash
rm /opt/qieshen-live/data/kb/taxonomy.json && docker compose -f docker-compose.server.yml restart
```

三种「没卡片」的区分办法：

| 现象 | 含义 | 处理 |
|---|---|---|
| 顶栏显示「未匹配主题」+ 有卡片 | 主题表里没有这句，卡片靠原文检索正常出 | 正常；把相关资料导入后重建主题表 |
| 顶栏灰字「知识库没收录这类内容」 | 检索判定这句不在业务范围内 | 导入对应资料；否则宁可不推，推错卡更糟 |
| 「源异常」出现在状态抽屉 | 配的 MCP 源报错 | 回管理页点「测试」看具体错误 |

命令行也能做（脚本化时方便）：

```bash
T=$(cat /opt/qieshen-live/data/.admin_token)
curl -s -H "X-Admin-Token: $T" localhost:19535/api/kb/state | python3 -m json.tool
curl -s -H "X-Admin-Token: $T" -H 'Content-Type: application/json' \
     -d '{"title":"访客问答口径","text":"# 结论\n\n刷脸登记即可，不用前台电话确认。"}' \
     localhost:19535/api/kb/text
curl -s -H "X-Admin-Token: $T" -H 'Content-Type: application/json' \
     -d '{"sources":[{"name":"OA","url":"https://k.example.com/mcp","token":"xxx"}]}' \
     localhost:19535/api/kb/sources
```

| 接口 | 作用 |
|---|---|
| `GET /api/kb/state` | 文档数 / 块数 / 上传数 / 生效源 / pypdf 是否可用 |
| `GET /api/kb/docs` | 文档清单（区分内置与上传） |
| `POST /api/kb/upload` | `{filename, data(base64), tags}` |
| `POST /api/kb/text` | `{title, text, tags}` |
| `POST /api/kb/url` | `{url, tags}` |
| `POST /api/kb/delete` | `{doc_id}`，只对上传的文档有效 |
| `POST /api/kb/sources` | `{sources:[...]}`，保存并热生效 |
| `POST /api/kb/sources/test` | `{url, token}`，只探测不保存 |
| `POST /api/kb/reindex` | 手工重建索引（改了 `data/kb/` 里的文件后可用） |


---

## 3. 端口 19535 与「机器上已经有很多服务」

三件容易撞的事，deploy.sh 已经替你挡了两件：

| 风险 | 说明 | 处理 |
|---|---|---|
| 端口被占 | 19535 已被别的容器/进程监听 | `ss -lntp \| grep 19535` 查是谁；或 `HOST_PORT=19536 bash deploy.sh` |
| 只想要本机可访问 | 默认就是 `127.0.0.1:19535` | 无需改动，反代到它即可 |
| 临时想直连 IP 测试 | 默认不对外，直连会连不上 | `BIND_ADDR=0.0.0.0 bash deploy.sh` |

> 为什么默认不对外：这个服务没有登录态，谁能连上谁就能烧你的豆包 ASR 额度。
> 对外请走 Nginx（下面那节），不要直接 `-p 19535:19535` 裸奔。

容器名固定为 `qieshen-live`，与服务器上其它容器不冲突；网络用默认 bridge，
不占额外网段。日志已限 `10m × 3`，内存限 512M，不会拖垮同机其它服务。

---

## 4. 麦克风必须走 HTTPS（这一步别省）

浏览器的 `getUserMedia` 只在 **https** 或 **localhost** 下可用。用
`http://服务器IP:19535` 打开，点「开始录音」会立刻报
`getUserMedia unavailable` —— 这不是服务端问题。

两条路：

1. **只在自己电脑上看**：把 19535 端口用 SSH 隧道拉到本地，然后开 `http://127.0.0.1:19535`
   ```bash
   ssh -L 19535:127.0.0.1:19535 user@your-server
   ```
   localhost 属于安全上下文，麦克风可用，不需要证书。

2. **要给别人用**：Nginx 反代 + 证书。配置样例在同目录
   `nginx-qieshen-live.conf`，两个关键点（漏了会「连上但一个字都不出」）：
   - `/events`（SSE）：`proxy_buffering off`，否则字幕被 Nginx 缓冲区攒住，过一会儿一次性冒出来
   - `/audio`（WebSocket）：`Upgrade` / `Connection "upgrade"`，`proxy_read_timeout 3600s`

   ```bash
   sudo cp nginx-qieshen-live.conf /etc/nginx/conf.d/
   sudo sed -i 's/pm.example.com/你的域名/g' /etc/nginx/conf.d/nginx-qieshen-live.conf
   sudo nginx -t && sudo systemctl reload nginx
   ```

   服务本身没有登录态，公网暴露前建议加 `auth_basic`（配置里已留好注释）。

---

## 5. 日常运维

```bash
cd /opt/qieshen-live

docker compose ps                       # 状态 + 健康
docker compose logs -f --tail=100       # 实时日志
docker compose restart                  # 改完 config.json 后重启生效
docker compose down                     # 停止并删除容器（不动镜像与文件）
bash deploy.sh                          # 更新代码后：重建 + 重启 + 自检
docker stats qieshen-live               # 看 CPU / 内存
```

改 Key 不用重新构建：编辑宿主机上的 `config.json` → `docker compose restart`。

不想用 compose 也可以直接 `docker run`（参数与 compose 等价）：

```bash
docker run -d --name qieshen-live --restart unless-stopped \
  -p 127.0.0.1:19535:19535 \
  -e QS_PORT=19535 -e QS_HOST=0.0.0.0 \
  -v "$PWD/config.json:/app/config.json:ro" \
  --log-opt max-size=10m --log-opt max-file=3 \
  --memory=512m \
  qieshen-live:1.0
```

改知识库：默认要重新构建（`bash deploy.sh`）；如果只是频繁调文档，把
`docker-compose.yml` 里那行 `- ./docs-kaiyi:/app/docs-kaiyi:ro` 的注释去掉，
之后改文件只需 `docker compose restart`。

---

## 6. 常见故障对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 容器起来就退出，日志里 `[启动失败] 找不到配置文件` | 没挂载 `config.json` | 确认 compose 里 `./config.json:/app/config.json:ro`，且文件真的在这个目录 |
| 容器起来就退出，`PermissionError: '/app/config.json'` | 宿主机文件权限 600 | `chmod 644 config.json` |
| 宿主机 `curl 127.0.0.1:19535` 通，公网访问不了 | 默认只绑本机 | 正常；走 Nginx，或 `BIND_ADDR=0.0.0.0` |
| 页面能开，点录音报 `getUserMedia unavailable` | 非 HTTPS / 非 localhost | 见 §4 |
| 录音时字幕卡住，过一会儿一次性出来 | Nginx 缓冲了 SSE | `/events` 加 `proxy_buffering off` |
| 日志 `WS 已断开` 反复刷 | 反代没转发 Upgrade 头 | `/audio` 加上 `Upgrade` / `Connection "upgrade"` |
| `ASR=doubao` 但一直没字幕 | 豆包 Key 或 Resource ID 不对 | 打开 `/api/probe` 看真实握手结果；再确认「模型开通项目 = API Key 所属项目」 |
| `WMA=rule` | 企业订阅到期（控制面通、建会话被拦） | 这是账务问题，不影响演示；续费后重启自动切云端 |
| 容器内存持续上涨 | 极少见（纯 asyncio，正常 60–100MB） | `docker compose restart`，并把 `deploy.resources.limits` 收紧 |

---

## 7. 安全与体积

- **凭据不进镜像**：`config.json` 被 `.dockerignore` 排除，`docker history` / 镜像 tar 里都看不到 Key。
  容器启动时如果没挂载配置会直接失败并提示，不会带着空配置跑起来。
- **非 root 运行**：容器内用户 `app`（uid 10001），无 shell。
- **镜像大小**：约 150MB，其中 140MB 是 `python:3.13-slim` 基础镜像；业务代码加知识库不到 1MB。
- **无第三方依赖**：不需要 `pip install`，构建不依赖 PyPI，`python:3.13-slim` 拉下来就能编。

# your-pm · 切身产品经理（实时语音链路）

对着麦克风说话，实时出字幕 → 识别「这条产线/这个岗位的业务问题」属于哪个主题 →
在同一句话还没说完时把对应的产品资料卡片推到屏幕上。
用于售前对话、需求调研、方案讲解等「边说边给证据」的场景。

- **ASR**：火山引擎豆包流式语音识别（双向流式 WebSocket）
- **主题提取**：WMA 托管 Agent（云端提示词 + 封闭主题表）／本地规则兜底
- **知识库检索**：MCP（本地 19 篇凯翼园区文档），按主题加权召回
- **零第三方依赖**：asyncio + 手写 WebSocket / SSE，只用 Python 标准库

## 快速开始（Docker）

镜像由 GitHub Actions 自动构建并推送到 GHCR：
`ghcr.io/hieedaniel/your-pm:latest`

```bash
git clone https://github.com/hieedaniel/your-pm.git
cd your-pm
cp config.example.json config.json     # 填 asr.api_key 等
chmod 644 config.json                  # 容器内以 uid 10001 运行

# 私有包需先登录（GitHub PAT，勾 read:packages）
echo "<PAT>" | docker login ghcr.io -u hieedaniel --password-stdin

docker compose -f docker-compose.server.yml up -d
curl -s localhost:19535/api/state | head -c 200
```

浏览器访问 `https://<你的域名>/` —— **必须是 HTTPS 或 localhost**，
否则浏览器不给麦克风权限（`getUserMedia` 的硬性要求）。
Nginx 反代样例见 `nginx-qieshen-live.conf`，**SSE 必须 `proxy_buffering off`**。

两个前端：

| 地址 | 给谁用 | 内容 |
|---|---|---|
| `/` | 电脑（手机 UA 会自动切到 `/m`） | 三栏调试台：会话控制、转写、主题与卡片、自检、日志 |
| `/m` | 手机 / APK | 只有主题条 + 提示卡 + 录音键，服务状态收进顶部「状态」抽屉 |
| `/?desk=1` | 手机上临时看桌面版 | 强制桌面界面 |

想在本机边改边跑（不用 Docker）：

```bash
python server.py          # 读 config.json，默认 127.0.0.1:8780
```

## 配置

`config.json` 里有真实凭据，**永不入库**（见 `.gitignore`），只提交脱敏模板 `config.example.json`。

| 段 | 作用 | 关键项 |
|---|---|---|
| `server` | 监听地址 | `host` / `port`（容器里被 `QS_HOST`/`QS_PORT` 覆盖） |
| `asr` | 豆包流式 ASR | `api_key`、`api_version`、`resource_id`、`hotwords` |
| `wma` | 主题提取 | `provider`（`auto`/`acp`/`rule`）、`acp.enterprise_id`、`acp.user_key` |
| `mcp` | 知识库 | `transport`、`adapter`、`docs_dir` |

`wma.provider=auto`：启动时先探 WMA 控制面，**只有控制面 200 且能成功建会话**才切到云端，
否则静默沿用本地规则 —— 凭据没通时演示不会挂。

`wma.prompt_mode`：`agent` = 提示词放在 Agent 侧（每轮只发一句话，省约 3500 token 预填充）；
`client` = 每轮连提示词一起发。

## 知识库（导入文件 / 接 MCP）

访问 **`/kb`** 进管理页（口令见 `docker logs qieshen-live | grep 管理页`）：

- **导入文件**：md / txt / docx / pdf / html，拖进去即可，索引立即重建
- **粘贴文本**：会议纪要、话术口径直接变成可检索文档
- **抓取网页**：去掉导航页脚脚本，只留正文
- **MCP 知识源**：填 URL + Token，多源按权重合并，保存即热生效（不重启）

上传的文档与配置都在 `data/`（compose 已挂载），`docs-kaiyi/` 内置 19 篇只读。
PDF 解析需要 `pypdf`（`requirements.txt` 唯一依赖），其余全部标准库。

## 手机端 APK

`android/` 是一个几百 KB 的 WebView 壳：只自己实现麦克风采集（`AudioRecord`，16kHz PCM），
界面、字幕、卡片全部复用网页，改一处两端生效。push 改动即由 Actions 构建，
在 Actions 页面的 Artifacts 里下载 `app-debug.apk`。详见 [`android/README.md`](android/README.md)。

## 目录

```
server.py            HTTP + WS 入口、路由、静态页
wsproto.py           手写 WebSocket / SSE（含 chunked 解码）
doubao_asr.py        豆包流式 ASR 客户端与鉴权自检
orchestrator.py      逐句链路编排：ASR → 主题 → 检索 → 卡片
wma_agent.py         WMA 主题提取（ACP 客户端 / 本地规则）
mcp_docs.py          知识库检索（MCP + 主题加权），多目录合并索引
mcp_client.py        MCP 传输层（stdio / http）
kb_admin.py          知识库导入（md/txt/docx/pdf/url）与 MCP 源配置
index.html           桌面版单页（录音、字幕、卡片、自检）
m.html               手机版单页（主题 + 提示卡 + 状态抽屉）
kb.html              知识库管理页
prompts/             topic_extract.md 提示词 + taxonomy.json 封闭主题表
docs-kaiyi/          19 篇知识库正文（只读）
data/                运行期数据：上传文档 / MCP 源 / 管理口令（.gitignore，卷挂载）
```

## 部署与运维

完整说明见 [`DEPLOY-Docker.md`](DEPLOY-Docker.md)：四条部署路线、端口冲突处理、
HTTPS 反代、日常运维、常见故障对照表。

主题表如何维护见 [`WMA-提示词配置.md`](WMA-提示词配置.md)。

## 安全

- 镜像里**不含任何凭据**，`config.json` 靠卷挂载；`.dockerignore` 也会把它挡在构建上下文外
- 容器以非 root（uid 10001）运行
- 默认只绑 `127.0.0.1`，对外一律走 Nginx + HTTPS：本服务没有登录态，
  谁连上谁就能消耗你的 ASR 额度

# RepoAssistant - GitHub 仓库学习助手

输入 GitHub 仓库链接，系统帮助用户快速理解项目目标、架构、核心模块，并给出学习路径和贡献建议。

三种展示形式：

- **代码地图**：展示仓库目录结构和关键文件
- **学习路径规划器**：给出新手阅读和上手顺序
- **Agent 问答**：基于仓库上下文回答问题

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | Vue 3 + Vite + TypeScript + 原生 CSS + 原生 fetch |
| 后端 | Python 3.11 + FastAPI + Pydantic + httpx |
| LLM  | DeepSeek（通过 OpenAI 兼容接口调用，可换成任意兼容模型） |

## 环境准备

### 后端

```bash
conda create -n repoassistant python=3.11 -y
conda activate repoassistant
cd backend
pip install -r requirements.txt
```

### 前端

```bash
cd frontend
npm install
```

## 配置

### 密钥（不明文写进仓库）

真实 API Key **不放在任何会被提交的配置文件里**，读取优先级：

1. **系统环境变量** `DEEPSEEK_API_KEY`（优先级最高）
2. **本机密钥文件** `backend/data/secrets.json`（已被 `.gitignore` 忽略，建议只给当前用户读写）

首次启动后端会自动生成不含密钥的模板：

```json
{
  "_readme": ["..."],
  "DEEPSEEK_API_KEY": ""
}
```

把 Key 填进去保存、重启后端即可。也可以设置环境变量 `REPOASSISTANT_SECRETS_FILE` 把密钥文件放到任意位置。

### 可选配置（`backend/.env`）

`.env` 里只放可以公开的内容：

```env
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

`DEEPSEEK_MODEL` 必须填服务端真实支持的模型名，目前可用的是 `deepseek-flash` 和 `deepseek-v4-pro`；填错会返回 400 `The supported API model names are ...`，此时前端会显示橙色失败原因并自动回退到规则分析。

其它可选环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANALYSIS_CACHE_TTL` | `86400` | 分析结果缓存有效期（秒），`0` 表示永不过期 |
| `ALLOWED_ORIGINS` | `localhost` / `127.0.0.1` 的 5173、4173 | 允许跨域访问后端的前端来源 |
| `REPOASSISTANT_SECRETS_FILE` | `backend/data/secrets.json` | 密钥文件路径 |
| `REPOASSISTANT_DATA_DIR` | `backend/data` | 运行时数据根目录（仓库缓存与分析结果） |

`DEEPSEEK_BASE_URL` 填 `https://api.deepseek.com` 或 `https://api.deepseek.com/v1` 都可以，后端会自动补全 `/v1/chat/completions`，不会再出现 `/v1/v1`。

### Demo 模式 vs LLM 模式

- **未配置密钥**：自动进入 Demo 模式。分析结果和问答基于已提取的 README、目录树和配置文件规则生成，不调用 LLM。前端显示黄色提示，并说明密钥该填在哪里。
- **已配置密钥**：优先调用 LLM。**LLM 调用失败（网络、401、超时、返回格式异常）时，前端会用橙色提示给出具体失败原因**，同时自动回退到规则结果，保证系统不崩溃。

## 启动

### Docker 一键运行

```bash
docker compose up --build
```

启动后访问：

- 前端：http://localhost:5173
- 后端：http://localhost:8000

容器与宿主机共享 `backend/data` 目录（密钥文件、克隆的仓库、分析结果都在这里），重启容器不会重新克隆仓库。

Docker 下配置密钥：

```bash
# 方式一：在宿主机设置环境变量，compose 直接读取
$env:DEEPSEEK_API_KEY="sk-xxx"     # Windows PowerShell（cmd 用 set DEEPSEEK_API_KEY=sk-xxx）
export DEEPSEEK_API_KEY=sk-xxx     # macOS / Linux
docker compose up --build

# 方式二：把不含密钥的 backend/.env 显式传给 compose
docker compose --env-file backend/.env up --build
```

用第二种方式时，密钥依然从 `backend/data/secrets.json` 读取（该文件已挂载进容器）。

### 启动后端

```bash
conda activate repoassistant
cd backend
uvicorn app.main:app --reload --port 8000
```

### 启动前端

```bash
cd frontend
npm run dev
```

前端默认请求同源 `/api`，由 Vite 代理转发到 `http://localhost:8000`，浏览器侧不再有跨域问题。前后端不在同一台机器时，在 `frontend/.env` 里设置 `VITE_API_BASE=http://后端地址:8000`。

## 使用流程

1. 启动前后端服务
2. 打开 http://localhost:5173 ，确认页面顶部显示「后端已连接」
3. 在输入框中填入 GitHub 仓库 URL（如 `https://github.com/pallets/click`）
4. 点击 **Analyze**，等待分析完成
5. 查看项目概览、目录结构、代码地图、关键模块、学习路径和贡献建议
6. 在侧边栏「仓库问答」区域输入问题（如"这个项目是做什么的？""架构是怎样的？"）

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 返回是否 Demo 模式、当前模型名、密钥来源（不含密钥内容） |
| POST | `/api/analyze` | 请求体 `{"repo_url": "...", "force_refresh": false}`，`force_refresh=true` 忽略缓存强制重新分析 |
| GET | `/api/analysis/{id}` | 读取已保存的分析结果 |
| POST | `/api/chat` | 请求体 `{"analysis_id": "...", "question": "..."}` |

## 测试

两套测试都是可直接运行的脚本，成功时退出码为 0。

**1）逻辑测试（59 项断言）**：不需要安装任何第三方依赖，脚本内部会替换掉 fastapi / pydantic / httpx。

```bash
cd backend
python tests/test_logic.py
```

覆盖点：密钥来源优先级与单行清洗、GitHub 地址校验与缓存键归一、目录树层级解析、源码抽样路径、monorepo 配置识别、目录树截断顺序、README 链接清洗、LLM 端点拼接、LLM 失败原因上报、缓存 TTL 与强制刷新、问答回退、analysis_id 路径穿越防护、入参校验、健康检查不泄露密钥。

**2）集成测试（33 项断言）**：用真实依赖跑完整链路（真实 HTTP 接口、真实规则/LLM 分析、真实落盘），需要在装好依赖的环境里运行。

```bash
cd backend
python tests/test_e2e.py
```

它会自己造一个多层级示例仓库，按 `clone_repo` 的目录约定预置好，然后验证目录树/配置/源码抽取、分析结果结构、缓存与 `force_refresh`、问答、健康检查与失败提示。运行目录默认取系统临时目录（可用 `REPOASSISTANT_DATA_DIR` 指定），不会污染 `backend/data/`。

## 已知边界

- 密钥文件必须是合法 JSON，且 `DEEPSEEK_API_KEY` 是单行字符串。若文件损坏，后端会打印警告并尽量从中恢复密钥；实在取不到时退回 Demo 模式，前端会说明密钥该填在哪里。
- `DEEPSEEK_MODEL` 需填服务端真实支持的模型名（当前为 `deepseek-flash` 与 `deepseek-v4-pro`），否则调用失败并回退到规则分析（前端会显示具体原因）。
- `.env` 里带 `#` 的行是注释，不会生效；想改缓存时间等可选配置时，要把该行开头的 `#` 去掉。
- LLM 返回的 JSON 若被截断（常见于输出长度触顶），后端会**自动补全已完整解析到的字段**并继续使用；补不回来时才回退规则分析并显示原因，两种情况都不会让接口报错。
- 分析请求的输出上限为 8192 tokens（`llm_service._MAX_OUTPUT_TOKENS`）；服务端若拒绝这个上限，会按失败处理并回退规则分析。
- 克隆下来的仓库会一直保留在 `backend/data/repos/`，便于复用缓存；需要清理时直接删除该目录即可。

## 目录结构

```
backend/
  app/
    main.py              # FastAPI 入口与路由
    config.py            # 配置与密钥加载（环境变量 > secrets.json）
    models.py            # 请求/响应模型，LLM 输出也用它校验
    urls.py              # GitHub 地址规则（校验层与抓取层共用）
    services/
      repo_service.py      # 克隆、目录树、配置与源码抽取
      analysis_service.py  # 分析编排、规则分析、问答兜底
      llm_service.py       # LLM 调用与失败原因上报
      storage_service.py   # 分析结果落盘（含 id 合法性校验）
  tests/
    test_logic.py        # 无依赖逻辑测试（59 项）
    test_e2e.py          # 真实栈集成测试（33 项）
  data/                  # 运行时数据：secrets.json、repos/、analyses/（全部被 git 忽略）
frontend/
  src/App.vue            # 单文件页面：分析结果、侧边栏、问答弹窗
  src/style.css          # GitHub 风格样式
```

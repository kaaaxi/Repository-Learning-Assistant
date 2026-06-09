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
| LLM  | DeepSeek V4 Pro（通过 OpenAI 兼容接口调用） |

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

复制 `backend/.env.example` 为 `backend/.env`：

```bash
cd backend
cp .env.example .env
```

`.env` 内容：

```env
DEEPSEEK_API_KEY=your_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
```

### Demo 模式 vs DeepSeek 模式

- **未配置 `DEEPSEEK_API_KEY`（或为空）**：自动进入 Demo 模式。分析结果和问答基于仓库已提取的 README、目录树和配置文件，通过规则生成，不调用 LLM。前端会显示黄色提示。
- **已配置有效 `DEEPSEEK_API_KEY`**：优先调用 DeepSeek V4 Pro 生成分析结果和回答。如果 LLM 调用失败，自动 fallback 到 Demo 模式，保证系统不崩溃。

## 启动

### Docker 一键运行

默认不需要配置 API Key，系统会以 Demo 模式运行：

```bash
docker compose up --build
```

启动后访问：

- 前端：http://localhost:5173
- 后端：http://localhost:8000

如果要在 Docker 中使用 DeepSeek，请不要提交真实 `.env`，只在本机保留 `backend/.env`，然后通过 `--env-file` 显式传入：

```bash
docker compose --env-file backend/.env up --build
```

停止服务：

```bash
docker compose down
```

清理 Docker 运行时数据卷（会删除容器内克隆的仓库和分析缓存）：

```bash
docker compose down -v
```

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

- 前端：http://localhost:5173
- 后端：http://localhost:8000

## 使用流程

1. 启动前后端服务
2. 打开 http://localhost:5173，确认页面顶部显示「后端已连接」
3. 在输入框中填入 GitHub 仓库 URL（如 `https://github.com/pallets/click`）
4. 点击 **Analyze**，等待分析完成
5. 查看项目概览、目录结构、代码地图、关键模块、学习路径和贡献建议
6. 在底部「仓库问答」区域输入问题（如"这个项目是做什么的？""架构是怎样的？"）

## API

### GET /api/health

测试后端是否运行。

```bash
curl http://localhost:8000/api/health
# {"status":"ok","demo_mode":true}
```

### POST /api/analyze

分析 GitHub 仓库。

```bash
curl -X POST http://localhost:8000/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/pallets/click"}'
```

### GET /api/analysis/{analysis_id}

读取已保存的分析结果。

```bash
curl http://localhost:8000/api/analysis/<analysis_id>
```

### POST /api/chat

基于已分析仓库回答问题。

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"analysis_id": "<analysis_id>", "question": "这个项目是做什么的？"}'
```

## 项目结构

```
RepoAssistant/
├── frontend/                # Vue 3 前端
│   ├── Dockerfile           # 前端 Docker 构建
│   ├── nginx.conf           # 前端静态服务配置
│   ├── src/
│   │   ├── App.vue          # 主页面
│   │   ├── main.ts          # 入口
│   │   └── style.css        # 样式
│   └── package.json
├── backend/
│   ├── Dockerfile           # 后端 Docker 构建
│   ├── app/
│   │   ├── main.py          # FastAPI 路由
│   │   ├── config.py        # 配置读取
│   │   ├── models.py        # Pydantic 模型
│   │   └── services/
│   │       ├── repo_service.py       # 仓库克隆与文件提取
│   │       ├── analysis_service.py   # 分析编排与 fallback
│   │       ├── llm_service.py        # DeepSeek 调用封装
│   │       └── storage_service.py    # JSON 存取
│   ├── data/                # 运行时数据（不提交）
│   │   ├── repos/
│   │   └── analyses/
│   ├── requirements.txt
│   └── .env.example
├── CLAUDE.md
├── docker-compose.yml
├── README.md
└── .gitignore
```

# RepoAssistant - GitHub 仓库学习助手

输入 GitHub 仓库链接，快速理解项目目标、架构、核心模块，并获取学习路径和贡献建议。

## 技术栈

- **前端**：Vue 3 + Vite + TypeScript
- **后端**：Python 3.11 + FastAPI
- **LLM**：DeepSeek V4 Pro

## 环境准备

### 后端

```bash
conda create -n repoassistant python=3.11 -y
conda activate repoassistant
cd backend
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env` 并填入 API Key（不配置则以 Demo 模式运行）：

```bash
cp .env.example .env
```

### 前端

```bash
cd frontend
npm install
```

## 启动

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

- 前端地址：http://localhost:5173
- 后端地址：http://localhost:8000
- Health 接口：http://localhost:8000/api/health

## 验证

1. 启动后端后，访问 http://localhost:8000/api/health 应返回 `{"status":"ok","demo_mode":true}`
2. 启动前端后，访问 http://localhost:5173 应看到页面顶部显示「后端已连接」

<script setup lang="ts">
import { ref, onMounted } from 'vue'

const API_BASE = 'http://localhost:8000'

const backendStatus = ref<'loading' | 'ok' | 'error'>('loading')
const demoMode = ref(false)
const repoUrl = ref('')

async function checkHealth() {
  try {
    const res = await fetch(`${API_BASE}/api/health`)
    if (!res.ok) throw new Error()
    const data = await res.json()
    backendStatus.value = 'ok'
    demoMode.value = data.demo_mode === true
  } catch {
    backendStatus.value = 'error'
  }
}

onMounted(checkHealth)
</script>

<template>
  <div class="header">
    <h1>RepoAssistant - GitHub 仓库学习助手</h1>
    <div class="status-bar">
      <span class="status-dot" :class="backendStatus"></span>
      <template v-if="backendStatus === 'loading'">正在连接后端...</template>
      <template v-else-if="backendStatus === 'ok'">后端已连接</template>
      <template v-else>后端未连接，请启动后端服务</template>
    </div>
  </div>

  <div class="main">
    <div class="input-section">
      <h2>分析 GitHub 仓库</h2>
      <p>输入 GitHub 仓库链接，系统将帮助你快速理解项目架构、核心模块和学习路径。</p>
      <div class="input-row">
        <input
          v-model="repoUrl"
          type="text"
          placeholder="https://github.com/owner/repo"
          :disabled="backendStatus !== 'ok'"
        />
        <button :disabled="backendStatus !== 'ok' || !repoUrl.trim()">Analyze</button>
      </div>
      <div v-if="demoMode" class="demo-notice">
        检测到未配置 DEEPSEEK_API_KEY，当前使用 Demo 模式。请在 backend/.env 中配置 API Key 后使用真实 LLM 分析。
      </div>
    </div>
  </div>
</template>

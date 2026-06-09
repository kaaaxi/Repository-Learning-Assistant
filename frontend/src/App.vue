<script setup lang="ts">
import { ref, onMounted } from 'vue'

const API_BASE = 'http://localhost:8000'

interface CodeMapItem { name: string; description: string }
interface KeyModule { name: string; description: string; key_files: string[] }
interface LearningPathStep { step: number; title: string; description: string }
interface ContributionSuggestion { title: string; description: string; difficulty: string }
interface AnalysisResult {
  analysis_id: string
  project_name: string
  repo_url: string
  overview: string
  directory_tree: string
  architecture_summary: string
  code_map: CodeMapItem[]
  key_modules: KeyModule[]
  learning_path: LearningPathStep[]
  contribution_suggestions: ContributionSuggestion[]
  developer_workflow: string
  coding_conventions: string
}

const backendStatus = ref<'loading' | 'ok' | 'error'>('loading')
const demoMode = ref(false)
const repoUrl = ref('')
const loading = ref(false)
const errorMsg = ref('')
const result = ref<AnalysisResult | null>(null)

const qaQuestion = ref('')
const qaLoading = ref(false)
const qaError = ref('')
const qaHistory = ref<{ question: string; answer: string }[]>([])
const qaOpen = ref(false)

interface WorkflowStep {
  html: string
}

function formatDeveloperWorkflow(text: string): WorkflowStep[] {
  if (!text) return []

  const normalized = text.replace(/\r\n/g, '\n').trim()
  const numberedParts = normalized
    .split(/(?:^|\n|(?<=[。；;.!?？])\s*)(?=\d+[.)、]\s*)/g)
    .map((part) => part.trim())
    .filter(Boolean)

  const sourceParts = numberedParts.length > 1
    ? numberedParts
    : normalized.split(/\n{2,}|(?<=。)\s+/).map((part) => part.trim()).filter(Boolean)

  return sourceParts
    .map((part) => part.replace(/^\d+[.)、]\s*/, '').trim())
    .filter(Boolean)
    .map((part) => ({ html: renderInlineMarkdown(part) }))
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function renderMarkdown(text: string): string {
  const escaped = escapeHtml(text)

  const codeBlocks: string[] = []
  let processed = escaped.replace(/```([\s\S]*?)```/g, (_match, code: string) => {
    const idx = codeBlocks.length
    codeBlocks.push(`<pre class="md-code-block"><code>${code.trim()}</code></pre>`)
    return `%%CODEBLOCK_${idx}%%`
  })

  processed = processed.replace(/`([^`\n]+)`/g, '<code class="md-inline-code">$1</code>')
  processed = processed.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')

  const lines = processed.split('\n')
  const out: string[] = []
  let inUl = false

  for (const line of lines) {
    if (line.startsWith('%%CODEBLOCK_')) {
      if (inUl) { out.push('</ul>'); inUl = false }
      const idx = parseInt(line.match(/%%CODEBLOCK_(\d+)%%/)?.[1] || '0')
      out.push(codeBlocks[idx])
      continue
    }
    const ulMatch = line.match(/^[-*]\s+(.+)/)
    if (ulMatch) {
      if (!inUl) { out.push('<ul class="md-list">'); inUl = true }
      out.push(`<li>${ulMatch[1]}</li>`)
    } else {
      if (inUl) { out.push('</ul>'); inUl = false }
      out.push(line === '' ? '' : `<p>${line}</p>`)
    }
  }
  if (inUl) out.push('</ul>')

  return out.join('\n')
}

function renderInlineMarkdown(text: string): string {
  return escapeHtml(text)
    .replace(/`([^`\n]+)`/g, '<code class="workflow-inline-code">$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
}

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

async function analyze() {
  errorMsg.value = ''
  result.value = null
  loading.value = true
  try {
    const res = await fetch(`${API_BASE}/api/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_url: repoUrl.value.trim() }),
    })
    const data = await res.json()
    if (!res.ok) {
      errorMsg.value = data.detail || 'Analysis failed'
      return
    }
    result.value = data as AnalysisResult
  } catch (e: any) {
    errorMsg.value = e.message || 'Network error'
  } finally {
    loading.value = false
  }
}

async function askQuestion() {
  if (!result.value) return
  const q = qaQuestion.value.trim()
  if (!q) return
  qaError.value = ''
  qaLoading.value = true
  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ analysis_id: result.value.analysis_id, question: q }),
    })
    const data = await res.json()
    if (!res.ok) {
      qaError.value = data.detail || 'Chat failed'
      return
    }
    qaHistory.value.push({ question: q, answer: data.answer })
    qaQuestion.value = ''
  } catch (e: any) {
    qaError.value = e.message || 'Network error'
  } finally {
    qaLoading.value = false
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
          :disabled="loading || backendStatus !== 'ok'"
          @keyup.enter="analyze"
        />
        <button :disabled="loading || backendStatus !== 'ok' || !repoUrl.trim()" @click="analyze">
          {{ loading ? '分析中...' : 'Analyze' }}
        </button>
      </div>
      <div v-if="demoMode" class="demo-notice">
        检测到未配置 DEEPSEEK_API_KEY，当前使用 Demo 模式。请在 backend/.env 中配置 API Key 后使用真实 LLM 分析。
      </div>
      <div v-if="errorMsg" class="error-notice">{{ errorMsg }}</div>
    </div>

    <div v-if="result" class="result-section">
      <h2>{{ result.project_name }}</h2>

      <div class="card">
        <h3>项目概览</h3>
        <p>{{ result.overview }}</p>
      </div>

      <div class="card">
        <h3>目录</h3>
        <pre>{{ result.directory_tree }}</pre>
      </div>

      <div class="card">
        <h3>架构摘要</h3>
        <p>{{ result.architecture_summary }}</p>
      </div>

      <div class="card">
        <h3>代码地图</h3>
        <ul class="list">
          <li v-for="item in result.code_map" :key="item.name">
            <strong>{{ item.name }}</strong> — {{ item.description }}
          </li>
        </ul>
      </div>

      <div class="card">
        <h3>关键模块</h3>
        <ul class="list">
          <li v-for="m in result.key_modules" :key="m.name">
            <strong>{{ m.name }}</strong> — {{ m.description }}
          </li>
        </ul>
      </div>

      <div class="card">
        <h3>学习路径</h3>
        <ol class="list steps">
          <li v-for="s in result.learning_path" :key="s.step">
            <strong>{{ s.title }}</strong>
            <p>{{ s.description }}</p>
          </li>
        </ol>
      </div>

      <div class="card">
        <h3>入门贡献建议</h3>
        <ul class="list">
          <li v-for="c in result.contribution_suggestions" :key="c.title">
            <strong>{{ c.title }}</strong>
            <span class="badge">{{ c.difficulty }}</span>
            <p>{{ c.description }}</p>
          </li>
        </ul>
      </div>

      <div class="card">
        <h3>开发流程</h3>
        <div class="workflow-list">
          <div v-for="(step, idx) in formatDeveloperWorkflow(result.developer_workflow)" :key="idx" class="workflow-step">
            <div class="workflow-step-title">
              <span class="workflow-index">{{ idx + 1 }}</span>
              <span v-html="step.html"></span>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>代码规范</h3>
        <p>{{ result.coding_conventions }}</p>
      </div>
    </div>
  </div>

  <!-- FAB -->
  <button v-if="result" class="qa-fab" @click="qaOpen = true">⌕ 问仓库</button>

  <!-- Modal -->
  <template v-if="qaOpen">
    <div class="qa-overlay" @click="qaOpen = false"></div>
    <div class="qa-modal">
      <div class="qa-modal-header">
        <div>
          <div class="qa-modal-title">仓库问答</div>
          <div class="qa-modal-project">{{ result!.project_name }}</div>
        </div>
        <button class="qa-modal-close" @click="qaOpen = false">&times;</button>
      </div>
      <div class="qa-modal-body">
        <div v-if="qaHistory.length" class="qa-modal-history">
          <div v-for="(item, i) in qaHistory" :key="i" class="qa-item">
            <div class="qa-q">{{ item.question }}</div>
            <div class="qa-a" v-html="renderMarkdown(item.answer)"></div>
          </div>
        </div>
        <div v-else class="qa-modal-empty">输入问题，基于已分析的项目信息回答。例如：项目是做什么的？架构是怎样的？</div>
      </div>
      <div class="qa-modal-footer">
        <div v-if="qaError" class="qa-modal-error">{{ qaError }}</div>
        <div class="qa-modal-input">
          <input
            v-model="qaQuestion"
            type="text"
            placeholder="输入你的问题..."
            :disabled="qaLoading"
            @keyup.enter="askQuestion"
          />
          <button :disabled="qaLoading || !qaQuestion.trim()" @click="askQuestion">
            {{ qaLoading ? '回答中...' : '提问' }}
          </button>
        </div>
      </div>
    </div>
  </template>
</template>

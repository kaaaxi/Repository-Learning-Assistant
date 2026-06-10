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

const qaSuggestions = [
  '这个项目是做什么的？',
  '我应该从哪里开始读？',
  '有哪些适合新手的贡献？',
]

function useSuggestion(q: string) {
  qaQuestion.value = q
  askQuestion()
}

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

function shortOverview(text: string): string {
  if (!text) return ''
  const limit = 140
  if (text.length <= limit) return text
  return text.slice(0, limit) + '…'
}

function sidebarReadFiles(result: AnalysisResult): string[] {
  const files: string[] = []
  for (const m of result.key_modules) {
    for (const f of m.key_files) {
      if (!files.includes(f)) files.push(f)
    }
    if (files.length >= 4) break
  }
  if (files.length === 0) {
    return result.key_modules.slice(0, 4).map((m) => m.name)
  }
  return files.slice(0, 4)
}

function extractWorkflowCommands(text: string): string[] {
  if (!text) return []
  const matches = text.match(/`([^`\n]+)`/g)
  if (!matches) return []
  return matches.map((m) => m.replace(/`/g, '')).slice(0, 4)
}

function sidebarTasks(result: AnalysisResult): { title: string; difficulty: string }[] {
  return result.contribution_suggestions.slice(0, 2).map((c) => ({
    title: c.title,
    difficulty: c.difficulty,
  }))
}

function scrollTo(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
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
  <header class="site-header">
    <h1>RepoAssistant</h1>
    <div class="status-bar">
      <span class="status-dot" :class="backendStatus"></span>
      <template v-if="backendStatus === 'loading'">connecting...</template>
      <template v-else-if="backendStatus === 'ok'">backend ok</template>
      <template v-else>backend offline</template>
    </div>
  </header>

  <div class="layout">
    <div class="layout-main">
      <!-- Toolbar -->
      <div class="toolbar">
        <div class="toolbar-label">Analyze repository</div>
        <div class="toolbar-row">
          <input
            v-model="repoUrl"
            type="text"
            placeholder="https://github.com/owner/repo"
            :disabled="loading || backendStatus !== 'ok'"
            @keyup.enter="analyze"
          />
          <button class="btn-primary" :disabled="loading || backendStatus !== 'ok' || !repoUrl.trim()" @click="analyze">
            {{ loading ? 'Analyzing...' : 'Analyze' }}
          </button>
        </div>
        <div v-if="demoMode" class="demo-notice">
          Demo 模式：未配置 DEEPSEEK_API_KEY，分析结果基于规则生成。配置后可使用 LLM 分析。
        </div>
        <div v-if="errorMsg" class="error-notice">{{ errorMsg }}</div>
      </div>

      <!-- Results -->
      <div v-if="result" class="result-section">
        <div class="repo-header">
          <h2>{{ result.project_name }}</h2>
          <div class="repo-header-url">{{ result.repo_url }}</div>
        </div>

        <div id="sec-overview" class="section">
          <h3>概览</h3>
          <p>{{ result.overview }}</p>
        </div>

        <div id="sec-directory" class="section">
          <h3>目录</h3>
          <div class="dir-tree-wrap">
            <pre>{{ result.directory_tree }}</pre>
          </div>
        </div>

        <div id="sec-architecture" class="section">
          <h3>架构摘要</h3>
          <p>{{ result.architecture_summary }}</p>
        </div>

        <div id="sec-codemap" class="section">
          <h3>代码地图</h3>
          <ul class="gh-list">
            <li v-for="item in result.code_map" :key="item.name">
              <div class="gh-list-title"><code>{{ item.name }}/</code></div>
              <div class="gh-list-desc">{{ item.description }}</div>
            </li>
          </ul>
        </div>

        <div id="sec-modules" class="section">
          <h3>关键模块</h3>
          <ul class="gh-list">
            <li v-for="m in result.key_modules" :key="m.name">
              <div class="gh-list-title">{{ m.name }}</div>
              <div class="gh-list-desc">{{ m.description }}</div>
              <div v-if="m.key_files.length" class="gh-list-tags">
                <code v-for="f in m.key_files" :key="f" class="file-tag">{{ f }}</code>
              </div>
            </li>
          </ul>
        </div>

        <div id="sec-learning" class="section">
          <h3>学习路径</h3>
          <ol class="steps">
            <li v-for="s in result.learning_path" :key="s.step">
              <div class="step-title">{{ s.title }}</div>
              <p>{{ s.description }}</p>
            </li>
          </ol>
        </div>

        <div id="sec-contrib" class="section">
          <h3>贡献建议</h3>
          <ul class="gh-list">
            <li v-for="c in result.contribution_suggestions" :key="c.title">
              <div class="gh-list-row">
                <span class="gh-list-title">{{ c.title }}</span>
                <span class="badge" :class="c.difficulty">{{ c.difficulty }}</span>
              </div>
              <div class="gh-list-desc">{{ c.description }}</div>
            </li>
          </ul>
        </div>

        <div id="sec-workflow" class="section">
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

        <div id="sec-conventions" class="section">
          <h3>代码规范</h3>
          <p>{{ result.coding_conventions }}</p>
        </div>
      </div>
    </div>

    <!-- Sidebar -->
    <div v-if="result" class="layout-sidebar">
      <div class="sidebar">
        <div class="sidebar-title">{{ result.project_name }}</div>
        <div class="sidebar-url">{{ result.repo_url }}</div>
        <span v-if="demoMode" class="sidebar-badge demo">Demo</span>
        <span v-else class="sidebar-badge deepseek">DeepSeek</span>

        <!-- About -->
        <div v-if="result.overview" class="sidebar-block">
          <div class="sidebar-block-title">About</div>
          <p class="sidebar-about-text">{{ shortOverview(result.overview) }}</p>
        </div>

        <!-- 仓库问答 -->
        <div class="sidebar-block">
          <div class="sidebar-block-title">仓库问答</div>
          <div class="sidebar-block-desc">基于当前分析结果提问</div>
          <button class="btn-primary sidebar-qa-btn" @click="qaOpen = true">问仓库</button>
        </div>

        <!-- 推荐先读 -->
        <div v-if="sidebarReadFiles(result).length" class="sidebar-block">
          <div class="sidebar-block-title">推荐先读</div>
          <ul class="sidebar-file-list">
            <li v-for="f in sidebarReadFiles(result)" :key="f"><code>{{ f }}</code></li>
          </ul>
        </div>

        <!-- 常用命令 -->
        <div v-if="extractWorkflowCommands(result.developer_workflow).length" class="sidebar-block">
          <div class="sidebar-block-title">常用命令</div>
          <ul class="sidebar-cmd-list">
            <li v-for="cmd in extractWorkflowCommands(result.developer_workflow)" :key="cmd"><code>{{ cmd }}</code></li>
          </ul>
        </div>

        <!-- 入门任务 -->
        <div v-if="sidebarTasks(result).length" class="sidebar-block">
          <div class="sidebar-block-title">入门任务</div>
          <ul class="sidebar-task-list">
            <li v-for="t in sidebarTasks(result)" :key="t.title">
              <span class="sidebar-task-title">{{ t.title }}</span>
              <span class="badge" :class="t.difficulty">{{ t.difficulty }}</span>
            </li>
          </ul>
        </div>

        <!-- 页面目录 -->
        <div class="sidebar-block">
          <div class="sidebar-block-title">页面目录</div>
          <ul class="sidebar-nav">
            <li><a href="#sec-overview" @click.prevent="scrollTo('sec-overview')">概览</a></li>
            <li><a href="#sec-directory" @click.prevent="scrollTo('sec-directory')">目录</a></li>
            <li><a href="#sec-architecture" @click.prevent="scrollTo('sec-architecture')">架构</a></li>
            <li><a href="#sec-codemap" @click.prevent="scrollTo('sec-codemap')">代码地图</a></li>
            <li><a href="#sec-modules" @click.prevent="scrollTo('sec-modules')">关键模块</a></li>
            <li><a href="#sec-learning" @click.prevent="scrollTo('sec-learning')">学习路径</a></li>
            <li><a href="#sec-contrib" @click.prevent="scrollTo('sec-contrib')">贡献建议</a></li>
            <li><a href="#sec-workflow" @click.prevent="scrollTo('sec-workflow')">开发流程</a></li>
          </ul>
        </div>
      </div>
    </div>
  </div>

  <!-- QA Modal -->
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
        <div v-else class="qa-modal-empty">
          <div class="qa-suggestions-label">试试以下问题：</div>
          <div class="qa-suggestions">
            <button
              v-for="s in qaSuggestions"
              :key="s"
              class="qa-suggestion"
              :disabled="qaLoading"
              @click="useSuggestion(s)"
            >{{ s }}</button>
          </div>
        </div>
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
          <button class="btn-primary" :disabled="qaLoading || !qaQuestion.trim()" @click="askQuestion">
            {{ qaLoading ? '回答中...' : '提问' }}
          </button>
        </div>
      </div>
    </div>
  </template>
</template>

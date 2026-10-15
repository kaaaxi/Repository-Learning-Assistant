# Stage 1 产出：最小可用的 Agent 循环

> 承接 [AGENT-STAGE0.md](AGENT-STAGE0.md) 的边界判断，本文记录 Stage 1 **实际做出来的东西**、怎么验证、以及刻意没做的部分。
> 所有数字均来自本机实测（`repoassistant` 环境，Python 3.11）。

## 1. Stage 1 的六项能力，逐条对应实现

| Stage 1 要求 | 本项目的落地 | 代码位置 |
|---|---|---|
| 会用 LLM API 完成普通对话 | 复用已有的 OpenAI 兼容调用（`/v1/chat/completions`） | `llm_service.request_chat_completion()` |
| 会让模型输出结构化 JSON | 概览阶段已有 `AnalysisResult` 校验；工具参数用 JSON 解析 | `analysis_service._normalize_llm_result()`、`agent_service._extract_tool_calls()` |
| 会定义工具函数 | 三个只读工具 + OpenAI JSON Schema | `repo_tools.list_directory / search_code / read_file`、`repo_tools.TOOL_SCHEMAS` |
| 会解析 tool call / function call | 兼容 `arguments` 为 JSON 字符串或对象两种形态；解析失败不抛异常 | `agent_service._extract_tool_calls()` |
| 会执行工具并把结果喂回模型 | 每个工具结果作为 `role: "tool"` + `tool_call_id` 追加进对话 | `agent_service.run_agent_chat()` |
| 会给 loop 加最大步数、超时、错误处理 | 四项上限 + 四类错误处理，见下表 | `agent_service`、`config.AGENT_*` |

## 2. Agent 循环的控制流

```
用户问题
  │
  ├─ 构造 messages：[system(工具说明+仓库信息+上限), user(问题)]
  │
  └─▶ 循环
        ├─ 检查耗时是否超过 AGENT_TIMEOUT_SECONDS ──超时──▶ time_limit 收尾
        ├─ 调用模型（带 tools）
        ├─ 没有 tool_calls ──────────────────────────▶ final_answer（结束）
        ├─ 解析 tool_calls（参数非法 ⇒ 按空参数处理并记日志）
        ├─ 检查步数是否超过 AGENT_MAX_STEPS ──超限──▶ step_limit 收尾
        ├─ 与上一批调用完全相同 ──────────────重复──▶ no_new_info 收尾
        ├─ 逐个执行工具
        │    ├─ 成功 → 结果文本（受上下文预算裁剪）→ 追加为 tool 消息
        │    └─ ToolError / 异常 → 错误文本同样追加为 tool 消息（循环不中断）
        └─ 回到循环开头
```

**"因为上限停下"不等于失败**：此时会追加一次不带工具的收尾提问，让模型基于已有证据给出"能确认什么、缺什么"，因此接口仍返回 200 与可用答案，`llm_error` 里说明触发了哪个上限。

## 3. 三个工具与安全边界

| 工具 | 参数 | 返回 | 上限 |
|---|---|---|---|
| `list_directory` | `rel_path` | 目录条目（目录带 `/`） | 每层最多 60 项，跳过 `.*` 与 `IGNORED_DIRS` |
| `search_code` | `keyword`、`glob?`、`max_hits?` | `文件:行号: 命中行` | 默认 16 条、最多 40 条；单文件 ≤ 100 KB |
| `read_file` | `rel_path`、`start?`、`end?` | 带行号的内容 | 单次最多 200 行；单文件 ≤ 100 KB；非文本跳过 |
| `finish` | `answer`、`evidence?` | —（结束信号） | 不消耗探索步数；参数解析失败时退回正文 |

`finish` 是**控制类工具**（`repo_tools.CONTROL_TOOL_NAMES`），它不读仓库、只用来显式声明"回答完毕"。引入它的原因见第 7 节第 9 条。

### 声明的工具 ≠ 执行的工具（注册表的三层结构）

| 名字 | 含义 | 当前值 |
|---|---|---|
| `DECLARED_TOOL_NAMES` | 发给模型看的声明（schema） | `finish`、`list_directory`、`search_code`、`read_file` |
| `TOOL_NAMES` | 本地真正能执行的函数 | `list_directory`、`search_code`、`read_file` |
| `CONTROL_TOOL_NAMES` | 有声明、但由 Agent 循环解释的控制信号 | `finish` |

`finish` **没有本地实现函数**，这是有意为之：`call_tool()` 的契约是"执行完返回一段文本给模型"，而 `finish` 的语义是"停止执行"，二者混在同一通道会互相冲突。它的真正处理发生在循环里（取 `answer`、合并证据、`break`）。

为避免"声明与实现脱节"，schema 上用 `handled_by: "agent_loop"` 标注控制工具，三个名字集全部由 schema 推导，并在模块导入时执行 `registry_issues()` 一致性检查（不一致直接报错，而不是等到某次问答才发现）。

所有路径都解析成绝对路径并确认落在仓库内（`repo_tools._resolve_in_repo`），越界、绝对路径、目录当文件读、超大文件、非文本文件都会抛出**可读的** `ToolError`，错误文本原样回喂给模型让它自行纠正。

## 4. 上限与错误处理一览

| 配置项 | 默认值 | 作用 |
|---|---|---|
| `AGENT_MAX_STEPS` | 8 | 单次问答最多执行多少次工具调用 |
| `AGENT_TIMEOUT_SECONDS` | 90 | 单次问答墙钟时间上限 |
| `AGENT_CONTEXT_BUDGET` | 60000 | 回喂给模型的工具结果总字符预算 |

| 错误场景 | 处理方式 | 结果 |
|---|---|---|
| 模型请求失败（网络/401/超时） | 记录日志，返回 `llm_error` | 上层回退到摘要问答或规则回答 |
| 模型返回的 `arguments` 不是合法 JSON | 该次按空参数执行并把问题回给模型 | 循环继续 |
| 工具抛 `ToolError`（路径越界、文件不存在…） | 错误文本作为工具结果回喂 | 循环继续，模型可换参数 |
| 工具内部意外异常 | 捕获并回喂，同时记日志 | 循环继续，不中断整次请求 |
| 触及步数/时长上限 | 追加收尾提问生成答案 + 说明原因 | 200 + `stop_reason` |
| 连续两批调用完全相同 | 判定原地打转，提前收尾 | 200 + `no_new_info` |

## 5. 接口变化

`POST /api/chat` 新增可选字段 `mode`：

| mode | 行为 |
|---|---|
| `auto`（默认） | 有密钥 → Agent；Demo 模式 → 摘要问答 |
| `agent` | 强制走 Agent |
| `summary` | 只用已有分析摘要（Stage 0 之前的行为，保留作对照与降级） |

响应新增：`source`（`agent`/`llm`/`fallback`）、`evidence`（Agent 实际读过的文件）、`steps`、`stop_reason`、`trace`（工具调用轨迹）。

另外 `AnalysisResult` 现在会持久化 `repo_path`：Agent 在请求时是**重新加载分析结果**的，没有这个字段就找不到本地代码（这是本轮实测中发现并修掉的真实缺陷）。找不到副本时会提示"请重新 Analyze"，而不是静默失败。

## 6. 验证方式与实测结果

```bash
cd backend
python tests/test_agent_tools.py   # 工具层 36 项
python tests/test_agent_loop.py    # Agent 循环 34 项（假模型驱动真实工具）
python tests/test_e2e.py           # 真实栈 + 真实 LLM 44 项
python tests/test_logic.py         # 原有逻辑 69 项
```

| 测试 | 断言数 | 状态 |
|---|---|---|
| test_logic.py | 83 | ✅ 全绿 |
| test_agent_tools.py | 46 | ✅ 全绿 |
| test_agent_loop.py | 49 | ✅ 全绿 |
| test_e2e.py | 45 | ✅ 全绿（真实 openai 3.31.0 + 真实 DeepSeek） |

**真实模型跑出来的 Agent 轨迹**（`deepseek-flash`，问题："`search_code` 定义在哪个文件？又被哪些文件调用？"）：

```
source=agent  steps=5  stop=final_answer
trace: 1. search_code({'keyword': 'search_code'})
       2. list_directory({'rel_path': 'backend/app'})
       3. read_file({'rel_path': 'backend/app/services/search_service.py'})
       4. read_file({'rel_path': 'backend/app/main.py'})
       5. read_file({'rel_path': 'backend/app/services/worker.py'})
evidence=['backend/app/services/search_service.py', 'backend/app/main.py', 'backend/app/services/worker.py']
回答：**定义位置**：`backend/app/services/search_service.py:6` — `def search_code(keyword):` …（并列出两个调用点）
```

第 3 → 4 → 5 步的"读哪个文件"完全由搜索与上一份文件的内容决定，这正是 Stage 0 判定"需要 Agent"的那类任务。

## 7. 本轮发现并修掉的问题

1. **`repo_path` 未持久化** → Agent 永远找不到本地仓库（只有第一轮分析的内存里有）。已写入分析结果并加 `analysis_id` 兜底推导。
2. **搜索工具默认只扫 ≤5 KB 的源码文件** → 大文件里的符号搜不到。给 `repo_service.iter_source_files()` 增加 `suffixes` 参数，搜索改为覆盖全部文本后缀、上限 100 KB。
3. **搜不到时的引导不足** → 模型会反复换关键词烧光步数。搜不到时现在会明确建议换策略，并提示"该符号可能不在这个仓库里"。
4. **`list_directory` 截断计数错误**（显示的剩余条数算错）。
5. **点文件在所有链路里都不可见**（发现于"`.env.example` 能被搜到却读不了"这个不一致）→ 根因是 `repo_service._iter_visible_entries()` 一刀切跳过所有 `.` 开头的条目，导致 `.gitignore`、`.env.example`、`.github/workflows/*` 这些关键线索既不在目录树里、也搜不到、也读不了。现已改为：**点条目不再一律跳过**，噪音（`.git`、`.venv`、`.next`…）由 `IGNORED_DIRS`、新增的 `IGNORED_FILENAMES`（`.DS_Store`/`Thumbs.db`/`desktop.ini`）明确排除；同时把该函数提升为公开 API `iter_visible_entries()`，让目录树、配置识别、源码抽样与 Agent 工具共用**同一套"可见性"定义**。
6. **文本/二进制判定与搜索口径不一致** → `read_file` 用后缀集合判断，而 `Path(".env.example").suffix` 是 `.example`，于是"搜得到但读不了"。现在搜索与读取共用同一个 `repo_tools._is_probably_text()`，并对没有常规后缀的文件做一次**内容嗅探**（含 NUL 字节即判为二进制），既收下 `.env.example`/`.gitignore`/`Makefile`，又挡住 `.DS_Store` 这类无后缀二进制文件。
7. **测试替身的两个缺陷**：收尾提问（`tools=None`）时假模型仍返回工具调用、以及"重复调用"用例参数没变化——这两次都是测试写错，修正后生产代码未改动，说明循环的停止逻辑本身是对的。
8. 另外修正了两处**测试自身的断言缺陷**：把 `list_directory` 表头说明文字里的 "node_modules" 误当成条目（子串匹配），以及断言"跳过隐藏目录"（与新语义相反）。
9. **"结束"原先只靠隐式信号（模型不再请求工具），存在无答案的边界** → 新增 `finish` 工具作为显式结束信号：
   - `finish(answer, evidence?)` 不计入 `AGENT_MAX_STEPS`；
   - 同一批里既有探索工具又有 `finish` 时，先执行探索再用 `finish` 收尾；
   - `finish` 参数为空时退回正文作答；若反复（≥2 次）都为空则判失败（**有硬闸，不指望模型守规矩**）；
   - 证据清单改为"轨迹实际读过的文件 ∪ `finish` 自报的依据"；
   - 响应新增 `finished_by` 字段（`finish_tool` / `content` / `forced_conclusion`），让"用了哪条结束路径"可观测。
   实测：真实模型连续两次都以 `finished_by=finish_tool` 结束。
10. **模型请求层从 httpx 换成 openai SDK**（应用户决定）→ 只替换"接入层"，工具与循环未动：
    - `_client()` 构造 `OpenAI(api_key, base_url, timeout=120, max_retries=2)`；
    - 响应经 `response.model_dump()` 转成 dict，**对上层保持"进出都是 dict"的契约**，所以 `_extract_content`、`_extract_tool_calls`、Agent 循环与所有测试替身都无需感知 SDK 类型；
    - 新增 `sdk_base_url()` 做 base_url 归一（SDK 会自动拼 `/chat/completions`，必须带 `/v1`），并加断言固定"SDK base_url + /chat/completions == chat_completions_url()"的等价性；
    - 新增 `_describe_sdk_error()`：把 SDK 的类型化异常（401/403/429/超时/连接失败/4xx/5xx）翻成可执行中文提示——这是这次迁移最实在的收益；
    - 新增 `LLMNotConfigured`：未配密钥时返回可读原因而不是抛异常；
    - 实测环境为 **openai 3.31.0**（依赖 httpx2，httpx 不再是直接依赖），`max_tokens`/`tools`/`tool_choice`/异常类均兼容，且以 `-W error::DeprecationWarning` 运行无告警；
    - **客户端复用**：`_client()` 按 `(api_key, base_url, timeout, max_retries)` 签名记忆化，同配置返回同一个 `OpenAI` 实例（复用连接池），配置变化时自动重建。原因是一轮 Agent 问答要调用模型 5~9 次，每次新建客户端等于重复创建连接池；SDK 官方也要求复用 client。
    - **分层的价值在这次迁移里得到验证**：从 httpx 换成 SDK 只改了 `llm_service.py`、`config.py`、`requirements.txt` 三个文件，`agent_service.py` 与 `repo_tools.py` 一行未动——Agent 循环只管"何时调、调完干什么"，传输层的事（base_url 归一、超时、重试、异常分类）全部留在 `llm_service`。

## 8. Stage 1 刻意没做（留给后续）

- ❌ 不做代码修改/打补丁，不运行被测项目的测试（仍只"看"）；
- ❌ 不做跨仓库检索、不读 Issues/PR、不联网；
- ❌ 不引入 embedding / 向量索引（先用关键词搜索证明必要性）；
- ❌ 前端暂未展示 `evidence` / `trace` / `steps`（接口已返回，UI 展示留到 Stage 2）；
- ❌ 未做"评测集"（固定问题 + 期望依据文件），这是 Stage 3 的内容。

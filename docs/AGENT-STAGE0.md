# Agent 适用性与功能边界说明（Stage 0）

> 本文是 Agent 化改造的第一阶段产出：**只界定边界，不实现功能**。目标是把"哪些环节继续用固定流程、哪些环节值得交给 Agent、以及第一版的验收与停止标准"写清楚，作为后续每个 Stage 的对照基准。
>
> 文中所有代码引用均可在当前仓库中核实；标注为"**拟实现**"的部分尚未编码。

## 1. 一句话结论

> **项目概览可以通过固定流程生成；具体代码问题涉及哪些文件，往往需要阅读后才能确定。因此，本阶段计划让模型在受限工具集合内按需探索，并依据真实源码回答。**

## 2. 当前实现现状

| 能力 | 现有实现 | 现有实现的性质 |
|---|---|---|
| 仓库获取 | `repo_service.clone_repo()` 以 `git clone --depth 1` 落到 `backend/data/repos/<analysis_id>/` | 固定流程 |
| 上下文收集 | `repo_service.extract_repo_context()`：README、目录树、配置文件（根目录 + 子目录清单）、按规则抽样的源码 | 固定流程 |
| 概览生成 | `analysis_service.analyze()`：LLM 生成 → `AnalysisResult` 校验 → 失败则 `build_fallback_result()` 规则兜底 | 固定流程（LLM 只作为一次性生成器） |
| 仓库问答 | `llm_service.chat_with_llm(analysis, question)`：把**已有分析摘要**拼进 prompt，单次调用 | 单次 LLM 问答，**不含探索** |
| 问答兜底 | `analysis_service.chat_fallback()`：按关键词集合（`_TOPIC_OVERVIEW` / `_TOPIC_ARCH` / `_TOPIC_MODULES` / `_TOPIC_LEARN` / `_TOPIC_CONTRIBUTE`）归类，用已生成字段拼答案 | 固定流程 |

关键事实：`chat_with_llm()` 组装的上下文只来自分析结果本身——

```python
# backend/app/services/llm_service.py  chat_with_llm()
analysis_summary = json.dumps({
    "project_name": ..., "overview": ..., "architecture_summary": ...,
    "key_modules": ..., "developer_workflow": ...,
}, ensure_ascii=False, indent=2)
user_content = f"## 项目分析结果\n{analysis_summary}\n\n## 用户问题\n{question}"
```

也就是说：**问具体代码问题时，模型看不到源码，只能看到早就生成好的摘要。** 它没有能力"再去读一个文件"。

## 3. 用户任务

帮助用户理解一个陌生仓库，任务分成两类：

1. **整体认识**：这个项目做什么、目录怎么组织、从哪读起、怎么跑起来、能贡献什么。→ 一次性、可预先编排。
2. **具体代码问题**：某条逻辑怎么实现、某个行为在什么条件下发生、某个函数被谁调用。→ 答案落在哪些文件上，提问时无法预先确定。

第一类已经由现有概览流程覆盖；第二类是本阶段要改善的对象。

## 4. 判断：哪些继续 Workflow，哪些引入 Agent

| 环节 | 选择 | 理由 |
|---|---|---|
| 仓库下载、目录提取、配置识别、概览生成 | **保留 Workflow** | 步骤稳定、输入可枚举、结果可缓存；用固定流程更快、更便宜、更可复现 |
| 具体代码问答（多文件、需追调用链） | **引入 Agent（受限工具循环）** | 需要先读到 A 才知道该读 B，属于"下一步取决于新信息"，无法预先编排 |

判断依据可以压缩成一句：**能用固定顺序列完步骤的，就别交给模型决策；必须边看边决定看什么的，才需要 Agent。**

## 5. 当前方案的不足（要解决的具体痛点）

1. **问答上下文只有摘要**：真正的实现细节不在 prompt 里。
2. **代码采样是"猜"的**：概览阶段按规则取源码（`MAX_SOURCE_FILES = 5` 个、单文件 `MAX_SOURCE_FILE_SIZE = 5_000` 字节、目录深度 ≤ 3，来自 `repo_service.iter_source_files()`）。被采到的文件纯属规则命中，**与用户后来的问题无关**。
3. **无法追问式深入**：用户问"回退是怎么实现的"，系统不能先找入口、再顺藤摸瓜读第二个文件。
4. **兜底是关键词匹配，不是探索**：`chat_fallback()` 命中关键词就返回对应字段，未命中就给通用摘要。它看起来像"回答了"，但没有验证任何源码。

## 6. 一个证明"下一步取决于新信息"的例子

**用户问题**：`模型调用失败时，仓库问答接口怎样返回答案？`

这个问题在现有实现下只能得到模糊回答（因为摘要里没有回退链路）。而 Agent 的**拟实现**执行过程如下——每一步的下一步都由上一步的发现决定，而不是预先排好的：

| 步 | 动作（拟） | 上一步带来的新信息 | 下一步的决定依据 |
|---|---|---|---|
| 1 | `search_code("/api/chat")` | 命中 `backend/app/main.py` | 需要读这个入口 |
| 2 | `read_file("backend/app/main.py", 60-83)` | 发现入口调用 `llm_service.chat_with_llm(analysis, req.question)`，且用 `answer is not None` 分流 | 必须去看 `chat_with_llm` 何时返回 `None` |
| 3 | `read_file("backend/app/services/llm_service.py", 300-332)` | 发现两件事：Demo 模式返回 `(None, None)`；请求失败返回 `(None, 错误原因)` | 需要回到调用方看这两种 `None` 分别怎么处理 |
| 4 | `read_file("backend/app/main.py", 77-83)` | 发现 `answer is None` 时走 `analysis_service.chat_fallback(..., reason=llm_error)` | 需要看兜底回答如何组织 |
| 5 | `search_code("def chat_fallback")` → `read_file(..., 293-366)` | 确认它按关键词主题（`_match_topic`）选字段拼答案，并在 `reason` 非空时追加失败说明 | 证据已足够（入口 → 调用 → 失败分支 → 兜底实现），可以作答 |

依据以上文件，最终能给出**有据可查**的回答：

- 接口路径：`POST /api/chat`（`main.py`）；
- 先试 LLM：`llm_service.chat_with_llm()`；
- 失败时**不抛错**，而是回退规则回答：`analysis_service.chat_fallback()`，并把失败原因作为 `reason` 传入；
- 返回体形如 `{"answer": ..., "source": "fallback", "llm_error": "<原因>"}`；`source == "llm"` 表示走了模型；
- 前端据此把原因显示成橙色提示（`frontend/src/App.vue` 中判断 `data.source === 'fallback' && data.llm_error`）；
- 另外，Demo 模式属于预期状态而非失败，此时 `llm_error` 为空，回答末尾只提示"当前为 Demo 模式"。

**为什么这个例子值得写进边界说明**：它包含了一个**无法预先规划的转向**——第 3 步若不读 `chat_with_llm`，就不知道有两种不同的 `None`（"没配密钥"与"调用失败"），也就无法解释为什么 Demo 模式不会显示错误提示。这正是 Workflow 做不到、而 Agent 应该做到的部分。

## 7. 拟提供的工具集合（第一版只有三个）

| 工具 | 作用 | 关键约束 |
|---|---|---|
| `list_directory(rel_path)` | 查看某个目录下有哪些文件/子目录 | 只读已克隆仓库；复用 `repo_service.IGNORED_DIRS` 忽略列表；限制返回条数 |
| `search_code(keyword, glob?)` | 按关键字/符号定位文件，返回 `文件:行号` 与命中片段 | 与 `SOURCE_EXTENSIONS` 对齐；限制命中数量；超长文件跳过 |
| `read_file(rel_path, start?, end?)` | 读取指定文件（可指定行范围） | 只能读仓库内路径（复用 `storage_service` 里同类的路径校验思路）；限制单次读取字节数与总读取量 |

设计原则：**工具只能"看"，不能"改"，也不能联网。** 三个工具的输入输出都必须是可判定的、可断言的，方便后面用测试固定行为。

## 8. 第一版边界（明确不做什么）

- ✅ 只分析**已经克隆到本地**的仓库（`backend/data/repos/<analysis_id>/`），不新增任何网络访问；
- ✅ 只做**查找、阅读、解释**：回答"某逻辑在哪、怎么实现、被谁调用"；
- ✅ 保留现有概览流程作为入口，Agent 作为问答能力的增强，**不替换** `/api/analyze`；
- ❌ 不修改仓库代码、不提 PR、不跑被测项目的测试；
- ❌ 不做跨仓库检索，不做 GitHub Issues/PR 读取（第一版不引入新的外部依赖）；
- ❌ 不引入向量库或 embedding 索引（先用关键词搜索证明必要性）。

## 9. 成功标准与停止标准

**成功（可验收）**

1. 回答中给出的文件与函数**真实存在**，且能在回答里指出依据（`文件:行号`）；
2. 对"需要跨多个文件"的问题，回答质量明显优于只给摘要的现状；
3. 信息不足时，明确说明**缺什么**，而不是编一个看似合理的答案；
4. 达到调用上限时，返回已确认的部分结论 + 说明还差什么。

**停止（防失控）**

| 条件 | 阈值（第一版拟定，可在 Stage 1 调整） |
|---|---|
| 工具调用次数上限 | 8 次 |
| 单次问答总耗时上限 | 60 秒 |
| 单文件读取上限 | 与 `repo_service.MAX_FILE_SIZE` 一致（100 KB） |
| 单次问答读取总量上限 | 200 KB |
| 连续两次工具调用没有带来新信息 | 停止并作答 |

## 10. 明确不在本阶段范围（留给后续 Stage）

| Stage | 计划内容 |
|---|---|
| **Stage 1** | 实现三个只读工具的调用、结果回传、循环与上限控制；接入 `/api/chat`；用真实仓库跑通第 6 节那条链路 |
| Stage 2 | 让"入口选择"更可靠（例如先定位入口再做多跳追踪）；给回答附上证据清单 |
| Stage 3 | 评估是否需要索引/检索优化；补齐评测集（固定问题 + 期望依据文件） |

> 本文只产出判断与边界，**不含任何代码改动**。

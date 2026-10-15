"""Stage 1：最小可用的 Agent 循环。

设计依据见 docs/AGENT-STAGE0.md：
  * 概览仍走固定流程（analysis_service.analyze），本模块只增强"问答"；
  * 模型在受限的只读工具集合（repo_tools）内按需探索：
      搜索 → 读文件 → 发现调用关系 → 再读下一个文件 …… → 证据足够再回答；
  * 模型的一次 tool call 结果会作为 tool 消息回喂，下一步由新信息决定，而不是预先编排。

循环的边界（对应 Stage 0 第 9 节的"停止标准"）：
    最大工具调用次数、总耗时上限、连续无新信息即停止、单次工具失败不致命（回给模型自行纠正）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import (
    AGENT_CONTEXT_BUDGET,
    AGENT_MAX_STEPS,
    AGENT_NUDGE_ENABLED,
    AGENT_TIMEOUT_SECONDS,
    DEMO_MODE,
    REPOS_DIR,
)
from app.services import llm_service, repo_tools

# 只用于记录：结果预览与答案引用的截断长度
_TRACE_PREVIEW_CHARS = 160
_ANSWER_MAX_CHARS = 8_000

STOP_FINAL_ANSWER = "final_answer"        # 模型给出最终回答
STOP_STEP_LIMIT = "step_limit"            # 工具调用次数用尽
STOP_TIME_LIMIT = "time_limit"            # 总耗时用尽
STOP_NO_NEW_INFO = "no_new_info"          # 连续两次重复同一调用
STOP_LLM_ERROR = "llm_error"              # 调用模型失败且没有可用的已有信息

# 结束方式：finish 工具显式结束 / 直接用正文回答 / 达到上限后被动收尾
FINISH_TOOL = "finish_tool"
FINISH_CONTENT = "content"
FINISH_FORCED = "forced_conclusion"

# 模型调用失败时的同轮重试次数（应对偶发的 SSL EOF / 超时）
LLM_RETRY_LIMIT = 1

# 中途纠偏：用掉少量步数后、最近 N 步都没读实现文件时，注入系统提示。
# 这里刻意**不按 AGENT_MAX_STEPS 的比例**计算：漂移在开头几步就会发生，
# 按比例算会让触发点随预算一起后移（实测 25 步时触发点被推到 12 步，等于失效）。
_NUDGE_WINDOW = 3
_NUDGE_MIN_STEPS = 5          # 至少先走 5 步再打断
_NUDGE_MAX_TIMES = 2          # 最多打断两次，避免变成噪音


@dataclass
class AgentTraceStep:
    step: int
    tool: str
    arguments: dict
    result_preview: str


@dataclass
class AgentResult:
    answer: str | None = None
    citations: list[str] = field(default_factory=list)
    claimed_evidence: list[str] = field(default_factory=list)   # finish 里模型自报的依据文件
    trace: list[AgentTraceStep] = field(default_factory=list)
    steps: int = 0
    stop_reason: str = STOP_FINAL_ANSWER
    finished_by: str | None = None      # finish_tool / content / forced_conclusion
    error: str | None = None
    elapsed: float = 0.0
    implementation_files_read: list[str] = field(default_factory=list)   # 读过的实现文件（用于观测"是否读到实质"）
    nudges: int = 0                                                      # 中途纠偏触发次数

    @property
    def ok(self) -> bool:
        return self.answer is not None

    def trace_text(self) -> str:
        """给日志/调试用的一行式轨迹。"""
        return " -> ".join(f"{s.tool}({json.dumps(s.arguments, ensure_ascii=False)})" for s in self.trace) or "（未调用工具）"


SYSTEM_PROMPT_TEMPLATE = """\
你是 RepoAssistant 的仓库问答 Agent。用户已经分析过一个 GitHub 仓库，你可以在**已下载到本地的这份代码**中自主查找证据，然后回答问题。

## 仓库信息
- 项目名：{project_name}
- 仓库地址：{repo_url}
- 本地根目录：{repo_root}（所有工具路径都相对这个目录）
{tree_excerpt}
## 你的工具（只读，不能修改仓库、不能联网）
- `search_code(keyword, glob?, max_hits?)`：按关键字搜索，返回 `文件:行号` 与命中行。**keyword 是字面匹配，不是正则**：一次只搜一个词，不要用 `|` 连接多个词
- `read_file(rel_path, start?, end?)`：读取文件（带行号），建议先用搜索定位再读片段
- `list_directory(rel_path)`：查看目录结构（知道路径时可直接 read_file，不必先列目录）
- `finish(answer, evidence?)`：**结束本轮并给出最终答案**（不消耗探索步数）

## 工作方式
1. 需要看代码时**必须调用工具**，不要凭记忆或猜测回答；
2. 按需多轮探索：先搜索定位，再读文件；读到调用关系后可以继续追下一个文件；
3. **优先读实现文件，把预算用在答案所在的地方**：
   - **实现文件 = 提供功能的源码**（如 `src/<pkg>/*.py`）；**测试、变更日志（CHANGES）、贡献指南（CONTRIBUTING）、`docs/` 下的文档都不算实现文件**；
   - 先看包的入口（如 `src/<pkg>/__init__.py`）找出公共 API，再**顺着它的 import 语句**去读真正的实现模块，而不是停在文件名上；
   - **打开一个实现文件就把它读完**：`read_file` 不给范围（默认 120 行）或分段读到文件末尾；**不要只读中段就换目标**——同一文件的其他行往往就是答案（例如参数装饰器的定义就在 `decorators.py` 开头）。读完一个再去看下一个；
   - **除非用户明确问到文档/贡献流程/变更日志，否则不要用 >2 步去看这些内容**；它们的约定可以用一两步扫过，但不要挤掉读实现的时间；
   - `list_directory` 只在不知道目录结构时用，**不要为每个目录都列一遍**；已经知道文件路径就直接 `read_file`；
4. `search_code` 的关键词是**字面匹配**，不支持正则：一次只搜一个词或符号，多个词请分多次调用；
   也不要搜索"自己已经知道的文件名"（例如刚读过 `docs/api.md` 又去搜 `api.md`），那是无效搜索；
5. **结束时必须调用 `finish`**：把最终答案放进 `answer`，并用 `evidence` 列出你依据的文件路径；
   不要只在正文里写答案而不调用 finish——那样这轮会被判定为"没有回答"；
6. 预算：最多 {max_steps} 次探索工具调用（finish 不计入）、总时长 {timeout} 秒。
   用掉约一半预算后自查一次：**如果还没读过任何实现文件，下一步必须去读实现**；
   接近预算时，宁可先给出"已确认的结论 + 还缺什么证据"，也不要在没读实现的情况下凭文件名推断。

## 回答要求
- 只依据你实际读到的代码作答，给出**文件路径 + 行号或函数名**作为依据；
- 如果代码里没有足够信息，明确说明"缺少什么证据"，不要编造；
- 用中文，简洁，先结论后依据。
"""


def _tree_excerpt(directory_tree: str, limit: int = 1_500) -> str:
    if not directory_tree:
        return ""
    text = directory_tree[:limit]
    if len(directory_tree) > limit:
        text += "\n...（目录树已截断，可用 list_directory 查看完整结构）"
    return f"- 目录树（节选）：\n```\n{text}\n```\n"


def _build_messages(analysis: dict, repo_root: Path, question: str) -> list[dict]:
    system = SYSTEM_PROMPT_TEMPLATE.format(
        project_name=analysis.get("project_name", "未知"),
        repo_url=analysis.get("repo_url", ""),
        repo_root=repo_root.as_posix(),
        tree_excerpt=_tree_excerpt(analysis.get("directory_tree", "")),
        max_steps=AGENT_MAX_STEPS,
        timeout=int(AGENT_TIMEOUT_SECONDS),
    )
    overview = analysis.get("overview") or ""
    context_line = f"\n（已有的项目概览，仅供参考，不能替代读代码：{overview[:300]}）" if overview else ""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question + context_line},
    ]


def _extract_tool_calls(message: dict) -> tuple[list[dict], str | None]:
    """解析模型消息里的 tool_calls，返回 (调用列表, 解析错误)。

    兼容两种写法：arguments 是 JSON 字符串（OpenAI 标准）或已经是对象。
    参数不是合法 JSON 时不抛异常，而是记成错误让模型下一轮自行纠正。
    """
    raw_calls = message.get("tool_calls") or []
    calls: list[dict] = []
    parse_error: str | None = None

    for index, call in enumerate(raw_calls):
        func = (call or {}).get("function") or {}
        name = func.get("name") or ""
        raw_args = func.get("arguments")
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError as e:
                arguments = {}
                parse_error = f"工具 {name or index} 的参数不是合法 JSON（{e}），本轮按空参数处理"
        elif isinstance(raw_args, dict):
            arguments = raw_args
        elif raw_args is None:
            arguments = {}
        else:
            arguments = {}
            parse_error = f"工具 {name or index} 的参数类型不支持：{type(raw_args).__name__}"

        calls.append({
            "id": call.get("id") or f"call_{index}",
            "name": name,
            "arguments": arguments if isinstance(arguments, dict) else {},
        })

    return calls, parse_error


def _merge_citations(trace: list[AgentTraceStep], claimed: list[str]) -> list[str]:
    """证据清单 = 实际读过的文件 ∪ 模型用 finish 自报的依据。

    先用轨迹（客观、不会被编造），再补上模型自报里那些轨迹中没有的路径——
    模型可能引用的是搜索命中的文件而没读全文，这类也保留，便于人工核对。
    """
    seen: list[str] = []
    for step in trace:
        if step.tool != "read_file":
            continue
        path = str(step.arguments.get("rel_path", "")).strip()
        if path and path not in seen:
            seen.append(path)
    for path in claimed:
        cleaned = str(path).strip().lstrip("./")
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


_DOC_DIRS = {"docs", "doc", "documentation"}
_TEST_DIRS = {"tests", "test", "testing"}


def _classify_read(rel_path: str) -> str:
    """把一次 read_file 的路径分类：实现 / 包入口 / 文档 / 测试 / 配置 / 其它。

    只用于统计与"中途纠偏"判断，不参与安全或功能逻辑。
    注意 `__init__.py` 单独归为 "export"：它通常只是重导出清单，
    **不代表已经读过真正的实现**（实测模型读完它就以为"读过实现了"）。
    """
    rel = str(rel_path or "").strip().lstrip("./").replace("\\", "/")
    if not rel:
        return "other"
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return "other"
    if parts[0] in _DOC_DIRS:
        return "docs"
    if parts[0] in _TEST_DIRS:
        return "test"
    name = parts[-1].lower()
    if name == "__init__.py":
        return "export"
    if name.endswith(".py"):
        return "implementation"
    if name.startswith(("readme", "changes", "changelog", "contributing", "license")):
        return "docs"
    if name in {"pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "requirements.txt"}:
        return "config"
    return "other"


def _nudge_message(used: int, limit: int, result: AgentResult) -> str:
    """最近几步没读实现文件时，给出的确定性纠偏提示。"""
    last_impl = [s.arguments.get("rel_path") for s in result.trace if s.tool == "read_file"]
    opened = [p for p in last_impl if p] or ["你之前打开过的实现文件"]
    return (
        f"（系统提示：你已用 {used}/{limit} 步，但最近 {_NUDGE_WINDOW} 步都没有读实现文件。"
        f"请不要再看文档、测试、变更日志或列目录——回到 {opened[-1]!r} 把它读完"
        "（read_file 不带范围或接着上次的 end 继续），再读顺着它 import 到的实现模块。"
        "如果确实无法确认，就用已经读到的证据收尾，并在答案里说明缺口。）"
    )


def _cap_output(output: str, used: int, budget: int) -> tuple[str, int]:
    """给回喂给模型的工具结果加总量预算，避免多轮读取把上下文撑爆。"""
    text = str(output)
    remaining = budget - used
    if remaining <= 0:
        notice = "（本轮已达到工具结果的上下文预算，请基于已有信息作答，或缩小读取范围）"
        return notice, used
    if len(text) > remaining:
        text = text[:remaining] + "\n...（内容超预算已截断）"
    return text, used + len(text)


def run_agent_chat(analysis: dict, repo_root: Path, question: str) -> AgentResult:
    """跑一轮最小 Agent 循环，返回 AgentResult。"""
    result = AgentResult()
    started = time.monotonic()

    if DEMO_MODE:
        result.answer = None
        result.stop_reason = STOP_LLM_ERROR
        result.error = "未配置 API Key，当前为 Demo 模式"
        return result

    messages = _build_messages(analysis, repo_root, question)
    context_used = sum(len(str(m.get("content") or "")) for m in messages)
    previous_batch_signature: str | None = None
    empty_finish_attempts = 0
    llm_attempts = 0

    while True:
        result.elapsed = time.monotonic() - started

        if result.elapsed > AGENT_TIMEOUT_SECONDS:
            result.stop_reason = STOP_TIME_LIMIT
            result.error = f"已达总时长上限 {int(AGENT_TIMEOUT_SECONDS)} 秒"
            break

        response, error = llm_service.request_chat_completion(
            messages, tools=repo_tools.TOOL_SCHEMAS, temperature=0.2
        )
        if response is None:
            # 同一轮重试一次：真实环境里偶发的 SSL EOF / 超时不该让整次问答直接失败
            llm_attempts += 1
            if llm_attempts <= LLM_RETRY_LIMIT and result.elapsed < AGENT_TIMEOUT_SECONDS:
                llm_service.log_llm_error("Agent 模型调用失败，本侧重试", str(error))
                continue
            result.stop_reason = STOP_LLM_ERROR
            result.error = error
            break

        try:
            choice = response["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            result.stop_reason = STOP_LLM_ERROR
            result.error = "模型响应结构异常（缺少 choices[0].message）"
            break

        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            # 输出被 max_tokens 截断：这是基础设施问题，说清楚比让模型"再答一次"更有用
            result.stop_reason = STOP_LLM_ERROR
            result.error = "模型输出超过 max_tokens 被截断（finish_reason=length），请缩小问题范围或提高输出上限"
            break

        calls, parse_error = _extract_tool_calls(message)
        if not calls:
            # 模型没有调用任何工具 → 这就是最终回答（协议里 finish_reason="stop" 的形态）
            answer = (message.get("content") or "").strip()
            result.answer = answer[:_ANSWER_MAX_CHARS] if answer else None
            if result.answer is None:
                result.stop_reason = STOP_LLM_ERROR
                # 带上真正的 finish_reason，避免所有异常都报同一句话
                result.error = f"模型既没有调用工具，也没有给出回答（finish_reason={finish_reason!r}）"
            else:
                result.stop_reason = STOP_FINAL_ANSWER
                result.finished_by = FINISH_CONTENT
            break

        if parse_error:
            llm_service.log_llm_error("Agent 工具参数解析", parse_error)

        # finish 是"结束"信号：从待执行列表中摘出来，且不占用探索步数
        finishing = [c for c in calls if c["name"] in repo_tools.CONTROL_TOOL_NAMES]
        research_calls = [c for c in calls if c["name"] not in repo_tools.CONTROL_TOOL_NAMES]

        # 一批里出现多个 finish：只用第一个，其余忽略（模型偶尔会重复声明结束）
        if len(finishing) > 1:
            llm_service.log_llm_error("Agent 重复 finish", f"同一批里有 {len(finishing)} 个 finish 调用，已取第一个")

        if research_calls and result.steps + len(research_calls) > AGENT_MAX_STEPS:
            result.stop_reason = STOP_STEP_LIMIT
            result.error = f"已达工具调用次数上限 {AGENT_MAX_STEPS} 次"
            break

        # 同一批探索调用与上一批完全相同 → 判定为原地打转，提前收尾
        batch_signature = "|".join(
            f"{c['name']}:{json.dumps(c['arguments'], ensure_ascii=False, sort_keys=True)}" for c in research_calls
        )
        if research_calls and batch_signature == previous_batch_signature:
            result.stop_reason = STOP_NO_NEW_INFO
            result.error = "模型重复了同一批工具调用，判定为没有新信息"
            break
        if research_calls:
            previous_batch_signature = batch_signature

        # 回放模型的这一轮消息（含 tool_calls），再逐条追加工具结果
        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
        })

        for call in research_calls:
            try:
                output = repo_tools.call_tool(repo_root, call["name"], call["arguments"])
            except repo_tools.ToolError as e:
                output = f"工具调用失败：{e}"
            except Exception as e:                      # 工具内部意外错误也不能中断整个循环
                output = f"工具执行异常（{e.__class__.__name__}）：{e}"
                llm_service.log_llm_error("Agent 工具异常", f"{call['name']}: {e}")

            fed_back, context_used = _cap_output(output, context_used, AGENT_CONTEXT_BUDGET)
            result.steps += 1
            result.trace.append(AgentTraceStep(
                step=result.steps,
                tool=call["name"],
                arguments=call["arguments"],
                result_preview=" ".join(str(output).split())[:_TRACE_PREVIEW_CHARS],
            ))
            # 统计读过的实现文件：既是可观测指标，也是"是否需要纠偏"的判断依据
            if call["name"] == "read_file":
                rel = str(call["arguments"].get("rel_path", ""))
                if _classify_read(rel) == "implementation" and rel not in result.implementation_files_read:
                    result.implementation_files_read.append(rel)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": fed_back,
            })

        # 中途纠偏：走够少量步数、且最近若干步都没读实现文件 → 注入系统提示。
        # 判据是"最近几步读了什么"（含 __init__.py 这类入口不算实现），
        # 因此"开头读一个就漂走"也能被纠正；这是确定性机制，不依赖模型自觉。
        if AGENT_NUDGE_ENABLED and not finishing:
            if (result.steps >= _NUDGE_MIN_STEPS
                    and result.nudges < _NUDGE_MAX_TIMES
                    and len(result.trace) >= _NUDGE_MIN_STEPS):
                recent = result.trace[-_NUDGE_WINDOW:]
                read_kinds = [
                    _classify_read(str(s.arguments.get("rel_path", "")))
                    for s in recent if s.tool == "read_file"
                ]
                if "implementation" not in read_kinds:
                    result.nudges += 1
                    messages.append({"role": "user", "content": _nudge_message(result.steps, AGENT_MAX_STEPS, result)})
                    llm_service.log_llm_error(
                        "Agent 中途纠偏",
                        f"已用 {result.steps}/{AGENT_MAX_STEPS} 步，最近 {_NUDGE_WINDOW} 步没读实现文件，"
                        f"已注入提示（第 {result.nudges} 次）",
                    )

        if finishing:
            # 模型显式声明结束：优先用 finish 的 answer；解析不出来就退回 content
            finish_call = finishing[0]
            finish_answer, finish_evidence = repo_tools.parse_finish_arguments(finish_call["arguments"])
            fallback = (message.get("content") or "").strip()
            result.answer = (finish_answer or fallback)[:_ANSWER_MAX_CHARS] or None
            result.claimed_evidence = finish_evidence
            if result.answer is None:
                # finish 却没给答案：不指望模型守规矩，最多给它一次机会，否则直接判失败
                empty_finish_attempts += 1
                if empty_finish_attempts > 1:
                    result.stop_reason = STOP_LLM_ERROR
                    result.error = "模型反复（≥2 次）调用 finish 都没有给出答案内容"
                    break
                messages.append({
                    "role": "user",
                    "content": "（系统提示：你刚才调用了 finish，但 answer 是空的。请重新调用 finish 并填写完整的 answer。）",
                })
                continue
            result.stop_reason = STOP_FINAL_ANSWER
            result.finished_by = FINISH_TOOL
            break

    result.elapsed = time.monotonic() - started

    if result.answer is None and result.stop_reason != STOP_LLM_ERROR:
        # 因为上限而停下：让模型基于已获得的证据给一个收尾回答，避免整个请求失败
        result.answer = _ask_for_conclusion(messages, result)
        if result.answer is not None:
            result.finished_by = FINISH_FORCED

    result.citations = _merge_citations(result.trace, result.claimed_evidence)
    return result


def _ask_for_conclusion(messages: list[dict], result: AgentResult) -> str | None:
    """达到上限后，用已有对话再要一次回答；同时会把限制如实告诉模型。"""
    notice = result.error or "已达探索上限"
    follow_up = messages + [{
        "role": "user",
        "content": (
            f"（系统提示：{notice}，现在不能再调用工具了。）\n"
            "请只根据已经读到的内容给出当前能确认的结论，"
            "并明确说明哪些部分因证据不足无法确认。"
        ),
    }]
    response, error = llm_service.request_chat_completion(follow_up, tools=None, temperature=0.2)
    if response is None:
        llm_service.log_llm_error("Agent 收尾回答失败", error or "未知错误")
        return None
    try:
        content = (response["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        return None
    return content[:_ANSWER_MAX_CHARS] if content else None


def ask_repository(analysis: dict, question: str, repo_path: str | Path | None = None) -> AgentResult:
    """对外的入口：按 analysis 定位本地仓库目录，跑一轮 Agent 问答。

    定位顺序：显式传入的路径 → 分析结果里记录的 repo_path → 按 analysis_id 推导出的默认克隆目录。
    分析结果按 URL 复用，克隆缓存却可能被清理，因此每次都要确认目录真的存在。
    """
    result = AgentResult()
    candidates: list[str | Path] = []
    if repo_path:
        candidates.append(repo_path)
    if analysis.get("repo_path"):
        candidates.append(analysis["repo_path"])
    if analysis.get("analysis_id"):
        candidates.append(REPOS_DIR / str(analysis["analysis_id"]))

    root: Path | None = None
    for candidate in candidates:
        path = Path(candidate)
        if path.is_dir():
            root = path
            break

    if root is None:
        result.stop_reason = STOP_LLM_ERROR
        result.error = (
            "找不到本地仓库副本（可能克隆缓存已被清理）。"
            "请重新点击 Analyze 让系统重新克隆，再进行代码问答。"
        )
        return result

    return run_agent_chat(analysis, root, question)

import json
import logging
import re

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)

from app.config import (
    DEMO_MODE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
)

logger = logging.getLogger("repoassistant.llm")

DEFAULT_BASE_URL = "https://api.deepseek.com"

# 请求层参数：超时与 SDK 内置退避重试次数
_REQUEST_TIMEOUT_SECONDS = 120.0
_SDK_MAX_RETRIES = 2


class LLMNotConfigured(RuntimeError):
    """没配密钥却尝试调用模型。"""

ANALYSIS_SYSTEM_PROMPT = """\
你是一个 GitHub 仓库分析助手。根据用户提供的仓库上下文信息，生成结构化的项目分析结果。

你必须返回一个合法的 JSON 对象，包含以下字段（所有字段都必须存在）：

{
  "overview": "项目概述，简要说明项目目标、核心功能和适用场景（2-4 句话）",
  "architecture_summary": "架构摘要，说明主要语言、技术栈、顶层目录结构和代码组织方式",
  "code_map": [
    {"name": "目录或文件名", "description": "该目录/文件的作用说明"}
  ],
  "key_modules": [
    {"name": "模块名", "description": "该模块为什么值得优先阅读", "key_files": ["代表性文件路径"]}
  ],
  "learning_path": [
    {"step": 1, "title": "步骤标题", "description": "该步骤的具体说明"}
  ],
  "contribution_suggestions": [
    {"title": "建议标题", "description": "具体说明", "difficulty": "beginner/intermediate"}
  ],
  "developer_workflow": "编号开发流程，每个步骤单独一行，步骤中的命令必须用反引号包裹",
  "coding_conventions": "代码规范和风格说明"
}

要求：
- code_map 列出主要目录及其作用，不超过 10 项
- code_map 的 name 只写目录或文件名本身（例如 src），不要带结尾的斜杠
- key_modules 列出最值得优先阅读的 3-5 个模块，key_files 填入实际存在的文件路径
- learning_path 提供 4-5 步学习建议，从易到难
- contribution_suggestions 提供 3-4 条入门贡献建议
- developer_workflow 必须使用编号步骤格式，每个步骤独占一行，例如：
  1. 克隆仓库后安装依赖：`uv sync`。
  2. 运行测试：`pytest tests/` 或 `tox run -e py312`。
  3. 运行格式检查：`pre-commit run --all-files`。
- developer_workflow 不要写成一整段长句；不要把所有命令集中到最后；命令或工具名必须保留在对应步骤里并用反引号包裹
- 所有内容使用中文
- 只返回 JSON，不要返回其他内容\
"""

CHAT_SYSTEM_PROMPT = """\
你是一个 GitHub 仓库学习助手。根据提供的项目分析结果，回答用户关于该项目的问题。
回答要准确、简洁、有条理。使用中文回答。如果分析结果中没有相关信息，请如实说明。\
"""

_MAX_CONTEXT_CHARS = 15_000
_MAX_OUTPUT_TOKENS = 8192


def _scan_json(text: str, start: int) -> tuple[str | None, int, bool]:
    """从 text[start]（必须是引号）开始扫描一个字符串。

    返回 (token, 结束位置, 是否完整)；未闭合时 token 为 None。
    """
    i = start + 1
    out: list[str] = []
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 < len(text):
                out.append(text[i:i + 2])
                i += 2
                continue
            return None, i, False
        if ch == '"':
            return "".join(out), i + 1, True
        out.append(ch)
        i += 1
    return None, i, False


def _scan_plain_value(text: str, i: int) -> tuple[int, bool]:
    """扫描数字/true/false/null 这类裸值，返回 (结束位置, 是否完整)。"""
    start = i
    while i < len(text) and text[i] not in ",}]":
        i += 1
    token = text[start:i].strip()
    if not token:
        return i, False
    return i, bool(re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?|true|false|null", token))


def _repair_truncated_json(text: str) -> dict | None:
    """模型输出被截断时，回退到最后一个完整位置并补齐未闭合的括号，再尝试解析。

    思路：单趟扫描记录所有"安全的回退点"——已经解析完一个完整值之后、逗号之前的位置。
    截断点可能是半个键名（`…, "desc`）、半个字符串值（`…: "前端使用 Vu`）或半个数组元素，
    从保留内容最多的回退点开始逐个往前试，补上对应的 `}` / `]` 后能解析成功就采用。
    补出来的对象可能缺少末尾字段，交由上层用 AnalysisResult 校验，仍缺字段就回退规则分析。
    """
    candidates: list[tuple[int, str]] = [(len(text), "")]   # (截断位置, 该位置对应的未闭合括号串)
    stack: list[str] = []
    i = 0

    def closers(opened: list[str]) -> str:
        return "".join("}" if ch == "{" else "]" for ch in reversed(opened))

    while i < len(text):
        ch = text[i]
        if ch == '"':
            _, end, complete = _scan_json(text, i)
            if not complete:
                # 截断在字符串中间：先试"把这个字符串补完"，能解析就保住整个值。
                # 注意 _scan_json 对未闭合字符串返回 token=None，这里要用原文切片判断内容
                partial = text[i:end]
                if len(partial) > 1 and not partial.endswith("\\"):
                    candidates.append((end, '"' + closers(stack)))
                break
            after = end
            while after < len(text) and text[after] in " \t\r\n":
                after += 1
            if after < len(text) and text[after] == ":":
                i = after + 1                     # 这是键，继续找它的值
                continue
            i = end
            candidates.append((i, closers(stack)))  # 一个完整的值到此结束
            continue
        if ch == "{":
            stack.append("{")
        elif ch == "[":
            stack.append("[")
        elif ch in "}]":
            if stack:
                stack.pop()
            candidates.append((i + 1, closers(stack)))
        elif ch not in " \t\r\n:,":
            end, complete = _scan_plain_value(text, i)
            if not complete:
                break
            i = end
            candidates.append((i, closers(stack)))
            continue
        i += 1

    for cut, tail in reversed(candidates):
        candidate = re.sub(r"[,:]\s*$", "", text[:cut].rstrip()) + tail
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def chat_completions_url(base_url: str) -> str:
    """把用户配置的 base_url 补全成 /v1/chat/completions，已经带 /v1 的不会被拼成 /v1/v1。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        url = "https://api.deepseek.com"
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def _trim_context(text: str) -> str:
    if len(text) <= _MAX_CONTEXT_CHARS:
        return text
    return text[:_MAX_CONTEXT_CHARS] + "\n...（内容过长已截断）"


def _build_analysis_user_prompt(
    readme: str | None,
    directory_tree: str,
    config_files: dict[str, str],
    sample_sources: list[str],
) -> str:
    parts: list[str] = []

    if readme:
        parts.append(f"## README\n{readme}")

    parts.append(f"## 目录树\n{directory_tree}")

    if config_files:
        parts.append("## 配置文件")
        for name, content in config_files.items():
            parts.append(f"### {name}\n{content}")

    if sample_sources:
        parts.append("## 代表性源码片段")
        for src in sample_sources:
            parts.append(src)

    full = "\n\n".join(parts)
    return _trim_context(full)


def _short_error(exc: BaseException) -> str:
    """错误描述里绝不带上请求头，避免密钥进入日志或响应。"""
    text = " ".join(str(exc).split())
    if not text:
        text = exc.__class__.__name__
    return text[:200]


def _extract_content(response: dict) -> tuple[str | None, str | None]:
    try:
        return response["choices"][0]["message"]["content"], None
    except (KeyError, IndexError, TypeError):
        return None, "响应结构不符合 OpenAI 兼容格式（缺少 choices[0].message.content）"


def log_llm_error(scope: str, reason: str) -> None:
    """给上层（Agent 循环等）复用同一套日志出口，保证失败原因都留在服务端日志里。"""
    logger.warning("%s：%s", scope, reason)


# ---------------------------------------------------------------------------
# 模型请求层（openai SDK）
#   * SDK 只负责"接入"：拼请求、读响应、分类异常、退避重试；
#   * 工具声明、参数校验、工具执行、结果回传、循环上限仍在本项目里实现。
# ---------------------------------------------------------------------------

def sdk_base_url(base_url: str) -> str:
    """把配置里的 base_url 归一化成 SDK 需要的形式。

    SDK 会在 base_url 后自动拼 `/chat/completions`，所以这里必须补上 `/v1`；
    带尾斜杠、已带 `/v1` 的写法都要吃得下。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        url = DEFAULT_BASE_URL
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def _client() -> OpenAI:
    """按当前配置构造 SDK 客户端（每次新建，便于测试替换与配置变更生效）。"""
    if not DEEPSEEK_API_KEY:
        raise LLMNotConfigured("未配置 DEEPSEEK_API_KEY")
    return OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=sdk_base_url(DEEPSEEK_BASE_URL),
        timeout=_REQUEST_TIMEOUT_SECONDS,
        max_retries=_SDK_MAX_RETRIES,
    )


def _describe_sdk_error(exc: BaseException) -> str:
    """把 SDK 的分类异常翻成可执行的中文原因，供前端直接展示。"""
    status = getattr(exc, "status_code", None)
    if isinstance(exc, AuthenticationError) or status == 401:
        return f"鉴权失败（HTTP {status or 401}）：请检查 API Key 是否有效"
    if isinstance(exc, PermissionDeniedError) or status == 403:
        return f"无访问权限（HTTP {status or 403}）：该密钥可能无权调用此模型"
    if isinstance(exc, RateLimitError) or status == 429:
        return f"触发限流（HTTP {status or 429}）：请稍后重试或降低调用频率"
    if isinstance(exc, APITimeoutError):
        return f"请求超时（{int(_REQUEST_TIMEOUT_SECONDS)} 秒）：可缩小问题范围后重试"
    if status == 400:
        return f"请求被服务端拒绝（HTTP 400）：多半是模型名或参数不受支持 —— {_short_error(exc)}"
    if status == 404:
        return f"接口或模型不存在（HTTP 404）：请检查 DEEPSEEK_BASE_URL 与 DEEPSEEK_MODEL —— {_short_error(exc)}"
    if status and status >= 500:
        return f"服务端错误（HTTP {status}）：稍后重试 —— {_short_error(exc)}"
    if isinstance(exc, APIConnectionError):            # 含 APITimeoutError 之外的连接问题
        return f"网络不通或服务不可达：{_short_error(exc)}"
    if status:
        return f"HTTP {status} {_short_error(exc)}"
    return f"{exc.__class__.__name__}：{_short_error(exc)}"


def request_chat_completion(
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
) -> tuple[dict | None, str | None]:
    """调用一次对话补全，可选带上工具定义。

    返回 (响应 JSON, 错误描述)；错误绝不静默。
    带 tools 且模型决定调用工具时，choices[0].message.tool_calls 会是 OpenAI 兼容结构。

    对上层保持"进出都是 dict"的契约：SDK 的响应对象在这里 model_dump() 成 dict，
    这样 _extract_content / _extract_tool_calls / 各测试替身都不用关心 SDK 类型。
    """
    try:
        client = _client()
    except LLMNotConfigured as e:
        return None, str(e)

    kwargs: dict = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": _MAX_OUTPUT_TOKENS,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as e:
        reason = _describe_sdk_error(e)
        logger.warning("LLM 调用失败（%s）：%s", DEEPSEEK_MODEL, reason)
        return None, reason

    return response.model_dump(), None


def _call_chat(messages: list[dict]) -> tuple[dict | None, str | None]:
    """单轮调用（不带工具），返回 (响应 JSON, 错误描述)。"""
    return request_chat_completion(messages, tools=None)


def analyze_with_llm(repo_context: dict) -> tuple[dict | None, str | None]:
    """返回 (分析结果, 错误描述)。DEMO_MODE 下返回 (None, None) 表示"未尝试调用"。"""
    if DEMO_MODE:
        return None, None

    user_prompt = _build_analysis_user_prompt(
        readme=repo_context.get("readme"),
        directory_tree=repo_context.get("directory_tree", ""),
        config_files=repo_context.get("config_files", {}),
        sample_sources=repo_context.get("sample_sources", []),
    )

    response, error = _call_chat([
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ])
    if response is None:
        return None, error

    content, error = _extract_content(response)
    if content is None:
        logger.warning("LLM 响应无法解析：%s", error)
        return None, error

    content = content.strip()
    if content.startswith("```"):
        first_newline = content.index("\n") if "\n" in content else 3
        content = content[first_newline + 1:]
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
        content = content.strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        parsed = _repair_truncated_json(content)
        if parsed is None:
            reason = f"模型返回的内容不是合法 JSON：{_short_error(e)}"
            logger.warning("LLM 输出解析失败：%s", reason)
            return None, reason
        logger.warning("LLM 输出被截断，已按未闭合结构补全后使用（可能缺少末尾字段）")

    if not isinstance(parsed, dict):
        reason = "模型返回的 JSON 顶层不是对象"
        logger.warning("LLM 输出解析失败：%s", reason)
        return None, reason

    return parsed, None


def chat_with_llm(analysis: dict, question: str) -> tuple[str | None, str | None]:
    """返回 (回答, 错误描述)。无法调用时返回 (None, 原因)。"""
    if DEMO_MODE:
        # Demo 模式是预期状态而不是失败：返回空的错误原因，前端才不会误报"LLM 调用失败"
        return None, None

    analysis_summary = json.dumps(
        {
            "project_name": analysis.get("project_name", ""),
            "overview": analysis.get("overview", ""),
            "architecture_summary": analysis.get("architecture_summary", ""),
            "key_modules": analysis.get("key_modules", []),
            "developer_workflow": analysis.get("developer_workflow", ""),
        },
        ensure_ascii=False,
        indent=2,
    )

    user_content = f"## 项目分析结果\n{analysis_summary}\n\n## 用户问题\n{question}"

    response, error = _call_chat([
        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ])
    if response is None:
        return None, error

    content, error = _extract_content(response)
    if content is None:
        logger.warning("LLM 响应无法解析：%s", error)
        return None, error

    return content, None

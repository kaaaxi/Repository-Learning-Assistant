import json

import httpx

from app.config import (
    DEMO_MODE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
)

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
  "developer_workflow": "开发流程说明，包含常用命令",
  "coding_conventions": "代码规范和风格说明"
}

要求：
- code_map 列出主要目录及其作用，不超过 10 项
- key_modules 列出最值得优先阅读的 3-5 个模块，key_files 填入实际存在的文件路径
- learning_path 提供 4-5 步学习建议，从易到难
- contribution_suggestions 提供 3-4 条入门贡献建议
- 所有内容使用中文
- 只返回 JSON，不要返回其他内容\
"""

CHAT_SYSTEM_PROMPT = """\
你是一个 GitHub 仓库学习助手。根据提供的项目分析结果，回答用户关于该项目的问题。
回答要准确、简洁、有条理。使用中文回答。如果分析结果中没有相关信息，请如实说明。\
"""

_MAX_CONTEXT_CHARS = 15_000


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


def _call_chat(messages: list[dict]) -> dict | None:
    try:
        resp = httpx.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 4096,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _extract_content(response: dict) -> str | None:
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return None


def analyze_with_llm(repo_context: dict) -> dict | None:
    if DEMO_MODE:
        return None

    user_prompt = _build_analysis_user_prompt(
        readme=repo_context.get("readme"),
        directory_tree=repo_context.get("directory_tree", ""),
        config_files=repo_context.get("config_files", {}),
        sample_sources=repo_context.get("sample_sources", []),
    )

    response = _call_chat([
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ])
    if response is None:
        return None

    content = _extract_content(response)
    if content is None:
        return None

    content = content.strip()
    if content.startswith("```"):
        first_newline = content.index("\n") if "\n" in content else 3
        content = content[first_newline + 1:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def chat_with_llm(analysis: dict, question: str) -> str | None:
    if DEMO_MODE:
        return None

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

    response = _call_chat([
        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ])
    if response is None:
        return None

    return _extract_content(response)

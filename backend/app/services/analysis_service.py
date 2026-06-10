import json
from pathlib import Path

from app.config import DEMO_MODE
from app.services import repo_service, storage_service
from app.services import llm_service

import re as _re

SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".jsx", ".tsx", ".vue"}
MAX_SOURCE_FILES = 5
MAX_SOURCE_SIZE = 5_000
OVERVIEW_MAX_PARAGRAPHS = 3
OVERVIEW_MAX_CHARS = 800

_RE_HTML_TAG = _re.compile(r"<[^>]+>")
_RE_MD_IMAGE = _re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_MD_LINK = _re.compile(r"\[([^\]]*)\]\([^)]*\)")
_RE_HEADING = _re.compile(r"^#{1,6}\s+", _re.MULTILINE)
_RE_CODE_BLOCK = _re.compile(r"```[\s\S]*?```")
_RE_INLINE_CODE = _re.compile(r"`([^`]+)`")
_RE_COMMENT = _re.compile(r"<!--[\s\S]*?-->")
_RE_MULTI_BLANK = _re.compile(r"\n{3,}")


def clean_readme_overview(readme: str) -> str:
    if not readme or not readme.strip():
        return "未找到 README 文件。"

    text = _RE_COMMENT.sub("", readme)
    text = _RE_CODE_BLOCK.sub("", text)
    text = _RE_HTML_TAG.sub("", text)
    text = _RE_MD_IMAGE.sub("", text)
    text = _RE_MD_LINK.sub(r"\1", text)
    text = _RE_HEADING.sub("", text)
    text = _RE_INLINE_CODE.sub(r"\1", text)

    paragraphs: list[str] = []
    for block in text.split("\n\n"):
        block = block.strip()
        block = _RE_MULTI_BLANK.sub("\n", block)
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        para = " ".join(lines)
        if para:
            paragraphs.append(para)
        if len(paragraphs) >= OVERVIEW_MAX_PARAGRAPHS:
            break

    result = "\n\n".join(paragraphs)
    if len(result) > OVERVIEW_MAX_CHARS:
        result = result[:OVERVIEW_MAX_CHARS].rstrip() + "..."
    return result or "未找到 README 文件。"


def _detect_language(config_files: dict[str, str]) -> str:
    if "go.mod" in config_files:
        return "Go"
    if "Cargo.toml" in config_files:
        return "Rust"
    if "pom.xml" in config_files or "build.gradle" in config_files:
        return "Java"
    if "package.json" in config_files:
        return "JavaScript/TypeScript"
    if "requirements.txt" in config_files or "pyproject.toml" in config_files:
        return "Python"
    return "Unknown"


def _extract_top_dirs(tree_text: str) -> list[str]:
    dirs: list[str] = []
    for line in tree_text.splitlines():
        line = line.strip()
        if line.startswith("├── ") or line.startswith("└── "):
            name = line[4:]
            if name.endswith("/"):
                dirs.append(name.rstrip("/"))
    return dirs


DIR_DESCRIPTIONS: dict[str, str] = {
    "src": "核心源码目录，包含项目主要业务逻辑和功能实现",
    "app": "应用主目录，包含入口文件和核心模块",
    "lib": "库代码目录，封装项目的核心功能和工具类",
    "tests": "测试用例目录，包含单元测试和集成测试",
    "test": "测试用例目录，包含单元测试和集成测试",
    "docs": "项目文档目录，包含使用说明、API 文档和设计文档",
    "documentation": "项目文档目录，包含使用说明、API 文档和设计文档",
    "examples": "示例代码目录，包含项目用法的演示代码",
    "demo": "示例代码目录，包含项目用法的演示代码",
    "scripts": "脚本和工具目录，包含构建、部署和自动化脚本",
    "tools": "工具目录，包含辅助开发、调试和运维的工具",
    "config": "配置目录，包含项目的运行时和构建时配置",
    "configs": "配置目录，包含项目的运行时和构建时配置",
    "cli": "命令行接口目录，包含 CLI 命令的定义和处理逻辑",
    "api": "API 接口目录，包含路由定义、请求处理和接口文档",
    "core": "核心模块目录，包含项目的基础能力和公共组件",
    "utils": "工具函数目录，包含通用的辅助函数和工具类",
    "helpers": "辅助函数目录，包含通用的辅助工具",
    "models": "数据模型目录，包含数据结构定义和 ORM 映射",
    "views": "视图层目录，包含页面渲染和前端展示逻辑",
    "controllers": "控制器目录，包含请求处理和业务编排逻辑",
    "middleware": "中间件目录，包含请求拦截、认证和日志等中间件",
    "services": "服务层目录，包含核心业务逻辑和外部调用封装",
    "components": "组件目录，包含可复用的 UI 或功能组件",
    "assets": "静态资源目录，包含样式、图片和字体等文件",
    "public": "公共资源目录，包含可直接访问的静态文件",
    "migrations": "数据库迁移目录，包含数据库结构变更脚本",
    "benchmarks": "基准测试目录，包含性能测试和对比基准",
    "plugins": "插件目录，包含可扩展的插件模块",
}


KEY_MODULE_REASONS: dict[str, str] = {
    "src": "这是理解项目功能的主入口，阅读这里的代码能快速掌握整体设计和业务逻辑",
    "app": "包含应用的核心流程和入口，是了解项目如何运行的第一站",
    "lib": "封装了项目最核心的功能，理解这里的代码有助于掌握项目的设计思路",
    "core": "提供了项目的基础能力和公共抽象，阅读后能理解整体架构和设计模式",
    "api": "定义了项目对外暴露的接口，阅读后能快速了解项目提供了哪些能力",
    "cli": "包含命令行交互的全部逻辑，适合想了解用户交互流程的开发者",
    "models": "定义了项目的核心数据结构，理解数据模型是阅读业务逻辑的前提",
    "services": "包含核心业务逻辑的实现，是理解项目功能细节的关键",
    "controllers": "编排了请求处理流程，阅读后能理清业务处理的完整链路",
    "views": "包含页面渲染逻辑，适合想了解前端展示和用户交互的开发者",
    "components": "包含可复用的组件实现，阅读后能了解项目如何组织和复用代码",
    "utils": "包含大量工具函数，适合在阅读核心代码时作为辅助参考",
    "helpers": "提供通用的辅助能力，在遇到具体工具需求时可以查阅",
    "middleware": "包含请求拦截和横切关注点的处理逻辑，对理解项目非功能需求很有帮助",
    "config": "定义了项目的运行时配置，阅读后能了解项目支持哪些可配置项",
    "configs": "定义了项目的运行时配置，阅读后能了解项目支持哪些可配置项",
    "plugins": "展示了项目的扩展机制，适合想了解如何扩展项目功能的开发者",
    "migrations": "记录了数据结构的演变历史，有助于理解数据模型的来龙去脉",
}


def _describe_dir(dirname: str) -> str:
    return DIR_DESCRIPTIONS.get(dirname.lower(), f"项目目录，包含与 {dirname} 相关的文件和模块")


def _explain_module(dirname: str) -> str:
    if dirname.lower() in KEY_MODULE_REASONS:
        return KEY_MODULE_REASONS[dirname.lower()]
    return f"浏览 {dirname}/ 目录可以了解项目的组织方式，建议从主入口文件开始阅读"


def _collect_key_files(repo_path: Path, dirname: str) -> list[str]:
    dir_path = repo_path / dirname
    if not dir_path.is_dir():
        return []
    files: list[str] = []
    for child in sorted(dir_path.iterdir()):
        if child.is_file() and child.suffix in SOURCE_EXTENSIONS:
            files.append(f"{dirname}/{child.name}")
            if len(files) >= 5:
                break
    return files


def _extract_sample_sources(repo_path: Path, tree_text: str) -> list[str]:
    top_dirs = _extract_top_dirs(tree_text)
    sources: list[str] = []
    for dirname in top_dirs:
        dir_path = repo_path / dirname
        if not dir_path.is_dir():
            continue
        for child in sorted(dir_path.iterdir()):
            if child.suffix in SOURCE_EXTENSIONS and child.stat().st_size <= MAX_SOURCE_SIZE:
                snippet = child.read_text(encoding="utf-8", errors="replace")
                sources.append(f"--- {dirname}/{child.name} ---\n{snippet}")
                if len(sources) >= MAX_SOURCE_FILES:
                    return sources
    return sources


def build_fallback_result(ctx: dict, repo_url: str, sample_sources: list[str]) -> dict:
    readme = ctx.get("readme") or ""
    tree_text = ctx.get("directory_tree") or ""
    configs = ctx.get("config_files") or {}
    project_name = ctx.get("project_name", "unknown")
    language = _detect_language(configs)

    readme_preview = clean_readme_overview(readme)

    top_dirs = _extract_top_dirs(tree_text)

    code_map = [
        {"name": d, "description": _describe_dir(d)}
        for d in top_dirs[:10]
    ]
    if not code_map:
        code_map = [{"name": project_name, "description": "项目根目录"}]

    repo_path = Path(ctx.get("repo_path", ""))
    key_modules = [
        {
            "name": d,
            "description": _explain_module(d),
            "key_files": _collect_key_files(repo_path, d),
        }
        for d in top_dirs[:5]
    ]

    if "package.json" in configs:
        try:
            pkg = json.loads(configs["package.json"])
            scripts = pkg.get("scripts", {})
            if scripts:
                dev_workflow = "常用命令：\n"
                for cmd, val in scripts.items():
                    dev_workflow += f"  npm {cmd} — {val}\n"
            else:
                dev_workflow = "这是一个 Node.js 项目，请查看 package.json 了解可用脚本。"
        except json.JSONDecodeError:
            dev_workflow = "这是一个 Node.js 项目。"
    elif "requirements.txt" in configs or "pyproject.toml" in configs:
        dev_workflow = "这是一个 Python 项目，通常先安装依赖，例如 pip install -r requirements.txt。"
    elif "go.mod" in configs:
        dev_workflow = "这是一个 Go 项目，通常可以使用 go build 构建，并使用 go test ./... 运行测试。"
    elif "Cargo.toml" in configs:
        dev_workflow = "这是一个 Rust 项目，通常可以使用 cargo build 构建，并使用 cargo test 运行测试。"
    else:
        dev_workflow = "请查看项目配置文件或 README 了解构建、运行和测试方式。"

    return {
        "analysis_id": ctx["analysis_id"],
        "project_name": project_name,
        "repo_url": repo_url,
        "analysis_mode": "demo",
        "overview": readme_preview,
        "directory_tree": tree_text,
        "architecture_summary": f"主要语言：{language}。顶层目录：{', '.join(top_dirs) or '无'}。",
        "code_map": code_map,
        "key_modules": key_modules,
        "learning_path": [
            {"step": 1, "title": "阅读 README", "description": "先阅读项目 README，了解项目目标、核心功能和基础使用方式。"},
            {"step": 2, "title": "查看配置文件", "description": f"查看 {'、'.join(configs.keys())}，理解依赖、构建配置和运行入口。" if configs else "查看项目配置文件，理解依赖和构建方式。"},
            {"step": 3, "title": "浏览顶层目录", "description": f"重点浏览 {', '.join(top_dirs[:5])}，建立项目代码组织的整体印象。"},
            {"step": 4, "title": "阅读代表性源码", "description": "选择核心目录中的少量源码文件，理解项目的代码风格和主要abstract。"},
            {"step": 5, "title": "尝试本地运行", "description": "按照开发流程在本地安装依赖、运行项目或执行测试。"},
        ],
        "contribution_suggestions": [
            {"title": "阅读贡献指南", "description": "优先查找 CONTRIBUTING.md、开发文档或 README 中的贡献说明。", "difficulty": "beginner"},
            {"title": "修复小问题或文档错误", "description": "从拼写错误、README 表述、简单 bug 或 good first issue 入手。", "difficulty": "beginner"},
            {"title": "补充测试", "description": "阅读现有测试结构，为已有模块补充边界用例或回归测试。", "difficulty": "intermediate"},
            {"title": "改进文档", "description": "完善 README、示例代码、注释或模块说明，降低新用户理解成本。", "difficulty": "beginner"},
        ],
        "developer_workflow": dev_workflow,
        "coding_conventions": f"主要语言：{language}。建议先阅读现有源码和测试，沿用仓库中已经形成的命名、目录组织和代码风格。",
    }


_TOPIC_OVERVIEW = {"做", "概述", "简介", "介绍", "是什么", "关于", "overview", "about", "干啥", "干嘛", "用途"}
_TOPIC_ARCH = {"架构", "目录", "代码地图", "结构", "arch", "directory", "tree", "structure", "文件组织", "codemap"}
_TOPIC_MODULES = {"模块", "关键", "核心", "module", "key", "core", "功能"}
_TOPIC_LEARN = {"学习", "怎么学", "从哪", "入门", "如何开始", "上手", "learn", "getting started", "read", "看起"}
_TOPIC_CONTRIBUTE = {"贡献", "贡献者", "contribute", "issue", "任务", "入门任务", "good first", "pr", "参与"}


def _match_topic(question: str) -> str:
    q = question.lower()
    for keyword in _TOPIC_CONTRIBUTE:
        if keyword in q:
            return "contribute"
    for keyword in _TOPIC_LEARN:
        if keyword in q:
            return "learn"
    for keyword in _TOPIC_MODULES:
        if keyword in q:
            return "modules"
    for keyword in _TOPIC_ARCH:
        if keyword in q:
            return "arch"
    for keyword in _TOPIC_OVERVIEW:
        if keyword in q:
            return "overview"
    return ""


def chat_fallback(analysis: dict, question: str) -> dict:
    topic = _match_topic(question)
    name = analysis.get("project_name", "该项目")

    if topic == "overview":
        return {"answer": analysis.get("overview", "暂无项目概述。")}

    if topic == "arch":
        arch = analysis.get("architecture_summary", "")
        code_map = analysis.get("code_map", [])
        parts = [f"{name} 的架构信息："]
        if arch:
            parts.append(arch)
        if code_map:
            parts.append("主要目录：")
            for item in code_map:
                parts.append(f"  - {item['name']}：{item['description']}")
        return {"answer": "\n".join(parts)}

    if topic == "modules":
        modules = analysis.get("key_modules", [])
        if not modules:
            return {"answer": f"{name} 暂未识别到关键模块。"}
        parts = [f"{name} 的关键模块："]
        for m in modules:
            parts.append(f"  {m['name']}：{m['description']}")
            if m.get("key_files"):
                parts.append(f"    代表性文件：{', '.join(m['key_files'])}")
        return {"answer": "\n".join(parts)}

    if topic == "learn":
        path = analysis.get("learning_path", [])
        if not path:
            return {"answer": f"建议先阅读 {name} 的 README，然后浏览顶层目录结构。"}
        parts = [f"{name} 的建议学习路径："]
        for s in path:
            parts.append(f"  {s['step']}. {s['title']} — {s['description']}")
        return {"answer": "\n".join(parts)}

    if topic == "contribute":
        suggestions = analysis.get("contribution_suggestions", [])
        if not suggestions:
            return {"answer": f"建议查看 {name} 的 Issues 列表，寻找标记为 good first issue 的任务。"}
        parts = [f"{name} 的入门贡献建议："]
        for c in suggestions:
            parts.append(f"  - {c['title']}（{c['difficulty']}）：{c['description']}")
        return {"answer": "\n".join(parts)}

    # 未匹配到具体主题，返回通用回答
    parts = [
        f"关于 {name}，以下是目前已有的分析摘要：",
        "",
        f"项目概述：{analysis.get('overview', '暂无')[:200]}",
        "",
        f"开发流程：{analysis.get('developer_workflow', '暂无')}",
        "",
        "你可以尝试更具体的问题，例如：",
        "  - 这个项目是做什么的？",
        "  - 项目的架构和目录结构是怎样的？",
        "  - 有哪些关键模块？",
        "  - 我应该怎么学习这个项目？",
        "  - 如何为这个项目做贡献？",
        "",
        "当前使用 Demo 模式，回答基于已提取的仓库信息生成规则化回复。",
        "请在 backend/.env 中配置 DEEPSEEK_API_KEY 后使用真实 LLM 分析。",
    ]
    return {"answer": "\n".join(parts)}


def analyze(repo_url: str) -> dict:
    analysis_id = repo_service.generate_analysis_id(repo_url)

    try:
        existing = storage_service.load_analysis(analysis_id)
        if DEMO_MODE:
            return existing
        cached_mode = existing.get("analysis_mode")
        if cached_mode == "llm":
            return existing
    except FileNotFoundError:
        pass

    ctx = repo_service.extract_repo_context(repo_url, analysis_id)

    repo_path = Path(ctx["repo_path"])
    sample_sources = _extract_sample_sources(repo_path, ctx.get("directory_tree", ""))

    llm_context = {**ctx, "sample_sources": sample_sources}
    llm_result = llm_service.analyze_with_llm(llm_context)

    if llm_result is not None:
        llm_result.setdefault("analysis_id", ctx["analysis_id"])
        llm_result.setdefault("project_name", ctx.get("project_name", "unknown"))
        llm_result.setdefault("repo_url", repo_url)
        llm_result["directory_tree"] = ctx["directory_tree"]
        llm_result["analysis_mode"] = "llm"
        storage_service.save_analysis(analysis_id, llm_result)
        return llm_result

    result = build_fallback_result(ctx, repo_url, sample_sources)
    storage_service.save_analysis(analysis_id, result)
    return result

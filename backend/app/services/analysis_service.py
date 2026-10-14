import json
import logging
import re as _re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.config import ANALYSIS_CACHE_TTL, DEMO_MODE
from app.models import AnalysisResult
from app.services import llm_service, repo_service, storage_service

logger = logging.getLogger("repoassistant.analysis")

# 目录树行首的连接符与竖线，用于判断层级深度
_TREE_PREFIX_RE = _re.compile(r"^([│├└─\s]*)[├└]── (.*)$")

OVERVIEW_MAX_PARAGRAPHS = 3
OVERVIEW_MAX_CHARS = 800

DIFFICULTY_ORDER = {"beginner": 0, "intermediate": 1, "advanced": 2}

MODE_DEMO = "demo"
MODE_LLM = "llm"

_RE_HTML_TAG = _re.compile(r"<[^>]+>")
_RE_MD_IMAGE = _re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 允许链接地址里带一层成对括号，例如 https://en.wikipedia.org/wiki/Foo_(bar)
_RE_MD_LINK = _re.compile(r"\[([^\]]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")
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
    names = {Path(name).name for name in config_files}
    if "go.mod" in names:
        return "Go"
    if "Cargo.toml" in names:
        return "Rust"
    if "pom.xml" in names or "build.gradle" in names:
        return "Java"
    if "package.json" in names:
        return "JavaScript/TypeScript"
    if "requirements.txt" in names or "pyproject.toml" in names:
        return "Python"
    return "Unknown"


def _tree_entry_depth(prefix: str) -> int:
    """由行首前缀算出层级：每 4 个字符代表一层，同时兼容仅用竖线缩进、不含空格的写法。"""
    if not prefix:
        return 0
    width = len(prefix.expandtabs(4))
    return width // 4 if width >= 4 else prefix.count("│")


def parse_tree_entries(tree_text: str) -> list[tuple[int, str, bool]]:
    """把目录树文本解析成 [(深度, 名字, 是否目录)]，深度由缩进还原。"""
    entries: list[tuple[int, str, bool]] = []
    for line in tree_text.splitlines():
        match = _TREE_PREFIX_RE.match(line)
        if not match:
            continue
        prefix, name = match.group(1), match.group(2)
        entries.append((_tree_entry_depth(prefix), name, name.endswith("/")))
    return entries


def extract_top_dirs(tree_text: str) -> tuple[list[str], list[str]]:
    """返回 (顶层目录, 根目录下的文件)。

    只有层级为 0 的条目才算顶层；子目录曾经被当作顶层目录返回，导致关键模块与源码抽样
    全部落在错误的位置。
    """
    dirs: list[str] = []
    files: list[str] = []
    # 目录树按 DFS 输出：同一层的名字依次出现，进入子层后深度递增。
    # 用栈记录每一层当前所在的目录名即可还原层级，避免把子目录误当成顶层目录。
    name_stack: list[str] = []

    for depth, name, is_dir in parse_tree_entries(tree_text):
        del name_stack[depth:]
        if depth == 0:
            (dirs if is_dir else files).append(name.rstrip("/") if is_dir else name)
        if is_dir:
            name_stack.append(name.rstrip("/"))
    return dirs, files


def _describe_dir(dirname: str) -> str:
    return DIR_DESCRIPTIONS.get(dirname.lower(), f"项目目录，包含与 {dirname} 相关的文件和模块")


def _explain_module(dirname: str) -> str:
    if dirname.lower() in KEY_MODULE_REASONS:
        return KEY_MODULE_REASONS[dirname.lower()]
    return f"浏览 {dirname}/ 目录可以了解项目的组织方式，建议从主入口文件阅读"


def _collect_key_files_by_dir(repo_path: Path, top_dirs: list[str]) -> dict[str, list[str]]:
    """为每个顶层目录收集真实存在的相对路径（如 backend/app/main.py）。"""
    key_files: dict[str, list[str]] = {}
    for dirname in top_dirs:
        if not (repo_path / dirname).is_dir():
            key_files[dirname] = []
            continue
        found = repo_service.iter_source_files(
            repo_path / dirname,
            max_files=5,
            max_depth=3,
            max_size=repo_service.MAX_FILE_SIZE,
        )
        key_files[dirname] = sorted(rel for rel, _ in found)
    return key_files


def _source_snippets(repo_path: Path) -> list[str]:
    return [
        f"--- {rel} ---\n{content}"
        for rel, content in repo_service.iter_source_files(repo_path)
    ]


def _difficulty_rank(value: str) -> int:
    return DIFFICULTY_ORDER.get(str(value).strip().lower(), 99)


def build_fallback_result(ctx: dict, repo_url: str) -> dict:
    readme = ctx.get("readme") or ""
    tree_text = ctx.get("directory_tree") or ""
    configs = ctx.get("config_files") or {}
    project_name = ctx.get("project_name", "unknown")
    language = _detect_language(configs)

    readme_preview = clean_readme_overview(readme)

    top_dirs, root_files = extract_top_dirs(tree_text)

    code_map = [
        {"name": d, "description": _describe_dir(d)}
        for d in top_dirs[:10]
    ]
    for name in root_files[: max(0, 10 - len(code_map))]:
        code_map.append({"name": name, "description": "仓库根目录下的入口或说明文件"})
    if not code_map:
        code_map = [{"name": project_name, "description": "项目根目录"}]

    repo_path = Path(ctx.get("repo_path", ""))
    key_files_by_dir = _collect_key_files_by_dir(repo_path, top_dirs)
    key_modules = [
        {
            "name": d,
            "description": _explain_module(d),
            "key_files": key_files_by_dir.get(d, []),
        }
        for d in top_dirs[:5]
    ]

    config_names = "、".join(configs.keys())
    if "package.json" in configs:
        try:
            pkg = json.loads(configs["package.json"])
            scripts = pkg.get("scripts", {})
            if scripts:
                lines = ["常用命令："]
                lines += [f"  {i}. `npm {cmd}` — {val}" for i, (cmd, val) in enumerate(scripts.items(), 1)]
                dev_workflow = "\n".join(lines)
            else:
                dev_workflow = "这是一个 Node.js 项目，请查看 package.json 了解可用脚本。"
        except json.JSONDecodeError:
            dev_workflow = "这是一个 Node.js 项目。"
    elif "requirements.txt" in configs or "pyproject.toml" in configs:
        dev_workflow = "这是一个 Python 项目，通常先安装依赖，例如 `pip install -r requirements.txt`。"
    elif "go.mod" in configs:
        dev_workflow = "这是一个 Go 项目，通常可以使用 `go build` 构建，并使用 `go test ./...` 运行测试。"
    elif "Cargo.toml" in configs:
        dev_workflow = "这是一个 Rust 项目，通常可以使用 `cargo build` 构建，并使用 `cargo test` 运行测试。"
    else:
        dev_workflow = "请查看项目配置文件或 README 了解构建、运行和测试方式。"

    return {
        "analysis_id": ctx["analysis_id"],
        "project_name": project_name,
        "repo_url": repo_url,
        "analysis_mode": MODE_DEMO,
        "overview": readme_preview,
        "directory_tree": tree_text,
        "architecture_summary": f"主要语言：{language}。顶层目录：{', '.join(top_dirs) or '无'}。",
        "code_map": code_map,
        "key_modules": key_modules,
        "learning_path": [
            {"step": 1, "title": "阅读 README", "description": "先阅读项目 README，了解项目目标、核心功能和基础使用方式。"},
            {"step": 2, "title": "查看配置文件", "description": f"查看 {config_names}，理解依赖、构建配置和运行入口。" if configs else "查看项目配置文件，理解依赖和构建方式。"},
            {"step": 3, "title": "浏览顶层目录", "description": f"重点浏览 {', '.join(top_dirs[:5])}，建立项目代码组织的整体印象。" if top_dirs else "从仓库根目录开始，建立项目代码组织的整体印象。"},
            {"step": 4, "title": "阅读代表性源码", "description": "选择核心目录中的少量源码文件，理解项目的代码风格和主要抽象。"},
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


def _normalize_llm_result(raw: dict, ctx: dict, repo_url: str) -> dict | None:
    """校验 LLM 输出：缺字段或类型不符就交给 Demo 兜底，避免前端拿到残缺数据。"""
    payload = dict(raw)
    payload.setdefault("analysis_id", ctx["analysis_id"])
    payload.setdefault("project_name", ctx.get("project_name", "unknown"))
    payload.setdefault("repo_url", repo_url)
    payload["directory_tree"] = ctx["directory_tree"]
    payload["analysis_mode"] = MODE_LLM

    for item in payload.get("code_map") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            # LLM 常见写法 src/，与前端模板拼接后会变成 src//
            item["name"] = item["name"].strip().rstrip("/")

    try:
        validated = AnalysisResult.model_validate(payload)
    except ValidationError as e:
        logger.warning("LLM 返回的结构不完整，已回退到规则分析：%s", e.errors()[:3])
        return None

    result = validated.model_dump()
    result["analysis_mode"] = MODE_LLM
    return result


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


def chat_fallback(analysis: dict, question: str, reason: str | None = None) -> dict:
    topic = _match_topic(question)
    name = analysis.get("project_name", "该项目")

    if topic == "overview":
        answer = analysis.get("overview", "暂无项目概述。")
    elif topic == "arch":
        arch = analysis.get("architecture_summary", "")
        code_map = analysis.get("code_map", [])
        parts = [f"{name} 的架构信息："]
        if arch:
            parts.append(arch)
        if code_map:
            parts.append("主要目录：")
            for item in code_map:
                parts.append(f"  - {item['name']}：{item['description']}")
        answer = "\n".join(parts)
    elif topic == "modules":
        modules = analysis.get("key_modules", [])
        if not modules:
            answer = f"{name} 暂未识别到关键模块。"
        else:
            parts = [f"{name} 的关键模块："]
            for m in modules:
                parts.append(f"  {m['name']}：{m['description']}")
                if m.get("key_files"):
                    parts.append(f"    代表性文件：{', '.join(m['key_files'])}")
            answer = "\n".join(parts)
    elif topic == "learn":
        path = analysis.get("learning_path", [])
        if not path:
            answer = f"建议先阅读 {name} 的 README，然后浏览顶层目录结构。"
        else:
            parts = [f"{name} 的建议学习路径："]
            for s in path:
                parts.append(f"  {s['step']}. {s['title']} — {s['description']}")
            answer = "\n".join(parts)
    elif topic == "contribute":
        suggestions = analysis.get("contribution_suggestions", [])
        if not suggestions:
            answer = f"建议查看 {name} 的 Issues 列表，寻找标记为 good first issue 的任务。"
        else:
            parts = [f"{name} 的入门贡献建议："]
            for c in suggestions:
                parts.append(f"  - {c['title']}（{c['difficulty']}）：{c['description']}")
            answer = "\n".join(parts)
    else:
        answer = "\n".join([
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
        ])

    # 只有 LLM 真的失败过才解释原因；Demo 模式属于预期行为，不显示错误。
    if reason:
        answer += (
            f"\n\n（本次回答由规则生成：LLM 调用失败——{reason}。"
            "请检查 backend/.env、密钥文件或网络后重试。）"
        )
    elif DEMO_MODE:
        answer += "\n\n（当前为 Demo 模式，回答基于已提取的仓库信息规则生成。配置密钥后可获得 LLM 回答。）"

    return {"answer": answer, "source": "fallback", "llm_error": reason}


def _cache_is_fresh(existing: dict) -> bool:
    if ANALYSIS_CACHE_TTL <= 0:
        return True
    stamp = existing.get("analyzed_at")
    if not stamp:
        # 没有时间戳的历史缓存视为已过期，便于升级后重新分析一次
        return False
    try:
        created = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds()
    return age < ANALYSIS_CACHE_TTL


def analyze(repo_url: str, force_refresh: bool = False) -> dict:
    analysis_id = repo_service.generate_analysis_id(repo_url)

    if not force_refresh:
        try:
            existing = storage_service.load_analysis(analysis_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            # Demo 模式下的规则结果只依赖仓库本身，直接复用
            if DEMO_MODE:
                return existing
            # 有密钥时不要把规则结果当成最终结果：要么重新分析，要么复用未过期的 LLM 结果
            if existing.get("analysis_mode") == MODE_LLM and _cache_is_fresh(existing):
                return existing

    ctx = repo_service.extract_repo_context(repo_url, analysis_id)

    repo_path = Path(ctx["repo_path"])
    sample_sources = _source_snippets(repo_path)

    llm_context = {**ctx, "sample_sources": sample_sources}
    llm_result, llm_error = llm_service.analyze_with_llm(llm_context)

    if llm_error:
        logger.warning("LLM 分析失败，回退到规则分析：%s", llm_error)

    result = None
    if isinstance(llm_result, dict):
        result = _normalize_llm_result(llm_result, ctx, repo_url)

    if result is None:
        result = build_fallback_result(ctx, repo_url)

    result["analyzed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["llm_error"] = llm_error
    storage_service.save_analysis(analysis_id, result)
    return result


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
    "migrations": "数据库迁移目录，包含数据库迁移脚本",
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

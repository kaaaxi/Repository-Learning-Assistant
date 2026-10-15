"""Agent 的只读工具层：只"看"仓库，不改、不联网。

三个工具与 AGENT-STAGE0.md 第 7 节的约定一致：
    list_directory(rel_path)          看目录
    search_code(keyword, glob?)       按关键字/符号定位，返回 文件:行号
    read_file(rel_path, start?, end?) 读文件（可指定行范围，带行号）

设计约束（都可用测试固定）：
  * 所有路径都会解析成绝对路径并确认落在仓库目录内，越界即报错；
  * 忽略规则复用 repo_service.IGNORED_DIRS，不进入 node_modules/.git 等目录；
  * 单文件大小、单次读取行数、搜索结果数量都有上限；
  * 出错一律抛 ToolError，由 Agent 循环把它作为工具结果回给模型，让模型自行纠正。
"""

from __future__ import annotations

from pathlib import Path

from app.services import repo_service

# 目录列表
MAX_LIST_ITEMS = 60
# 搜索
DEFAULT_SEARCH_HITS = 16
MAX_SEARCH_HITS = 40
SNIPPET_MAX_CHARS = 160
# 读取
DEFAULT_READ_LINES = 120
MAX_READ_LINES = 200
MAX_TEXT_FILE_SIZE = repo_service.MAX_FILE_SIZE          # 100 KB，与目录树/配置抽取保持一致

TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".hpp",
    ".sh", ".ps1", ".bat", ".sql", ".md", ".rst", ".txt", ".toml", ".ini", ".cfg",
    ".yaml", ".yml", ".json", ".xml", ".html", ".css", ".scss", ".env",
}

# 没有后缀、但有确定文件名的文本文件。点文件（.gitignore、.env.example、.dockerignore…）
# 不能靠 suffix 判断：Path(".env.example").suffix 是 ".example"，Path(".gitignore").suffix 是空字符串。
TEXT_FILENAMES = {
    "Dockerfile", "Makefile", "LICENSE", "README", "go.mod", "Cargo.toml", "requirements.txt",
}

# 明确是二进制的后缀：这些即使没有后缀名规则也不当文本读
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz", ".pdf",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".whl", ".egg",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyo", ".class", ".wasm",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav", ".mov", ".avi",
    ".sqlite", ".db", ".lockb",
}

# 搜索覆盖面要比"源码抽样"更宽：能当文本读的文件都应能被搜到。
# 具体筛选由 _is_probably_text 决定（含点文件与内容嗅探），这里只保留源码后缀作为参考集合。
SEARCH_SUFFIXES = TEXT_SUFFIXES | repo_service.SOURCE_EXTENSIONS


class ToolError(Exception):
    """工具无法完成请求时抛出；消息会原样回给模型，因此必须可读、可据此纠正。"""


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

def _resolve_in_repo(root: Path, rel_path: str) -> Path:
    """把相对路径解析到仓库内；越界、不是文件/目录时抛 ToolError。"""
    root = root.resolve()
    raw = (rel_path or ".").strip().replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ToolError(f"只接受仓库内的相对路径，不接受绝对路径：{rel_path!r}")
    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(f"路径越出仓库范围：{rel_path!r}")
    return candidate


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root.resolve()).as_posix() or "."


def _looks_binary(path: Path) -> bool:
    """读开头一小段字节，含 NUL 就判定为二进制。用于兜住没有后缀名的二进制文件（如 .DS_Store）。"""
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(1024)
    except OSError:
        return True


def _is_probably_text(path: Path) -> bool:
    """判断是否按文本读取——搜索与读取共用这一个判定，保证"搜得到就读得到"。

    顺序：先排已知二进制后缀，再认已知文本后缀与文件名，最后按"无常规后缀/点文件"
    收进来，并做一次内容嗅探兜住 .DS_Store 这类没有后缀名的二进制文件。
    """
    suffix = path.suffix.lower()
    if suffix in BINARY_SUFFIXES:
        return False
    if suffix in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
        return True
    if not suffix or path.name.startswith("."):
        return not _looks_binary(path)
    return False


def _read_text(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT_FILE_SIZE:
        raise ToolError(
            f"文件过大（{path.stat().st_size} 字节 > {MAX_TEXT_FILE_SIZE}），请改用 search_code 定位片段"
        )
    if not _is_probably_text(path):
        raise ToolError(f"看起来不是文本文件，已跳过：{path.name}")
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ToolError(f"读取失败：{e}") from e


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def list_directory(root: Path, rel_path: str = ".") -> str:
    """列出仓库内某个目录的条目（目录带 / 后缀）。"""
    target = _resolve_in_repo(root, rel_path)
    if not target.exists():
        raise ToolError(f"目录不存在：{rel_path}")
    if not target.is_dir():
        raise ToolError(f"不是目录：{rel_path}，如果想看内容请用 read_file")

    subdirs: list[str] = []
    files: list[str] = []
    # 复用 repo_service 的忽略规则：`.git`、`node_modules` 等被排除，
    # 但 `.gitignore`、`.env.example`、`.github/` 这类有信息量的点文件会正常列出。
    for child in repo_service.iter_visible_entries(target):
        if child.is_dir():
            subdirs.append(child.name + "/")
        else:
            files.append(f"{child.name} ({child.stat().st_size} B)")

    if not subdirs and not files:
        return f"{_rel(root, target)} 下没有可列出的条目（可能为空目录，或全部被忽略）。"

    entries = [f"  {name}" for name in subdirs] + [f"  {name}" for name in files]
    truncated = len(entries) > MAX_LIST_ITEMS
    lines = [f"{_rel(root, target)} 下的内容（已跳过 .git、node_modules 等噪音目录）："]
    lines += entries[:MAX_LIST_ITEMS]
    if truncated:
        lines.append(f"  ...（另有 {len(entries) - MAX_LIST_ITEMS} 项未显示）")
    return "\n".join(lines)


def search_code(root: Path, keyword: str, glob: str | None = None, max_hits: int = DEFAULT_SEARCH_HITS) -> str:
    """按关键字搜索仓库内的文本文件，返回 文件:行号 与命中行。"""
    keyword = (keyword or "").strip()
    if not keyword:
        raise ToolError("keyword 不能为空")
    if len(keyword) > 200:
        raise ToolError("keyword 过长（上限 200 字符）")

    try:
        limit = max(1, min(int(max_hits), MAX_SEARCH_HITS))
    except (TypeError, ValueError):
        limit = DEFAULT_SEARCH_HITS

    needle = keyword.lower()
    hits: list[str] = []
    scanned = 0

    for rel, content in repo_service.iter_source_files(
        root,
        max_files=10_000,
        max_depth=repo_service.MAX_TREE_DEPTH,
        max_size=MAX_TEXT_FILE_SIZE,
        # 与 read_file 用同一个判定：搜得到的文件必然读得到
        predicate=_is_probably_text,
    ):
        if glob and not Path(rel).match(glob):
            continue
        scanned += 1
        for lineno, line in enumerate(content.splitlines(), 1):
            if needle in line.lower():
                hits.append(f"{rel}:{lineno}: {line.strip()[:SNIPPET_MAX_CHARS]}")
                if len(hits) >= limit:
                    return "\n".join(hits) + f"\n（命中数已达上限 {limit}，如需更多请缩小关键词）"

    if not hits:
        return (
            f"没有找到 {keyword!r}（已扫描 {scanned} 个文本文件）。\n"
            "建议：换更短或用词根的关键词（例如只留函数名的一部分）、"
            "用 list_directory 先确认目录结构，或直接 read_file 看最可能的文件；"
            "如果多次搜索都没有命中，说明该符号可能不在这个仓库里。"
        )
    return "\n".join(hits) + f"\n（共扫描 {scanned} 个文本文件）"


def read_file(root: Path, rel_path: str, start: int | None = None, end: int | None = None) -> str:
    """读取仓库内某个文件的行范围，返回带行号的内容。"""
    target = _resolve_in_repo(root, rel_path)
    if not target.exists():
        raise ToolError(f"文件不存在：{rel_path}")
    if target.is_dir():
        raise ToolError(f"{rel_path} 是目录，请用 list_directory")

    text = _read_text(target)
    all_lines = text.splitlines()
    total = len(all_lines)
    if total == 0:
        return f"{rel_path} 是空文件。"

    try:
        first = max(1, int(start)) if start is not None else 1
        last = int(end) if end is not None else first + DEFAULT_READ_LINES - 1
    except (TypeError, ValueError):
        raise ToolError("start/end 必须是整数行号") from None

    first = min(first, total)
    last = min(last, total)
    if last < first:
        raise ToolError(f"行范围不合法：start={first} > end={last}")
    if last - first + 1 > MAX_READ_LINES:
        last = first + MAX_READ_LINES - 1
        truncated = True
    else:
        truncated = False

    body = "\n".join(f"{i:>5}  {all_lines[i - 1]}" for i in range(first, last + 1))
    header = f"{rel_path}（第 {first}-{last} 行，共 {total} 行）"
    if truncated:
        header += f"，单次最多 {MAX_READ_LINES} 行，已截断"
    return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# 工具注册表（给模型看的 JSON Schema + 本地实现）
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        # handled_by：这个工具不是本地函数，而是由 Agent 循环自己解释的"控制信号"
        "handled_by": "agent_loop",
        "function": {
            "name": "finish",
            "description": (
                "结束探索并给出最终答案。当证据足够、或你判断再查也查不到时调用它。"
                "注意 finish 不是搜索工具：调用它即代表本轮问答结束。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": (
                            "给用户的最终答案（中文，先结论后依据）。"
                            "依据要写到 文件路径 + 行号或函数名；信息不足时明确说明缺少什么证据。"
                        ),
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：本次答案依据的文件路径列表，例如 ['backend/app/main.py']",
                    },
                },
                "required": ["answer"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出仓库内某个目录的文件与子目录。用于了解项目结构，例如 rel_path='backend/app'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {"type": "string", "description": "相对仓库根的目录路径，默认 '.'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "在仓库文本文件中按关键字搜索（不区分大小写），返回 文件:行号 与命中行。"
                "适合查找函数名、接口路径、配置项、错误信息等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要搜索的关键字或符号"},
                    "glob": {"type": "string", "description": "可选，按路径过滤，例如 '*service*.py'"},
                    "max_hits": {"type": "integer", "description": f"最多返回多少条命中（默认 {DEFAULT_SEARCH_HITS}）"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取仓库内某个文件的内容（带行号）。建议先用 search_code 定位，再用 start/end 只读相关片段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {"type": "string", "description": "相对仓库根的文件路径，例如 backend/app/main.py"},
                    "start": {"type": "integer", "description": "起始行号（从 1 开始），默认 1"},
                    "end": {"type": "integer", "description": f"结束行号，默认 start+{DEFAULT_READ_LINES - 1}"},
                },
                "required": ["rel_path"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# 工具注册表
#   * TOOL_SCHEMAS        → 发给模型看的声明（"模型可以调用哪些"）
#   * TOOL_IMPLEMENTATIONS→ 本地真正能执行的函数（"我们怎么执行"）
#   * CONTROL_TOOL_NAMES  → 有声明、但由 Agent 循环解释的控制信号（不在这里执行）
# 三者必须一致：test_agent_tools.py 里有一致性断言，避免"加了一边忘了另一边"。
# ---------------------------------------------------------------------------

DECLARED_TOOL_NAMES = tuple(
    schema["function"]["name"] for schema in TOOL_SCHEMAS if schema.get("type") == "function"
)
CONTROL_TOOL_NAMES = tuple(
    schema["function"]["name"] for schema in TOOL_SCHEMAS if schema.get("handled_by") == "agent_loop"
)

TOOL_IMPLEMENTATIONS = {
    "list_directory": list_directory,
    "search_code": search_code,
    "read_file": read_file,
}

# 能通过 call_tool 真正执行的工具（控制信号不在其中）
TOOL_NAMES = tuple(TOOL_IMPLEMENTATIONS)
ALL_TOOL_NAMES = DECLARED_TOOL_NAMES


def registry_issues() -> list[str]:
    """检查声明与实现是否一致；返回问题列表（空列表表示一致）。"""
    issues: list[str] = []
    for name in DECLARED_TOOL_NAMES:
        if name in CONTROL_TOOL_NAMES:
            continue
        if name not in TOOL_IMPLEMENTATIONS:
            issues.append(f"工具 {name!r} 已声明但没有实现，应在 schema 上标 handled_by 或补实现")
    for name in TOOL_IMPLEMENTATIONS:
        if name not in DECLARED_TOOL_NAMES:
            issues.append(f"工具 {name!r} 有实现但没有声明，模型看不到它")
    for name in CONTROL_TOOL_NAMES:
        if name in TOOL_IMPLEMENTATIONS:
            issues.append(f"工具 {name!r} 同时被标为控制工具和本地实现，会让人分不清谁来处理")
    return issues


_issues = registry_issues()
if _issues:                                     # 配置错误应当立刻暴露，而不是等到某次问答才发现
    raise RuntimeError("工具注册表不一致：" + "；".join(_issues))


def parse_finish_arguments(arguments: dict) -> tuple[str | None, list[str]]:
    """解析 finish 工具的参数，返回 (答案, 模型自报的依据文件)。

    模型偶尔会把参数写成字符串或漏字段，这里做容错：解析不出答案就返回 None，
    由 Agent 循环决定用 content 兜底还是继续。
    """
    if not isinstance(arguments, dict):
        return None, []
    answer = arguments.get("answer")
    answer = answer.strip() if isinstance(answer, str) else None
    raw_evidence = arguments.get("evidence")
    evidence: list[str] = []
    if isinstance(raw_evidence, list):
        evidence = [str(item).strip() for item in raw_evidence if str(item).strip()]
    elif isinstance(raw_evidence, str) and raw_evidence.strip():
        evidence = [raw_evidence.strip()]
    return (answer or None), evidence


def call_tool(root: Path, name: str, arguments: dict) -> str:
    """执行一个可执行的工具，把结果文本返回给模型；失败信息同样以文本返回。

    注意：控制类工具（如 finish）不在这里执行——它们由 Agent 循环解释为控制信号，
    因此会走到下面这条报错分支，报错信息里会说明这一点。
    """
    func = TOOL_IMPLEMENTATIONS.get(name)
    if func is None:
        if name in CONTROL_TOOL_NAMES:
            raise ToolError(f"{name} 是控制工具，由 Agent 循环处理，不能作为文件工具执行")
        raise ToolError(f"未知工具：{name}（可执行工具：{', '.join(TOOL_NAMES)}）")
    if not isinstance(arguments, dict):
        raise ToolError("工具参数必须是 JSON 对象")
    return func(root, **arguments)

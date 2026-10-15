import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from app.config import REPOS_DIR
from app.urls import GITHUB_URL_PATTERN

MAX_TREE_DEPTH = 10
MAX_TREE_ENTRIES_PER_DIR = 50
MAX_FILE_SIZE = 100_000

SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".jsx", ".tsx", ".vue"}

MAX_SOURCE_FILES = 5
MAX_SOURCE_FILE_SIZE = 5_000

IGNORED_DIRS = {
    ".git", "node_modules", "dist", "build", "target", "venv", ".venv",
    "__pycache__", ".next", ".nuxt", "coverage", ".cache", ".tox", "egg-info",
}

# 明确要跳过的文件（点文件不再一律跳过，这里只列噪音文件）
IGNORED_FILENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

README_NAMES = ["README.md", "README.rst", "README.txt", "README", "readme.md"]

# 只在仓库根目录查找的配置文件。
ROOT_CONFIG_FILENAMES = [
    "package.json", "requirements.txt", "pyproject.toml", "setup.py",
    "pom.xml", "build.gradle", "go.mod", "Cargo.toml",
    "Dockerfile", "docker-compose.yml", "Makefile",
]

# 允许在子目录中查找的配置文件名（monorepo 的清单文件通常不在根目录）。
NESTED_CONFIG_FILENAMES = [
    "package.json", "requirements.txt", "pyproject.toml", "Cargo.toml", "go.mod",
]

CONFIG_SEARCH_DEPTH = 3
MAX_CONFIG_FILES = 8


class RepoError(Exception):
    """仓库获取阶段的错误基类。"""


class InvalidRepoUrl(RepoError):
    """URL 不是受支持的 GitHub 仓库地址。"""


class CloneFailed(RepoError):
    """git clone 失败。"""


def normalize_repo_url(repo_url: str) -> str:
    url = repo_url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def generate_analysis_id(repo_url: str) -> str:
    normalized = normalize_repo_url(repo_url)
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def parse_github_url(repo_url: str) -> tuple[str, str]:
    m = GITHUB_URL_PATTERN.match(repo_url.strip())
    if not m:
        raise InvalidRepoUrl(f"仅支持 GitHub 仓库地址，例如 https://github.com/owner/repo（收到：{repo_url}）")
    return m.group("owner"), m.group("repo")


def clone_repo(repo_url: str, analysis_id: str) -> Path:
    owner, repo = parse_github_url(repo_url)
    dest = REPOS_DIR / analysis_id
    if dest.exists():
        return dest
    clone_url = f"https://github.com/{owner}/{repo}.git"
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(dest)],
            capture_output=True, text=True, timeout=120, check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise CloneFailed(f"克隆超时（120 秒）：{clone_url}") from e
    except subprocess.CalledProcessError as e:
        message = (e.stderr or "").strip() or f"git 退出码 {e.returncode}"
        raise CloneFailed(f"克隆失败：{message}") from e
    except FileNotFoundError as e:
        raise CloneFailed("未找到 git 命令，请先安装 git 并确保在 PATH 中。") from e
    return dest


def extract_readme(repo_path: Path) -> str | None:
    for name in README_NAMES:
        p = repo_path / name
        if p.is_file() and p.stat().st_size <= MAX_FILE_SIZE:
            return p.read_text(encoding="utf-8", errors="replace")
    return None


def iter_visible_entries(directory: Path) -> list[Path]:
    """排序后的可见条目：目录优先、名称不区分大小写；忽略项在截断之前就被剔除。

    这是全仓库唯一的"哪些文件算可见"的定义，目录树、配置抽取、源码抽样与
    Agent 工具（list_directory）都复用它，避免各处规则不一致。

    这里不再一刀切地跳过所有点开头的条目：`.gitignore`、`.env.example`、`.github/workflows/*`
    都是理解项目的重要线索。真正的噪音（.git、.venv、.DS_Store…）由 IGNORED_DIRS /
    IGNORED_FILENAMES 明确列出。
    """
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []
    return [
        entry for entry in entries
        if entry.name not in IGNORED_DIRS and entry.name not in IGNORED_FILENAMES
    ]


def build_directory_tree(repo_path: Path, depth: int = 0, max_depth: int = MAX_TREE_DEPTH) -> dict:
    if depth >= max_depth:
        return {}
    entries = iter_visible_entries(repo_path)[:MAX_TREE_ENTRIES_PER_DIR]
    tree: dict = {}
    for entry in entries:
        if entry.is_dir():
            tree[entry.name + "/"] = build_directory_tree(entry, depth + 1, max_depth)
        elif entry.is_file() and entry.stat().st_size <= MAX_FILE_SIZE:
            tree[entry.name] = None
    return tree


def format_tree(tree: dict, prefix: str = "") -> str:
    lines: list[str] = []
    items = list(tree.items())
    for i, (name, subtree) in enumerate(items):
        connector = "└── " if i == len(items) - 1 else "├── "
        lines.append(f"{prefix}{connector}{name}")
        if subtree is not None:
            extension = "    " if i == len(items) - 1 else "│   "
            lines.append(format_tree(subtree, prefix + extension))
    return "\n".join(lines)


def _looks_like_comment(raw_lines: list[str]) -> bool:
    meaningful = [line.strip() for line in raw_lines if line.strip()]
    if len(meaningful) < 2:
        return True
    marked = sum(1 for line in meaningful if line.startswith(("#", "//", "*", "/*", "-")))
    return marked >= len(meaningful) * 0.6


def _render_dependency_block(filename: str, text: str) -> str:
    """依赖清单可能很长，只保留前若干条依赖，避免把 LLM 上下文全部占满。"""
    limit = 800 if filename == "package.json" else 400
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    if not _looks_like_comment(lines):
        return text[:limit].rstrip() + "\n...（已截断）"
    kept = [line for line in lines if line.strip() and not line.strip().startswith(("#", "//"))]
    return "\n".join(kept[:40]) + "\n...（已截断）"


def extract_config_files(repo_path: Path) -> dict[str, str]:
    """收集配置：根目录常见清单文件，外加子目录里的包清单（monorepo 也能识别）。"""
    configs: dict[str, str] = {}

    def add(rel_path: str, path: Path) -> None:
        if len(configs) >= MAX_CONFIG_FILES or rel_path in configs:
            return
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_SIZE:
                return
        except OSError:
            return
        configs[rel_path] = _render_dependency_block(path.name, path.read_text(encoding="utf-8", errors="replace"))

    for name in ROOT_CONFIG_FILENAMES:
        add(name, repo_path / name)

    for directory in (
        d for d in _walk_directories(repo_path, CONFIG_SEARCH_DEPTH) if d != repo_path
    ):
        for name in NESTED_CONFIG_FILENAMES:
            rel = f"{directory.relative_to(repo_path).as_posix()}/{name}"
            add(rel, directory / name)
        if len(configs) >= MAX_CONFIG_FILES:
            break

    return configs


def _walk_directories(repo_path: Path, max_depth: int) -> list[Path]:
    """按目录优先、名称排序的稳定顺序遍历仓库目录（不含被忽略目录）。"""
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        found.append(directory)
        if depth >= max_depth:
            return
        for entry in iter_visible_entries(directory):
            if entry.is_dir():
                walk(entry, depth + 1)

    walk(repo_path, 0)
    return found


def iter_source_files(
    repo_path: Path,
    max_files: int = MAX_SOURCE_FILES,
    max_depth: int = 3,
    max_size: int = MAX_SOURCE_FILE_SIZE,
    suffixes: set[str] | None = None,
    predicate: Callable[[Path], bool] | None = None,
) -> list[tuple[str, str]]:
    """返回 [(仓库内相对路径, 文件内容)]，跳过被忽略目录、超大文件和过深的层级。

    三种选择方式，优先级从高到低：
      * predicate：自定义判定（例如"能当文本读"的完整判断，含内容嗅探）；
      * suffixes：按后缀集合筛选；
      * 都不传：按概览阶段的源码后缀（SOURCE_EXTENSIONS）筛选。
    """
    wanted = SOURCE_EXTENSIONS if suffixes is None else suffixes
    results: list[tuple[str, str]] = []

    def selected(entry: Path) -> bool:
        if predicate is not None:
            return predicate(entry)
        return entry.suffix in wanted

    def walk(directory: Path, depth: int) -> None:
        if len(results) >= max_files or depth > max_depth:
            return
        for entry in iter_visible_entries(directory):
            if len(results) >= max_files:
                return
            if entry.is_dir():
                walk(entry, depth + 1)
            elif selected(entry):
                try:
                    if entry.stat().st_size > max_size:
                        continue
                    content = entry.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                results.append((entry.relative_to(repo_path).as_posix(), content))

    walk(repo_path, 0)
    return results


def extract_repo_context(repo_url: str, analysis_id: str) -> dict:
    repo_path = clone_repo(repo_url, analysis_id)
    readme = extract_readme(repo_path)
    tree = build_directory_tree(repo_path)
    tree_text = format_tree(tree)
    configs = extract_config_files(repo_path)
    owner, repo = parse_github_url(repo_url)
    return {
        "analysis_id": analysis_id,
        "project_name": repo,
        "owner": owner,
        "repo_path": str(repo_path),
        "readme": readme,
        "directory_tree": tree_text,
        "config_files": configs,
    }

import hashlib
import re
import subprocess
from pathlib import Path

from app.config import REPOS_DIR

MAX_TREE_DEPTH = 10
MAX_TREE_ENTRIES_PER_DIR = 50
MAX_FILE_SIZE = 100_000

GITHUB_URL_PATTERN = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/?$"
)

IGNORED_DIRS = {
    ".git", "node_modules", "dist", "build", "target", "venv", ".venv",
    "__pycache__", ".next", ".nuxt", "coverage", ".cache", ".tox", "egg-info",
}

README_NAMES = ["README.md", "README.rst", "README.txt", "README", "readme.md"]

CONFIG_FILENAMES = [
    "package.json", "requirements.txt", "pyproject.toml", "setup.py",
    "pom.xml", "build.gradle", "go.mod", "Cargo.toml",
    "Dockerfile", "docker-compose.yml", "Makefile",
]


def generate_analysis_id(repo_url: str) -> str:
    return hashlib.md5(repo_url.encode()).hexdigest()[:12]


def parse_github_url(repo_url: str) -> tuple[str, str]:
    m = GITHUB_URL_PATTERN.match(repo_url.strip())
    if not m:
        raise ValueError(f"Invalid GitHub URL: {repo_url}")
    return m.group("owner"), m.group("repo")


def clone_repo(repo_url: str, analysis_id: str) -> Path:
    owner, repo = parse_github_url(repo_url)
    dest = REPOS_DIR / analysis_id
    if dest.exists():
        return dest
    clone_url = f"https://github.com/{owner}/{repo}.git"
    subprocess.run(
        ["git", "clone", "--depth", "1", clone_url, str(dest)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return dest


def extract_readme(repo_path: Path) -> str | None:
    for name in README_NAMES:
        p = repo_path / name
        if p.is_file() and p.stat().st_size <= MAX_FILE_SIZE:
            return p.read_text(encoding="utf-8", errors="replace")
    return None


def build_directory_tree(repo_path: Path, depth: int = 0, max_depth: int = MAX_TREE_DEPTH) -> dict:
    if depth >= max_depth:
        return {}
    entries = sorted(repo_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    if len(entries) > MAX_TREE_ENTRIES_PER_DIR:
        entries = entries[:MAX_TREE_ENTRIES_PER_DIR]
    tree: dict = {}
    for entry in entries:
        if entry.name in IGNORED_DIRS or entry.name.startswith("."):
            continue
        if entry.is_dir():
            subtree = build_directory_tree(entry, depth + 1, max_depth)
            tree[entry.name + "/"] = subtree
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


def extract_config_files(repo_path: Path) -> dict[str, str]:
    configs: dict[str, str] = {}
    for name in CONFIG_FILENAMES:
        p = repo_path / name
        if p.is_file() and p.stat().st_size <= MAX_FILE_SIZE:
            configs[name] = p.read_text(encoding="utf-8", errors="replace")
    return configs


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

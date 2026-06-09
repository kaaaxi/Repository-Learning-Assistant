import json
from pathlib import Path

from app.services import repo_service, storage_service

SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".jsx", ".tsx", ".vue"}
MAX_SOURCE_FILES = 5
MAX_SOURCE_SIZE = 5_000


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

    readme_preview = readme[:800].strip() if readme else "No README found."

    top_dirs = _extract_top_dirs(tree_text)
    code_map = [
        {"name": d, "description": f"{d}/ directory"}
        for d in top_dirs[:10]
    ]
    if not code_map:
        code_map = [{"name": project_name, "description": "Project root"}]

    key_modules = [
        {"name": d, "description": f"See {d}/ directory for details", "key_files": []}
        for d in top_dirs[:5]
    ]

    if "package.json" in configs:
        try:
            pkg = json.loads(configs["package.json"])
            scripts = pkg.get("scripts", {})
            if scripts:
                dev_workflow = "Common commands:\n"
                for cmd, val in scripts.items():
                    dev_workflow += f"  npm {cmd} — {val}\n"
            else:
                dev_workflow = "Node.js project. See package.json for scripts."
        except json.JSONDecodeError:
            dev_workflow = "Node.js project."
    elif "requirements.txt" in configs or "pyproject.toml" in configs:
        dev_workflow = "Python project. Typical: pip install -r requirements.txt"
    elif "go.mod" in configs:
        dev_workflow = "Go project. Typical: go build / go test ./..."
    elif "Cargo.toml" in configs:
        dev_workflow = "Rust project. Typical: cargo build / cargo test"
    else:
        dev_workflow = "See project configuration files for build instructions."

    return {
        "analysis_id": ctx["analysis_id"],
        "project_name": project_name,
        "repo_url": repo_url,
        "overview": readme_preview,
        "architecture_summary": f"Language: {language}. Top-level directories: {', '.join(top_dirs) or 'none'}. Directory tree:\n{tree_text}",
        "code_map": code_map,
        "key_modules": key_modules,
        "learning_path": [
            {"step": 1, "title": "Read README", "description": "Start with the project README to understand goals and setup."},
            {"step": 2, "title": "Review configuration", "description": f"Check {' '.join(configs.keys())} for dependencies and build setup." if configs else "Check project config files for dependencies."},
            {"step": 3, "title": "Explore top-level directories", "description": f"Browse: {', '.join(top_dirs[:5])} to understand code organization."},
            {"step": 4, "title": "Read sample source files", "description": "Look at representative source files to understand coding patterns."},
            {"step": 5, "title": "Try building and running", "description": "Follow the developer workflow to build and run the project locally."},
        ],
        "contribution_suggestions": [
            {"title": "Read CONTRIBUTING.md", "description": "Check for contribution guidelines in the repository.", "difficulty": "beginner"},
            {"title": "Fix a small bug or typo", "description": "Look for open issues labeled 'good first issue' or 'bug'.", "difficulty": "beginner"},
            {"title": "Add tests", "description": "Improve test coverage for existing modules.", "difficulty": "intermediate"},
            {"title": "Improve documentation", "description": "Enhance README or add docstrings to key modules.", "difficulty": "beginner"},
        ],
        "developer_workflow": dev_workflow,
        "coding_conventions": f"Language: {language}. Follow existing code style in the repository.",
    }


def analyze(repo_url: str) -> dict:
    analysis_id = repo_service.generate_analysis_id(repo_url)

    try:
        existing = storage_service.load_analysis(analysis_id)
        return existing
    except FileNotFoundError:
        pass

    ctx = repo_service.extract_repo_context(repo_url, analysis_id)

    repo_path = Path(ctx["repo_path"])
    sample_sources = _extract_sample_sources(repo_path, ctx.get("directory_tree", ""))

    result = build_fallback_result(ctx, repo_url, sample_sources)
    storage_service.save_analysis(analysis_id, result)
    return result

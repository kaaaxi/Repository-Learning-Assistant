"""真实栈集成测试：真实 fastapi/pydantic + 真实规则/LLM 分析 + 真实磁盘读写。

与 test_logic.py 的区别：本文件不做任何库替身，直接用项目依赖跑完整链路，
需要在已装好 backend/requirements.txt 的环境里运行：

    cd backend
    python tests/test_e2e.py

说明：
  * 需要一个可写的运行时目录。默认用系统临时目录，不可写时回退到仓库根的 .tmp-tests
    （便于在受限沙箱里运行），并把 data/repos、data/analyses 指过去；
  * 密钥文件仍读真正的 backend/data/secrets.json，所以有没有配 Key 都能跑：
    配了 Key 会真的调用 LLM，没配则自动走规则分析，两种情况都做断言；
  * 沙箱禁网/禁 git clone 时，本脚本不会失败——它只按 clone_repo 的目录约定预置仓库副本。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

# 运行目录：优先系统临时目录，不可写则回退到仓库根（受限沙箱下只有它能写）
def _pick_workdir() -> Path:
    for base in (Path(os.environ.get("TEMP", "/tmp")), REPO_ROOT):
        try:
            candidate = base / f"repoassistant-e2e-{os.getpid()}"
            candidate.mkdir(parents=True, exist_ok=True)
            (candidate / ".probe").write_text("x", encoding="utf-8")
            (candidate / ".probe").unlink()
            return candidate
        except OSError:
            continue
    raise SystemExit("找不到可写的临时目录")


WORKDIR = _pick_workdir()
REPOS_DIR = WORKDIR / "repos"
ANALYSES_DIR = WORKDIR / "analyses"
REPOS_DIR.mkdir(parents=True, exist_ok=True)
ANALYSES_DIR.mkdir(parents=True, exist_ok=True)

# 通过配置项把运行时目录整体指到可写位置（应用代码自己读的就是这两个值），
# 密钥文件路径保持默认（真实文件）
os.environ["REPOASSISTANT_DATA_DIR"] = str(WORKDIR)
os.environ.setdefault("REPOASSISTANT_SECRETS_FILE", str(BACKEND_DIR / "data" / "secrets.json"))

from app import config  # noqa: E402
from app.services import analysis_service as asvc  # noqa: E402
from app.services import repo_service, storage_service  # noqa: E402

repo_service.REPOS_DIR = REPOS_DIR
storage_service.ANALYSES_DIR = ANALYSES_DIR

from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   <- {detail}" if detail and not ok else ""))


def build_sample_repo(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    (root / "backend" / "app" / "services").mkdir(parents=True)
    (root / "frontend" / "src" / "components").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "docs").mkdir()
    (root / "backend" / "app" / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (root / "backend" / "app" / "services" / "worker.py").write_text("def work():\n    return 2\n", encoding="utf-8")
    (root / "backend" / "requirements.txt").write_text("fastapi==0.115.12\nuvicorn==0.34.2\n", encoding="utf-8")
    (root / "frontend" / "package.json").write_text(
        json.dumps({"name": "web", "scripts": {"dev": "vite", "build": "vue-tsc -b && vite build"}}),
        encoding="utf-8")
    (root / "frontend" / "src" / "App.vue").write_text("<template><div/></template>\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    (root / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (root / "README.md").write_text(
        "# Sample Repo\n\n这是一个用于集成测试的示例仓库。\n\n[文档](https://en.wikipedia.org/wiki/Foo_(bar)) 见说明。\n",
        encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("junk\n", encoding="utf-8")
    (root / "small.py").write_text("x = 1\n", encoding="utf-8")
    (root / "big.py").write_text("x = 1\n" * 20000, encoding="utf-8")   # 140KB，超过 100KB 阈值
    return root


def main() -> int:
    repo_url = "https://github.com/owner/sample-repo"
    analysis_id = repo_service.generate_analysis_id(repo_url) + time.strftime("%H%M%S")
    src = build_sample_repo(WORKDIR / "sample-repo")

    print("=== 1. 按 clone_repo 的目录约定准备仓库（沙箱禁网时的替代）===")
    real_clone = repo_service.clone_repo

    def prepared_clone(url: str, aid: str) -> Path:
        """幂等：目录已存在就复用（与真实 clone_repo 的『存在即复用』语义一致）。"""
        dest = repo_service.REPOS_DIR / aid
        if not dest.exists():
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
        return dest

    # 整个测试期间保持补丁：/api/analyze 内部也会调用 clone_repo
    repo_service.clone_repo = prepared_clone
    try:
        ctx = repo_service.extract_repo_context(repo_url, analysis_id)
    finally:
        pass
    cloned = Path(ctx["repo_path"])
    rec("仓库目录就位并被 extract_repo_context 采用",
        (cloned / "README.md").exists() and ctx["project_name"] == "sample-repo", str(cloned))

    print("=== 2. 目录树与顶层目录识别 ===")
    dirs, files = asvc.extract_top_dirs(ctx["directory_tree"])
    rec("只识别顶层目录", dirs == ["backend", "docs", "frontend", "scripts"], f"实际 {dirs}")
    rec("识别根目录文件", files == ["README.md", "small.py"], f"实际 {files}")
    rec("node_modules 不出现在目录树", "node_modules" not in ctx["directory_tree"], "")
    rec("超过 100KB 的文件被排除",
        "big.py" not in ctx["directory_tree"] and "big.py" not in files
        and (cloned / "big.py").stat().st_size > repo_service.MAX_FILE_SIZE, "")

    print("=== 3. 配置识别范围 ===")
    cfg_names = sorted(ctx["config_files"])
    rec("子目录 backend/requirements.txt 被识别", "backend/requirements.txt" in cfg_names, str(cfg_names))
    rec("子目录 frontend/package.json 被识别", "frontend/package.json" in cfg_names, str(cfg_names))

    print("=== 4. 源码抽样与关键文件 ===")
    names = [s.splitlines()[0].strip("- ").strip() for s in asvc._source_snippets(cloned)]
    rec("抽样路径都是仓库内真实存在的相对路径",
        bool(names) and all((cloned / n).exists() for n in names)
        and any("/" in n for n in names) and "small.py" in names, str(names))
    key_files = asvc._collect_key_files_by_dir(cloned, dirs)
    rec("关键模块路径真实且完整",
        key_files.get("backend") == ["app/main.py", "app/services/worker.py"], str(key_files))

    print("=== 5. README 清洗 ===")
    overview = asvc.clean_readme_overview(ctx["readme"])
    rec("带括号的链接被完整替换", "文档" in overview and ")" not in overview, repr(overview))

    print("=== 5b. LLM 输出被截断时的修复链路（走真实接口）===")
    from app.services import llm_service
    client = TestClient(app_main.app)
    truncated = (
        '{"overview": "被截断的项目概述。",'
        ' "architecture_summary": "主要语言：Python。",'
        ' "code_map": [{"name": "backend", "description": "后端目录"}],'
        ' "key_modules": [{"name": "backend", "description": "主入口", "key_files": ["backend/app/main.py"]}],'
        ' "learning_path": [{"step": 1, "title": "看 README", "description": "先读说明"}],'
        ' "contribution_suggestions": [{"title": "补测试", "description": "加用例", "difficulty": "beginner"}],'
        ' "developer_workflow": "1. 安装依赖。",'
        ' "coding_conventions": "遵循 PEP8，覆盖'          # 故意断在字符串中间
    )
    repaired = llm_service._repair_truncated_json(truncated)
    rec("截断的 JSON 能被修复成完整对象",
        isinstance(repaired, dict) and repaired.get("coding_conventions") == "遵循 PEP8，覆盖",
        repr(repaired))
    real_analyze_with_llm = llm_service.analyze_with_llm
    llm_service.analyze_with_llm = lambda _ctx: (repaired, None)
    try:
        repaired_resp = client.post("/api/analyze", json={"repo_url": repo_url, "force_refresh": True})
    finally:
        llm_service.analyze_with_llm = real_analyze_with_llm
    repaired_data = repaired_resp.json() if repaired_resp.status_code == 200 else {}
    rec("修复后的结果走真实接口仍为 llm 模式且无错误",
        repaired_resp.status_code == 200 and repaired_data.get("analysis_mode") == "llm"
        and repaired_data.get("llm_error") is None and repaired_data.get("overview") == "被截断的项目概述。",
        f"{repaired_resp.status_code} mode={repaired_data.get('analysis_mode')} err={repaired_data.get('llm_error')}")
    # 之后的分析从真实 LLM 重新开始
    data = client.post("/api/analyze", json={"repo_url": repo_url, "force_refresh": True}).json()
    mode = data.get("analysis_mode")

    print("=== 6. HTTP 接口 + 分析 + 落盘 ===")
    resp = client.post("/api/analyze", json={"repo_url": repo_url})
    rec("POST /api/analyze 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:200]}")
    data = resp.json() if resp.status_code == 200 else data
    mode = data.get("analysis_mode")
    print(f"      analysis_mode={mode} llm_error={str(data.get('llm_error'))[:90]}")
    rec("分析模式合法", mode in ("llm", "demo"), str(mode))
    rec("LLM 失败时给出可见原因", mode == "llm" or bool(data.get("llm_error")),
        f"mode={mode} err={data.get('llm_error')}")
    code_map_names = [i["name"] for i in data.get("code_map", [])]
    rec("顶层目录全部出现在 code_map 中",
        all(d in code_map_names for d in ["backend", "docs", "frontend", "scripts"]), str(code_map_names))
    rec("code_map 名字都来自真实目录树",
        all((cloned / n).exists() or n in ctx["directory_tree"] for n in code_map_names), str(code_map_names))
    rec("关键模块带真实文件路径",
        any("/" in f for m in data.get("key_modules", []) for f in m.get("key_files", [])),
        str([m.get("key_files") for m in data.get("key_modules", [])]))
    rec("结果带 analyzed_at", bool(data.get("analyzed_at")), str(data.get("analyzed_at")))
    saved = ANALYSES_DIR / f"{data.get('analysis_id')}.json"
    rec("结果已落盘为 JSON",
        saved.exists() and json.loads(saved.read_text(encoding="utf-8"))["project_name"] == "sample-repo", str(saved))

    print("=== 7. 缓存与 force_refresh ===")
    again = client.post("/api/analyze", json={"repo_url": repo_url}).json()
    if mode == "llm":
        rec("LLM 结果在 TTL 内命中缓存", again.get("analyzed_at") == data.get("analyzed_at"),
            f"{again.get('analyzed_at')} vs {data.get('analyzed_at')}")
    else:
        rec("有密钥时不复用规则结果", again.get("analyzed_at") != data.get("analyzed_at"),
            f"时间戳相同 = {again.get('analyzed_at')}")
    forced = client.post("/api/analyze", json={"repo_url": repo_url, "force_refresh": True})
    rec("force_refresh=true 仍返回可用结果",
        forced.status_code == 200 and forced.json().get("analysis_mode") in ("llm", "demo"),
        f"{forced.status_code} {forced.json().get('analysis_mode')}")
    old_ttl = asvc.ANALYSIS_CACHE_TTL
    asvc.ANALYSIS_CACHE_TTL = 1
    time.sleep(1.1)
    rec("TTL 过期判定生效", asvc._cache_is_fresh({"analyzed_at": data.get("analyzed_at")}) is False, "")
    asvc.ANALYSIS_CACHE_TTL = old_ttl

    print("=== 8. URL 归一 ===")
    ids = {repo_service.generate_analysis_id(u) for u in
           ("https://github.com/o/r", "https://github.com/o/r/", "https://github.com/o/r.git")}
    rec("三种写法同一 analysis_id", len(ids) == 1, str(ids))

    print("=== 9. 问答 ===")
    chat_resp = client.post("/api/chat", json={"analysis_id": data.get("analysis_id"), "question": "这个项目是做什么的？"})
    chat = chat_resp.json() if chat_resp.status_code == 200 else {}
    rec("POST /api/chat 200", chat_resp.status_code == 200, f"{chat_resp.status_code} {chat_resp.text[:150]}")
    rec("问答带 source 字段", chat.get("source") in ("llm", "fallback"), str(chat.get("source")))
    answer = chat.get("answer") or ""
    if chat.get("source") == "fallback" and chat.get("llm_error"):
        rec("回退回答里带上失败原因", "LLM 调用失败" in answer, answer[-90:])
    else:
        rec("回答非空且不含失败提示", bool(answer.strip()) and "LLM 调用失败" not in answer, answer[:60])
    rec("空问题被拒",
        client.post("/api/chat", json={"analysis_id": data.get("analysis_id"), "question": ""}).status_code in (400, 422), "")

    print("=== 10. 健康检查 ===")
    health = client.get("/api/health").json()
    print("      health =", json.dumps(health, ensure_ascii=False))
    rec("health 不含密钥内容", "sk-" not in json.dumps(health), "")
    rec("密钥来源字段合法", health.get("api_key_source") in ("env", "file", "none"), str(health.get("api_key_source")))

    print("=== 11. 路径穿越与 URL 校验 ===")
    rec("非法 analysis_id 不读任意文件", client.get("/api/analysis/..%2F..%2Fsecrets").status_code in (400, 404), "")
    rec("合法 id 可读回", client.get(f"/api/analysis/{data.get('analysis_id')}").status_code == 200, "")
    try:
        repo_service.parse_github_url("https://gitlab.com/a/b")
        rec("非 GitHub 地址抛 InvalidRepoUrl", False, "没有报错")
    except repo_service.InvalidRepoUrl:
        rec("非 GitHub 地址抛 InvalidRepoUrl", True)
    rec("非 GitHub 地址在接口层被拒",
        client.post("/api/analyze", json={"repo_url": "https://example.com/x"}).status_code in (400, 422), "")

    print("=== 12. 克隆失败提示（真实 git，地址不存在）===")
    repo_service.clone_repo = real_clone          # 还原真实克隆，才能拿到真实的 git 失败信息
    try:
        repo_service.clone_repo("https://github.com/owner/definitely-not-a-real-repo-xyz", "deadbeefcafe")
        rec("克隆不存在的仓库抛 CloneFailed", False, "没有报错")
    except repo_service.CloneFailed as e:
        rec("克隆不存在的仓库抛 CloneFailed", True)
        print("      提示文案:", str(e)[:90].replace("\n", " "))

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print(f"集成测试：通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    for name in failed:
        print("  FAIL:", name)
    shutil.rmtree(WORKDIR, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

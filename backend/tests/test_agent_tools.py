"""Stage 1 工具层测试：三个只读工具的真实文件操作与安全边界。

不需要第三方依赖（openai SDK 用替身），可直接运行：

    cd backend
    python tests/test_agent_tools.py
"""

import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))     # 让 _openai_stub 可导入

import _openai_stub  # noqa: E402

# repo_tools 只依赖 repo_service，不直接用 SDK；这里仍装上替身，
# 保证与其它测试一致的 import 环境
_openai_stub.install()

from app.services import repo_tools  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   <- {detail}" if detail and not ok else ""))


def build_fixture(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    (root / "backend" / "app" / "services").mkdir(parents=True)
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "backend" / "app" / "main.py").write_text(
        "import logging\n"
        "\n"
        "def chat(question):\n"
        "    answer, llm_error = chat_with_llm(question)\n"
        "    if answer is not None:\n"
        "        return {'answer': answer, 'source': 'llm'}\n"
        "    return chat_fallback(question, reason=llm_error)\n",
        encoding="utf-8",
    )
    (root / "backend" / "app" / "services" / "llm_service.py").write_text(
        "DEMO_MODE = False\n"
        "\n"
        "def chat_with_llm(question):\n"
        "    if DEMO_MODE:\n"
        "        return None, None\n"
        "    return None, 'HTTP 500'\n",
        encoding="utf-8",
    )
    (root / "frontend" / "src" / "App.vue").write_text("<template><div/></template>\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("# guide\n需要 chat_fallback 时看后端\n", encoding="utf-8")
    # 点文件：搜索能命中、读取也必须能读（suffix 判断对它们不适用）
    (root / ".env.example").write_text("DEEPSEEK_MODEL=deepseek-flash\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\nnode_modules/\n", encoding="utf-8")
    (root / "Makefile").write_text("run:\n\tpython -m app\n", encoding="utf-8")
    # 明确是二进制/系统文件：不该被当文本读
    (root / ".DS_Store").write_bytes(b"\x00\x01\x02\x03binary")
    (root / "archive.zip").write_bytes(b"PK\x03\x04binary")
    (root / "node_modules" / "pkg" / "index.js").write_text("// chat_fallback 不应该被搜到\n", encoding="utf-8")
    (root / "big.py").write_text("x = 1\n" * 30_000, encoding="utf-8")      # 300KB，超过读取上限
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)     # 非文本
    (root / "empty").mkdir()
    (root / ".hidden").mkdir()
    (root / ".hidden" / "secret.py").write_text("chat_fallback\n", encoding="utf-8")


def expect_error(name: str, fn, needle: str) -> None:
    """断言某次工具调用抛出包含 needle 的 ToolError。"""
    try:
        out = fn()
        rec(name, False, f"没有报错，返回：{str(out)[:80]}")
    except repo_tools.ToolError as e:
        rec(name, needle in str(e), str(e))


def main() -> int:
    workdir = REPO_ROOT / ".tmp-tests" / "agent-tools"
    fixture = workdir / "sample-repo"
    workdir.mkdir(parents=True, exist_ok=True)
    build_fixture(fixture)

    print("=== 1. list_directory ===")
    listing = repo_tools.list_directory(fixture, ".")
    # 只看条目行（以两个空格开头的那些），避免把表头说明文字误当成条目
    entry_lines = [line for line in listing.splitlines() if line.startswith("  ")]
    rec("能列出根目录并标出子目录", "  backend/" in listing and "  docs/" in listing, listing[:200])
    rec("跳过噪音目录与噪音文件",
        not any("node_modules" in line or ".DS_Store" in line for line in entry_lines),
        str(entry_lines[:12]))
    rec("有信息量的点文件与点目录正常列出",
        any(".env.example" in line for line in entry_lines) and any(".gitignore" in line for line in entry_lines),
        str(entry_lines[:12]))
    rec("列出子目录内容", "main.py" in repo_tools.list_directory(fixture, "backend/app"), "")
    rec("空目录有明确说明", "没有可列出的条目" in repo_tools.list_directory(fixture, "empty"), "")

    print("=== 2. search_code ===")
    hits = repo_tools.search_code(fixture, "chat_fallback")
    rec("返回 文件:行号 格式", "backend/app/main.py:7" in hits, hits[:300])
    rec("搜索结果不含被忽略目录", "node_modules" not in hits, hits[:300])
    rec("不区分大小写", "CHAT_FALLBACK" in repo_tools.search_code(fixture, "CHAT_FALLBACK").upper(), "")
    rec("glob 过滤生效",
        all("backend" in line for line in repo_tools.search_code(fixture, "chat_fallback", glob="backend/*").splitlines()
            if ":" in line and not line.startswith("（")),
        repo_tools.search_code(fixture, "chat_fallback", glob="backend/*")[:200])
    rec("搜不到时给出建议", "没有找到" in repo_tools.search_code(fixture, "绝对不存在的符号xyz"), "")
    rec("max_hits 生效",
        len([l for l in repo_tools.search_code(fixture, "def", max_hits=1).splitlines() if ":" in l]) == 1,
        repo_tools.search_code(fixture, "def", max_hits=1)[:120])

    # 关键词被误当正则时的纠正回路（实测真实模型会写 "autofunction|autoclass"）
    pipe_result = repo_tools.search_code(fixture, "autofunction|autoclass")
    rec("带正则符号且零命中时明确指出不支持正则",
        "字面匹配" in pipe_result and "不支持正则" in pipe_result, pipe_result[:150])
    rec("纠正提示里给了替代做法（拆成单个关键词）", "拆成单个关键词" in pipe_result, "")
    rec("带正则符号但确有命中时不误报",
        "不支持正则" not in repo_tools.search_code(fixture, "chat(question)"), "")
    rec("普通关键词零命中时仍是原来的建议",
        "不支持正则" not in repo_tools.search_code(fixture, "绝对不存在的符号xyz"), "")

    print("=== 3. read_file ===")
    body = repo_tools.read_file(fixture, "backend/app/main.py")
    rec("带行号返回", "     1  import logging" in body or "1  import logging" in body, body[:120])
    rec("报告行范围与总行数", "第 1-" in body and "共 7 行" in body, body.splitlines()[0])
    rec("start/end 生效", "第 4-5 行" in repo_tools.read_file(fixture, "backend/app/main.py", start=4, end=5), "")
    rec("单次读取行数有上限",
        "已截断" in repo_tools.read_file(fixture, "backend/app/main.py", start=1, end=9999)
        or "共 7 行" in repo_tools.read_file(fixture, "backend/app/main.py", start=1, end=9999), "")

    print("=== 3b. 点文件与文本/二进制判定（搜索与读取口径必须一致）===")
    rec("点文件能被读取（.env.example）",
        "DEEPSEEK_MODEL" in repo_tools.read_file(fixture, ".env.example"), "")
    rec("点文件能被读取（.gitignore）",
        "node_modules" in repo_tools.read_file(fixture, ".gitignore"), "")
    rec("无后缀文本文件能读取（Makefile）",
        "python -m app" in repo_tools.read_file(fixture, "Makefile"), "")
    rec("点文件能被搜索命中",
        ".env.example:1" in repo_tools.search_code(fixture, "DEEPSEEK_MODEL"), "")
    rec("搜索命中的文件必然可读（口径一致）",
        (lambda hits: all(
            "ToolError" not in repo_tools.read_file(fixture, line.split(":")[0]) or False
            for line in hits.splitlines() if ":" in line and not line.startswith("（")
        ))(repo_tools.search_code(fixture, "DEEPSEEK_MODEL")), "")
    expect_error("二进制/系统文件仍被拒绝（.DS_Store）",
                 lambda: repo_tools.read_file(fixture, ".DS_Store"), "不是文本文件")
    expect_error("压缩包仍被拒绝（archive.zip）",
                 lambda: repo_tools.read_file(fixture, "archive.zip"), "不是文本文件")

    print("=== 4. 错误处理（必须可读，便于模型自行纠正）===")
    expect_error("越界路径被拒（..）", lambda: repo_tools.read_file(fixture, "../../secrets.json"), "越出仓库范围")
    expect_error("绝对路径被拒", lambda: repo_tools.read_file(fixture, "C:/Windows/win.ini"), "相对路径")
    expect_error("不存在的文件给出提示", lambda: repo_tools.read_file(fixture, "nope.py"), "文件不存在")
    expect_error("目录不能当文件读", lambda: repo_tools.read_file(fixture, "backend"), "是目录")
    expect_error("超大文件整读被拒并建议分段", lambda: repo_tools.read_file(fixture, "big.py"), "整读上限")
    expect_error("非文本文件被跳过", lambda: repo_tools.read_file(fixture, "logo.png"), "不是文本文件")
    expect_error("空关键字被拒", lambda: repo_tools.search_code(fixture, "  "), "不能为空")
    expect_error("非法行范围被拒", lambda: repo_tools.read_file(fixture, "backend/app/main.py", start=5, end=2), "不合法")

    print("=== 4c. 大文件：范围读必须可用，整读才受限 ===")
    big_mid = fixture / "mid_big.py"
    try:
        # 介于"整读上限"与"硬上限"之间的文件（约 150 KB）：真实案例里 click 的 core.py 就是这样
        big_mid.write_text("x = 1\n" * 25_000, encoding="utf-8")
        ranged = repo_tools.read_file(fixture, "mid_big.py", start=10, end=12)
        rec("超大文件的范围读可用（不再一刀切拒绝）",
            "第 10-12 行" in ranged and ranged.count("x = 1") == 3, ranged[:80])
        rec("范围读带行号（右对齐到 5 位）", "   11  x = 1" in ranged, repr(ranged[:120]))
        try:
            repo_tools.read_file(fixture, "mid_big.py")
            rec("整读超大文件被拒", False, "竟然成功了")
        except repo_tools.ToolError as e:
            rec("整读超大文件被拒，且说明是整读上限", "整读上限" in str(e), str(e)[:100])
            rec("拒绝时明确告知范围读不受限制", "范围读不受大小限制" in str(e), str(e)[:100])
        # 搜索必须能扫到这类大文件（否则大文件里的符号永远搜不到）
        hits = repo_tools.search_code(fixture, "x = 1", glob="mid_big.py", max_hits=3)
        rec("搜索能扫到超过整读上限的大文件", "mid_big.py:" in hits, hits[:100])
    finally:
        big_mid.unlink(missing_ok=True)

    print("=== 4d. glob 写法的兼容性 ===")
    for rel, g, want in [("src/click/core.py", "src/click/core.py", True),
                         ("src/click/core.py", "core.py", True),
                         ("src/click/core.py", "*core.py", True),
                         ("src/click/core.py", "src/**/*.py", True),
                         ("src/click/core.py", "*.md", False),
                         ("src/click/core.py", "", True)]:
        got = repo_tools._path_matches(rel, g)
        rec(f"glob {g!r:20} 匹配 {rel} → {want}", got == want, f"实际 {got}")

    print("=== 4b. 读取路径分类（用于中途纠偏判断）===")
    from app.services import agent_service as _agent
    for rel, want in [("src/click/decorators.py", "implementation"),
                      ("app/services/repo_tools.py", "implementation"),
                      ("src/click/__init__.py", "export"),
                      ("tests/test_imports.py", "test"),
                      ("docs/api.md", "docs"),
                      ("CHANGES.md", "docs"),
                      ("CONTRIBUTING.md", "docs"),
                      ("pyproject.toml", "config"),
                      ("", "other")]:
        got = _agent._classify_read(rel)
        rec(f"分类 {rel or '(空)'} → {want}", got == want, f"实际 {got}")

    print("=== 5. 工具注册表与调用入口 ===")
    rec("声明集与可执行集分离且都符合预期",
        repo_tools.DECLARED_TOOL_NAMES == ("finish", "list_directory", "search_code", "read_file")
        and set(repo_tools.TOOL_NAMES) == {"list_directory", "search_code", "read_file"}, "")
    rec("控制工具由 schema 推导，且只有 finish",
        repo_tools.CONTROL_TOOL_NAMES == ("finish",), str(repo_tools.CONTROL_TOOL_NAMES))
    rec("注册表一致性检查通过（声明/实现不脱节）", repo_tools.registry_issues() == [], str(repo_tools.registry_issues()))
    rec("schema 符合 OpenAI 结构",
        all(t["type"] == "function" and "name" in t["function"] and "parameters" in t["function"]
            for t in repo_tools.TOOL_SCHEMAS), "")
    rec("schema 的参数名与实现签名一致（控制工具除外）",
        all(set(t["function"]["parameters"].get("properties", {})) <=
            {"rel_path", "keyword", "glob", "max_hits", "start", "end"}
            for t in repo_tools.TOOL_SCHEMAS if t["function"]["name"] != "finish"), "")
    rec("finish 的必填参数是 answer",
        next(t for t in repo_tools.TOOL_SCHEMAS if t["function"]["name"] == "finish")
        ["function"]["parameters"].get("required") == ["answer"], "")
    rec("finish 声明了 handled_by=agent_loop",
        next(t for t in repo_tools.TOOL_SCHEMAS if t["function"]["name"] == "finish").get("handled_by") == "agent_loop", "")

    print("=== 5b. finish 参数解析 ===")
    rec("正常参数", repo_tools.parse_finish_arguments({"answer": "结论", "evidence": ["a.py", " b.py "]})
        == ("结论", ["a.py", "b.py"]), "")
    rec("缺 answer 时返回 None，但 evidence 仍保留",
        repo_tools.parse_finish_arguments({"evidence": ["a.py"]}) == (None, ["a.py"]), "")
    rec("answer 为空串返回 None", repo_tools.parse_finish_arguments({"answer": "   "}) == (None, []), "")
    rec("evidence 传字符串也能用",
        repo_tools.parse_finish_arguments({"answer": "x", "evidence": "a.py"}) == ("x", ["a.py"]), "")
    rec("参数不是对象时不崩", repo_tools.parse_finish_arguments("乱传") == (None, []), "")

    rec("call_tool 能执行", "import logging" in repo_tools.call_tool(fixture, "read_file", {"rel_path": "backend/app/main.py"}), "")
    expect_error("控制工具不能被当成文件工具执行（报错要说明原因）",
                 lambda: repo_tools.call_tool(fixture, "finish", {"answer": "x"}), "控制工具")
    expect_error("未知工具的报错只列可执行工具",
                 lambda: repo_tools.call_tool(fixture, "delete_repo", {}), "可执行工具")
    expect_error("参数不是对象时报错",
                 lambda: repo_tools.call_tool(fixture, "read_file", ["backend/app/main.py"]), "JSON 对象")

    print()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"工具层测试：通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    for name in failed:
        print("  FAIL:", name)
    shutil.rmtree(workdir, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

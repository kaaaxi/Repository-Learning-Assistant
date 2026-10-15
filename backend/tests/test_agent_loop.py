"""Stage 1 Agent 循环测试：用假模型驱动真实工具，验证循环的每一步行为。

覆盖：多轮探索、工具结果回喂、最终回答、步数上限、超时、重复调用、参数非法、
      工具失败不中断、路径越界、证据清单、finish 结束工具、模型不可用时的降级。

不需要第三方依赖（openai SDK 用替身），可直接运行：

    cd backend
    python tests/test_agent_loop.py
"""

import json
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))     # 让 _openai_stub 可导入

import _openai_stub  # noqa: E402

_openai_stub.install()          # 必须在 import app.services.llm_service 之前

from app.services import agent_service, llm_service  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   <- {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------------------
# 假模型：按对话历史里的信息决定下一步（模拟真实 Agent 的"边看边决定"）
# ---------------------------------------------------------------------------

def _tool_messages(messages: list[dict]) -> list[str]:
    return [m.get("content", "") for m in messages if m.get("role") == "tool"]


def make_scripted_llm(record: dict):
    """返回一个 request_chat_completion 替身，行为等价于"模型看了工具结果再决定下一步"。"""

    def fake(messages, tools=None, temperature=0.3):
        record.setdefault("messages", []).append(json.loads(json.dumps(messages)))
        record["calls"] = record.get("calls", 0) + 1

        # 收尾提问不带工具：此时模型只能给文本结论
        if not tools:
            return _final_answer("收尾：根据已读到的内容给出当前能确认的结论。"), None

        tool_outputs = _tool_messages(messages)
        joined = "\n".join(tool_outputs)
        # 第 1 步：先搜索定位
        if not tool_outputs:
            return _tool_call("search_code", {"keyword": "chat_fallback"}), None

        # 第 2 步：搜索命中了 main.py:7 → 去读它
        if "main.py:7" in joined and "def chat(question)" not in joined:
            return _tool_call("read_file", {"rel_path": "backend/app/main.py", "start": 1, "end": 20}), None

        # 第 3 步：读到 main.py 里调用了 chat_with_llm → 追过去读
        if "def chat(question)" in joined and "chat_with_llm(question)" in joined and "DEMO_MODE = False" not in joined:
            return _tool_call("read_file", {"rel_path": "backend/app/services/llm_service.py"}), None

        # 第 4 步：证据够了，直接回答（不再调用工具）
        return _final_answer(
            "LLM 调用失败时，/api/chat 不会报错：main.py 里先取 answer, llm_error，"
            "answer 为 None 就调用 chat_fallback，并把失败原因作为 reason 传下去。"
            "llm_service.py 中 Demo 模式返回 (None, None)，请求失败返回 (None, 错误原因)。"
        ), None

    return fake


def _tool_call(name: str, arguments: dict, call_id: str = "call_1"):
    return {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                }],
            },
        }]
    }


def _final_answer(text: str):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}


def _always_tool(name: str, arguments: dict, call_id: str = "call_x"):
    """模型每轮都请求同一个工具调用。

    注意：收尾提问（tools=None）时模型没有工具可用，必须给出文本回答，
    否则就不是一个可信的替身。
    """

    def fake(messages, tools=None, temperature=0.3):
        if not tools:
            return _final_answer("根据目前掌握的信息：证据不足，先给已确认的部分结论。"), None
        return _tool_call(name, arguments, call_id), None

    return fake


def _varying_tool(name: str, template: str):
    """每轮请求一个**参数不同**的工具调用，用来测试"步数用尽"（而不是被判定为重复）。"""
    state = {"n": 0}

    def fake(messages, tools=None, temperature=0.3):
        if not tools:
            return _final_answer("达到步数上限，以下是已确认的部分。"), None
        state["n"] += 1
        return _tool_call(name, {"rel_path": template.format(n=state["n"])}, f"c{state['n']}"), None

    return fake


def build_fixture(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    (root / "backend" / "app" / "services").mkdir(parents=True)
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


ANALYSIS = {
    "project_name": "sample",
    "repo_url": "https://github.com/owner/sample",
    "overview": "示例仓库",
    "directory_tree": "├── backend/\n│   └── app/\n└── README.md",
}


def main() -> int:
    workdir = REPO_ROOT / ".tmp-tests" / "agent-loop"
    fixture = workdir / "sample-repo"
    workdir.mkdir(parents=True, exist_ok=True)
    build_fixture(fixture)

    real_request = llm_service.request_chat_completion
    real_demo = agent_service.DEMO_MODE
    real_steps = agent_service.AGENT_MAX_STEPS
    real_timeout = agent_service.AGENT_TIMEOUT_SECONDS
    agent_service.DEMO_MODE = False

    try:
        print("=== 1. 多轮探索链路（搜索 → 读入口 → 追调用 → 读实现 → 作答）===")
        record: dict = {}
        llm_service.request_chat_completion = make_scripted_llm(record)
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "模型调用失败时，仓库问答接口怎样返回答案？")
        rec("给出了最终回答", result.answer is not None and "chat_fallback" in result.answer, str(result.answer)[:80])
        rec("停止原因是 final_answer", result.stop_reason == agent_service.STOP_FINAL_ANSWER, result.stop_reason)
        tools_used = [s.tool for s in result.trace]
        rec("工具调用顺序符合预期",
            tools_used == ["search_code", "read_file", "read_file"], str(tools_used))
        rec("第 3 步读的是上一步发现的文件",
            result.trace[2].arguments.get("rel_path") == "backend/app/services/llm_service.py",
            str(result.trace[2].arguments))
        rec("步数统计正确", result.steps == 3, str(result.steps))
        rec("证据清单只含真正读过的文件",
            result.citations == ["backend/app/main.py", "backend/app/services/llm_service.py"], str(result.citations))
        rec("没有报错", result.error is None, str(result.error))
        rec("轨迹可读", "search_code" in result.trace_text() and "read_file" in result.trace_text(), result.trace_text())

        print("=== 2. 工具结果确实被回喂给模型 ===")
        first_call_messages = record["messages"][0]
        last_call_messages = record["messages"][-1]
        rec("第 1 次调用时还没有工具结果", not any(m.get("role") == "tool" for m in first_call_messages), "")
        tool_msgs = [m for m in last_call_messages if m.get("role") == "tool"]
        # 走到最终回答那次请求时，前面 3 次工具调用（搜索 + 读两个文件）的结果都应已回喂
        rec("工具结果按轮次累积回喂", len(tool_msgs) == result.steps == 3,
            f"tool 消息数={len(tool_msgs)} 步数={result.steps}")
        rec("tool 消息带 tool_call_id", all(m.get("tool_call_id") for m in tool_msgs), str(tool_msgs)[:120])
        rec("工具结果里含真实文件内容",
            any("def chat(question)" in m["content"] for m in tool_msgs),
            str([m["content"][:60] for m in tool_msgs]))
        rec("assistant 的 tool_calls 被回放",
            any(m.get("tool_calls") for m in last_call_messages if m.get("role") == "assistant"), "")
        rec("system 提示里说明了工具与上限",
            "search_code" in first_call_messages[0]["content"] and "工具调用" in first_call_messages[0]["content"], "")

        print("=== 2b. 系统提示词里的关键约束（改进项 ①②）===")
        system_text = first_call_messages[0]["content"]
        rec("说明搜索是字面匹配、不支持正则",
            "字面匹配" in system_text and "不支持正则" in system_text, "")
        rec("工具清单里也标注了这一点", "不是正则" in system_text, "")
        rec("要求优先读实现文件（顺 import 追实现）",
            "优先读实现文件" in system_text and "import" in system_text, "")
        rec("要求不要反复列目录",
            "不要为每个目录都列一遍" in system_text, "")
        rec("提醒接近预算时宁可交代缺口",
            "还缺什么证据" in system_text, "")
        rec("要求打开实现文件就把它读完（改进项①）",
            "打开一个实现文件就把它读完" in system_text and "不要只读中段就换目标" in system_text, "")
        rec("预算数字来自配置（当前默认 25 次）",
            f"最多 {agent_service.AGENT_MAX_STEPS} 次探索工具调用" in system_text,
            f"AGENT_MAX_STEPS={agent_service.AGENT_MAX_STEPS}")

        print("=== 2c. 中途纠偏（默认关闭，可通过 AGENT_NUDGE_ENABLED=1 打开）===")
        rec("纠偏默认关闭（对照实验证明无可测量收益）",
            agent_service.AGENT_NUDGE_ENABLED is False, f"实际 {agent_service.AGENT_NUDGE_ENABLED}")
        nudge_record: dict = {}

        # 下面的用例直接测机制本身：临时打开开关，跑完恢复
        agent_service.AGENT_NUDGE_ENABLED = True

        def doc_then_impl(messages, tools=None, temperature=0.3):
            nudge_record["messages"] = json.loads(json.dumps(messages))
            if not tools:
                return _final_answer("收尾回答"), None
            outputs = _tool_messages(messages)
            impl_hint = any("打开过的实现文件" in m.get("content", "") or "把它读完" in m.get("content", "")
                            for m in messages if m.get("role") == "user")
            if impl_hint and not any("app/main.py" in o for o in outputs):
                return _tool_call("read_file", {"rel_path": "backend/app/main.py"}, "c_impl"), None
            if not outputs:
                # 第一步读文档（不是实现文件）——这才是"只读文档"的真实形态
                return _tool_call("read_file", {"rel_path": "docs/guide.md"}, "c0"), None
            if len(outputs) < 6:                      # 接着几轮都在做非实现的探索
                return _tool_call("search_code", {"keyword": f"kw{len(outputs)}"}, f"c{len(outputs)}"), None
            return _tool_call("finish", {"answer": "最终答案"}, "c_fin"), None

        llm_service.request_chat_completion = doc_then_impl
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "只看文档会怎样")
        rec("一直不读实现时触发了纠偏", result.nudges == 1, f"nudges={result.nudges}")
        rec("纠偏后模型确实去读了实现文件",
            result.implementation_files_read == ["backend/app/main.py"], str(result.implementation_files_read))
        rec("纠偏次数上限为 1（不会反复打断）", result.nudges <= 1, str(result.nudges))
        nudge_texts = [m.get("content", "") for m in nudge_record["messages"]
                       if m.get("role") == "user" and "系统提示" in m.get("content", "")]
        rec("纠偏消息出现在对话里", bool(nudge_texts), str(nudge_texts)[:120])
        rec("纠偏消息点明该回到哪个文件（引用已打开过的文件）",
            bool(nudge_texts) and "docs/guide.md" in nudge_texts[-1], str(nudge_texts)[-1][:160] if nudge_texts else "")

        # 一上来就读实现：不该被纠偏打扰
        def impl_first(messages, tools=None, temperature=0.3):
            if not tools:
                return _final_answer("收尾"), None
            if not _tool_messages(messages):
                return _tool_call("read_file", {"rel_path": "backend/app/main.py"}, "c1"), None
            return _tool_call("finish", {"answer": "答案"}, "cf"), None

        llm_service.request_chat_completion = impl_first
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "先读实现")
        rec("已读实现文件时不触发纠偏", result.nudges == 0, f"nudges={result.nudges}")
        rec("实现文件被登记", result.implementation_files_read == ["backend/app/main.py"], str(result.implementation_files_read))

        # 读过一个入口（__init__.py）之后就漂走：也要能被纠正
        def export_then_drift(messages, tools=None, temperature=0.3):
            if not tools:
                return _final_answer("收尾"), None
            outputs = _tool_messages(messages)
            if not outputs:
                return _tool_call("read_file", {"rel_path": "backend/app/__init__.py"}, "c0"), None
            if any("把它读完" in m.get("content", "") for m in messages if m.get("role") == "user") \
                    and not any("app/main.py" in o for o in outputs):
                return _tool_call("read_file", {"rel_path": "backend/app/main.py"}, "c_impl"), None
            if len(outputs) < 8:
                return _tool_call("list_directory", {"rel_path": f"d{len(outputs)}"}, f"c{len(outputs)}"), None
            return _tool_call("finish", {"answer": "答案"}, "cf"), None

        llm_service.request_chat_completion = export_then_drift
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "读完入口就漂走")
        rec("读过 __init__.py 后漂走仍会被纠偏", result.nudges >= 1, f"nudges={result.nudges}")
        rec("纠偏后被引导去读了实现文件",
            result.implementation_files_read == ["backend/app/main.py"], str(result.implementation_files_read))

        # 一直不读实现：纠偏次数要有上限，不能每步都打断
        llm_service.request_chat_completion = _varying_tool("list_directory", "d{n}")
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "永远不读实现")
        rec("纠偏次数不超过上限", result.nudges <= agent_service._NUDGE_MAX_TIMES,
            f"nudges={result.nudges} 上限={agent_service._NUDGE_MAX_TIMES}")

        agent_service.AGENT_NUDGE_ENABLED = False      # 恢复默认（关闭）

        # 关闭时不应再注入任何提示
        llm_service.request_chat_completion = _varying_tool("list_directory", "d{n}")
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "关闭纠偏后不该被打断")
        rec("关闭后不再注入纠偏", result.nudges == 0, f"nudges={result.nudges}")

        print("=== 3. 步数上限：停下并如实报告 ===")
        agent_service.AGENT_MAX_STEPS = 2
        llm_service.request_chat_completion = _varying_tool("read_file", "backend/app/main.py?n={n}")  # 每轮参数不同
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "一直读同一个文件会怎样？")
        rec("在步数上限处停止", result.steps <= 2, f"steps={result.steps}")
        rec("停止原因是 step_limit", result.stop_reason == agent_service.STOP_STEP_LIMIT, result.stop_reason)
        rec("仍然给出收尾回答（不让请求失败）", result.answer is not None, str(result.answer)[:60])
        rec("错误信息说明了原因", "上限" in (result.error or ""), str(result.error))
        agent_service.AGENT_MAX_STEPS = real_steps

        print("=== 4. 重复调用被识别（避免原地打转）===")
        llm_service.request_chat_completion = _always_tool("search_code", {"keyword": "chat_fallback"})
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "重复搜索")
        rec("停止原因是 no_new_info", result.stop_reason == agent_service.STOP_NO_NEW_INFO, result.stop_reason)
        rec("只用了一步就判定重复", result.steps == 1, str(result.steps))

        print("=== 5. 超时上限 ===")
        # 用负阈值保证第一次循环就触发时长上限（0.0 与极小值都可能因计时精度测不出来）
        agent_service.AGENT_TIMEOUT_SECONDS = -1.0
        llm_service.request_chat_completion = make_scripted_llm({})
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "超时测试")
        rec("停止原因是 time_limit", result.stop_reason == agent_service.STOP_TIME_LIMIT, result.stop_reason)
        rec("超时也给出收尾回答", result.answer is not None, str(result.answer)[:60])
        rec("错误信息说明是时长上限", "时长上限" in (result.error or ""), str(result.error))
        agent_service.AGENT_TIMEOUT_SECONDS = real_timeout

        print("=== 6. 模型不可用时优雅降级 ===")
        llm_service.request_chat_completion = lambda messages, tools=None, temperature=0.3: (None, "连接被拒绝")
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "模型挂了会怎样")
        rec("停止原因是 llm_error", result.stop_reason == agent_service.STOP_LLM_ERROR, result.stop_reason)
        rec("没有回答，交给上层回退", result.answer is None, str(result.answer))
        rec("错误原因可读", result.error == "连接被拒绝", str(result.error))
        rec("重试后仍失败才算失败（重试次数=上限）",
            result.stop_reason == agent_service.STOP_LLM_ERROR, "")

        # 第一次失败、第二次成功：应当被重试救回来
        state = {"n": 0}
        def flaky_then_ok(messages, tools=None, temperature=0.3):
            state["n"] += 1
            if state["n"] == 1:
                return None, "偶发 SSL EOF"
            return _final_answer("重试后拿到的答案"), None
        llm_service.request_chat_completion = flaky_then_ok
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "偶发失败")
        rec("偶发失败被同轮重试救回",
            result.answer == "重试后拿到的答案" and state["n"] == 2, f"n={state['n']} answer={result.answer!r}")

        print("=== 7. 工具参数非法 / 工具报错都不能中断循环 ===")
        def bad_then_good(messages, tools=None, temperature=0.3):
            if not any(m.get("role") == "tool" for m in messages):
                return _tool_call("read_file", {"rel_path": "backend/app/main.py"}, "call_bad"), None
            return _final_answer("根据读到的内容作答。"), None

        # 先让 arguments 不是合法 JSON
        def broken_json_llm(messages, tools=None, temperature=0.3):
            if not any(m.get("role") == "tool" for m in messages):
                return {
                    "choices": [{"message": {"role": "assistant", "content": "",
                                             "tool_calls": [{"id": "c1", "type": "function",
                                                             "function": {"name": "read_file", "arguments": "{不是JSON"}}]}}]
                }, None
            return _final_answer("参数坏了之后我改正了。"), None

        llm_service.request_chat_completion = broken_json_llm
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "参数非法")
        rec("参数非法时循环不崩", result.answer is not None, str(result.answer)[:60])
        rec("参数非法被记为一次空参数调用", result.steps == 1 and result.trace[0].arguments == {}, str(result.trace[0].arguments))

        llm_service.request_chat_completion = _always_tool("read_file", {"rel_path": "不存在的文件.py"})
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "读不存在的文件")
        rec("工具报错时把错误回给模型并继续",
            result.steps >= 1 and result.answer is not None, f"steps={result.steps} answer={str(result.answer)[:40]}")
        rec("轨迹里能看到失败的工具结果", "失败" in result.trace[0].result_preview, result.trace[0].result_preview)
        rec("重复请求同一个失败调用会被判定为无新信息",
            result.stop_reason == agent_service.STOP_NO_NEW_INFO, result.stop_reason)

        print("=== 8. 路径越界在 Agent 层同样被挡住 ===")
        llm_service.request_chat_completion = _always_tool("read_file", {"rel_path": "../../../secrets.json"})
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "越界读取")
        rec("越界请求被拒绝且回给模型", "越出仓库范围" in result.trace[0].result_preview, result.trace[0].result_preview)

        print("=== 9. 本地仓库不存在时的提示 ===")
        result = agent_service.ask_repository({**ANALYSIS, "repo_path": str(workdir / "not-exist")}, "在吗")
        rec("给出可操作的提示", result.answer is None and "重新" in (result.error or ""), str(result.error))

        print("=== 10. finish 结束工具 ===")

        def scripted_with_finish(messages, tools=None, temperature=0.3):
            """先搜索一次，然后调用 finish 结束——模拟显式声明结束的模型。"""
            if not tools:
                return _final_answer("收尾回答"), None
            tool_outputs = _tool_messages(messages)
            if not tool_outputs:
                return _tool_call("search_code", {"keyword": "chat_fallback"}), None
            return _tool_call("finish", {
                "answer": "chat_fallback 在 main.py 的 chat() 里被调用（backend/app/main.py:7）。",
                "evidence": ["backend/app/main.py", "backend/app/services/llm_service.py"],
            }, "call_finish"), None

        llm_service.request_chat_completion = scripted_with_finish
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "finish 用法")
        rec("finish 被识别为结束", result.stop_reason == agent_service.STOP_FINAL_ANSWER, result.stop_reason)
        rec("答案取自 finish 的 answer 参数",
            (result.answer or "").startswith("chat_fallback 在 main.py"), str(result.answer)[:60])
        rec("finish 不计入探索步数", result.steps == 1, f"steps={result.steps}")
        rec("证据清单合并了模型自报的依据",
            result.citations == ["backend/app/main.py", "backend/app/services/llm_service.py"], str(result.citations))
        rec("自报依据被单独保留", result.claimed_evidence == ["backend/app/main.py", "backend/app/services/llm_service.py"],
            str(result.claimed_evidence))
        rec("没有报错", result.error is None, str(result.error))

        def empty_finish_then_answer(messages, tools=None, temperature=0.3):
            """反复调用空 finish，验证硬闸生效。"""
            if not tools:
                return _final_answer("收尾回答"), None
            return _tool_call("finish", {}, "call_bad_finish"), None

        llm_service.request_chat_completion = empty_finish_then_answer
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "永远空 finish")
        rec("反复空 finish 不会无限循环（一次改正机会后判失败）",
            result.answer is None and result.stop_reason == agent_service.STOP_LLM_ERROR
            and "反复" in (result.error or ""), f"answer={result.answer!r} error={result.error}")

        def empty_finish_with_body(messages, tools=None, temperature=0.3):
            """finish 参数为空、但正文里有答案：应退回用正文，而不是判失败。"""
            return {"choices": [{"message": {"role": "assistant", "content": "正文里写好的答案",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "finish", "arguments": "{}"}}]}}]}, None

        llm_service.request_chat_completion = empty_finish_with_body
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "空 finish 但正文有答案")
        rec("finish 参数为空时退回用正文作答",
            result.answer == "正文里写好的答案" and result.stop_reason == agent_service.STOP_FINAL_ANSWER,
            f"answer={result.answer!r} stop={result.stop_reason}")

        def finish_empty_content(messages, tools=None, temperature=0.3):
            if not tools:
                return _final_answer("收尾回答"), None
            return {"choices": [{"message": {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "finish",
                                                 "arguments": json.dumps({"answer": "来自 finish 的答案"})}}]}}]}, None

        llm_service.request_chat_completion = finish_empty_content
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "只有 finish、正文为空")
        rec("finish 带答案但正文为空时仍能作答（原来的风险场景）",
            result.answer == "来自 finish 的答案" and result.stop_reason == agent_service.STOP_FINAL_ANSWER,
            f"answer={result.answer!r} stop={result.stop_reason}")

        # 探索工具与 finish 混在同一批：先执行探索，再用 finish 收尾
        def mixed_batch(messages, tools=None, temperature=0.3):
            if not tools:
                return _final_answer("收尾回答"), None
            if not _tool_messages(messages):
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "r1", "type": "function",
                     "function": {"name": "read_file", "arguments": json.dumps({"rel_path": "backend/app/main.py"})}},
                    {"id": "f1", "type": "function",
                     "function": {"name": "finish", "arguments": json.dumps({"answer": "混合批次的答案"})}},
                ]}}]}, None
            return _final_answer("兜底"), None

        llm_service.request_chat_completion = mixed_batch
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "混合批次")
        rec("同一批里的探索工具被先执行",
            result.steps == 1 and any(path.endswith("app/main.py") for path in result.citations),
            f"steps={result.steps} cites={result.citations}")
        rec("同一批里的 finish 正常收尾", result.answer == "混合批次的答案", str(result.answer)[:40])

        print("=== 11. finish_reason 分支（协议层的截断信号）===")

        def truncated_by_length(messages, tools=None, temperature=0.3):
            if not tools:
                return _final_answer("收尾回答"), None
            return {"choices": [{"finish_reason": "length",
                                 "message": {"role": "assistant", "content": "这是被截断的半个答"}}]}, None

        llm_service.request_chat_completion = truncated_by_length
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "输出被截断")
        rec("finish_reason=length 被识别并给出可操作原因",
            result.answer is None and result.stop_reason == agent_service.STOP_LLM_ERROR
            and "max_tokens" in (result.error or ""), f"error={result.error}")

        def empty_content_with_reason(messages, tools=None, temperature=0.3):
            if not tools:
                return _final_answer("收尾回答"), None
            return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": ""}}]}, None

        llm_service.request_chat_completion = empty_content_with_reason
        result = agent_service.run_agent_chat(ANALYSIS, fixture, "空正文")
        rec("错误信息带上真正的 finish_reason",
            result.answer is None and "finish_reason='stop'" in (result.error or ""), f"error={result.error}")

        print("=== 12. Demo 模式不进入 Agent ===")
        agent_service.DEMO_MODE = True
        llm_service.request_chat_completion = real_request
        result = agent_service.ask_repository({**ANALYSIS, "repo_path": str(fixture)}, "任何问题")
        rec("Demo 模式直接返回原因", result.answer is None and "Demo" in (result.error or ""), str(result.error))

    finally:
        llm_service.request_chat_completion = real_request
        agent_service.DEMO_MODE = real_demo
        agent_service.AGENT_MAX_STEPS = real_steps
        agent_service.AGENT_TIMEOUT_SECONDS = real_timeout
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"Agent 循环测试：通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    for name in failed:
        print("  FAIL:", name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

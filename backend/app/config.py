import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("repoassistant.config")

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("REPOASSISTANT_DATA_DIR") or (BASE_DIR / "data"))
REPOS_DIR = DATA_DIR / "repos"
ANALYSES_DIR = DATA_DIR / "analyses"

# 真实密钥不写在仓库里的任何文件中：优先读进程环境变量，其次读这个被 .gitignore 忽略的文件。
DEFAULT_SECRETS_FILE = DATA_DIR / "secrets.json"
SECRETS_FILE = Path(os.getenv("REPOASSISTANT_SECRETS_FILE") or DEFAULT_SECRETS_FILE)

# 分析结果缓存有效期（秒）；设为 0 表示只要缓存存在就直接复用。
ANALYSIS_CACHE_TTL = int(os.getenv("ANALYSIS_CACHE_TTL", str(24 * 60 * 60)))

# Agent 问答的循环上限（对应 docs/AGENT-STAGE0.md 第 9 节的停止标准）。
# 实测：中等仓库（pallets/click）一个"跨多文件"的问题，12 步仍会被用满且没读到实现，
# 因此默认设为 25。注意另外两个相邻约束（按实测每步约 2.6 秒 / 2000 字符估算）：
#   AGENT_TIMEOUT_SECONDS=90  -> 约 35 步
#   AGENT_CONTEXT_BUDGET=60000 字符 -> 约 30 步
# 也就是说 25 步刚好卡在两者之内；若继续调大步数，应同步调大这两个上限，
# 否则会先撞上"上下文预算截断"或"总时长超时"。
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "25"))             # 单次问答最多调用多少次工具
AGENT_TIMEOUT_SECONDS = float(os.getenv("AGENT_TIMEOUT_SECONDS", "90"))  # 单次问答的墙钟时间上限
AGENT_CONTEXT_BUDGET = int(os.getenv("AGENT_CONTEXT_BUDGET", "60000"))   # 回喂给模型的工具结果总字符预算
# 中途纠偏开关（默认**关闭**）：连续多步只读文档/测试时，注入一条"去读实现文件"的提示。
#
# 为什么默认关闭：对照实验（同一个问题、同一份代码、真实 click 仓库）显示它没有可测量的收益——
#   关闭纠偏：19 步 / 42 秒 / 自然收尾 / 读了 3 个实现文件（脚本内 6 次读实现）
#   开启纠偏：18 步 / 38 秒 / 自然收尾 / 读了 2 个实现文件（脚本内 5 次读实现）
#   两次注入里只有 1 次紧接着变成了读实现；而"读到实现 + 自然收尾"在纠偏为 0 时同样发生。
# 真正决定质量的是工具层修复（core.py 从"不可见"变成可读），不是这条提醒。
# 保留它的价值：遇到"整轮只在文档里打转"的极端轨迹时给一次纠正机会，且日志里的 纠偏=N 可当探针。
# 需要时设 AGENT_NUDGE_ENABLED=1 打开。
AGENT_NUDGE_ENABLED = os.getenv("AGENT_NUDGE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}

_DEFAULT_ALLOWED_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173"

_SECRETS_TEMPLATE = {
    "_readme": [
        "本文件保存本机私密配置，已被 .gitignore 忽略，请勿提交、勿截图分享。",
        "填写 DEEPSEEK_API_KEY 后重启后端即可使用 LLM；也可以改用系统环境变量 DEEPSEEK_API_KEY，环境变量优先级更高。",
        "建议把本文件权限限制为仅当前用户可读写。",
    ],
    "DEEPSEEK_API_KEY": "",
}


def _clean_secret(value: object) -> str:
    """密钥必须是单行：手工编辑时很容易把换行或引号一起粘进来。"""
    text = str(value or "").strip()
    return text.splitlines()[0].strip() if text else ""


def _load_secrets_file(path: Path) -> dict:
    """读取本地密钥文件；不存在返回空字典，内容有问题则记警告而不是静默失败。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        logger.warning("无法读取密钥文件 %s：%s", path, e)
        return {}

    try:
        data = json.loads(raw)
    except ValueError as e:
        logger.warning("密钥文件不是合法 JSON（%s）：%s", e, path)
        # 兜底：即使整体 JSON 不合法，也尽力取出单行密钥，避免一个多余换行就让系统退回 Demo 模式
        match = re.search(r'"DEEPSEEK_API_KEY"\s*:\s*"([^"\r\n]*)"', raw)
        recovered = _clean_secret(match.group(1)) if match else ""
        if recovered:
            logger.warning("已从损坏的密钥文件中恢复出 DEEPSEEK_API_KEY，建议重新保存为合法 JSON")
            return {"DEEPSEEK_API_KEY": recovered}
        return {}

    return data if isinstance(data, dict) else {}


def ensure_secrets_template() -> None:
    """首次运行时生成不含真实密钥的模板文件，方便用户知道该往哪里填。"""
    if SECRETS_FILE.exists():
        return
    try:
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRETS_FILE.write_text(
            json.dumps(_SECRETS_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass


def _resolve_api_key() -> tuple[str, str]:
    """返回 (api_key, source)。source 只描述密钥来自哪里，自身绝不含密钥内容。"""
    env_key = _clean_secret(os.getenv("DEEPSEEK_API_KEY", ""))
    if env_key:
        return env_key, "env"

    file_key = _clean_secret(_load_secrets_file(SECRETS_FILE).get("DEEPSEEK_API_KEY", ""))
    if file_key:
        return file_key, "file"

    return "", "none"


DEEPSEEK_API_KEY, API_KEY_SOURCE = _resolve_api_key()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
# 支持的名字以服务端为准：deepseek-flash / deepseek-v4-pro
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()

DEMO_MODE = not bool(DEEPSEEK_API_KEY)

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", _DEFAULT_ALLOWED_ORIGINS).split(",")
    if origin.strip()
]

REPOS_DIR.mkdir(parents=True, exist_ok=True)
ANALYSES_DIR.mkdir(parents=True, exist_ok=True)
if not DEMO_MODE or not SECRETS_FILE.exists():
    ensure_secrets_template()


def api_key_hint() -> str:
    """给日志用的密钥描述：只暴露来源和长度，不暴露内容。"""
    if not DEEPSEEK_API_KEY:
        return "未配置"
    return f"来源 {API_KEY_SOURCE}，长度 {len(DEEPSEEK_API_KEY)}"

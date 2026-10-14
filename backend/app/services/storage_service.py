import json
import re
from pathlib import Path

from app.config import ANALYSES_DIR

ANALYSIS_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class UnsafeAnalysisId(ValueError):
    """analysis_id 不是安全的文件名片段。"""


def _filepath(analysis_id: str) -> Path:
    if not ANALYSIS_ID_PATTERN.match(analysis_id or ""):
        raise UnsafeAnalysisId(f"非法 analysis_id：{analysis_id!r}")

    path = (ANALYSES_DIR / f"{analysis_id}.json").resolve()
    # 双保险：即使 id 通过正则，也确认最终路径仍然落在 analyses 目录内。
    if path.parent != ANALYSES_DIR.resolve():
        raise UnsafeAnalysisId(f"analysis_id 越出存储目录：{analysis_id!r}")
    return path


def save_analysis(analysis_id: str, data: dict) -> Path:
    ANALYSES_DIR.mkdir(parents=True, exist_ok=True)
    path = _filepath(analysis_id)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_analysis(analysis_id: str) -> dict:
    path = _filepath(analysis_id)
    if not path.exists():
        raise FileNotFoundError(f"Analysis not found: {analysis_id}")
    return json.loads(path.read_text(encoding="utf-8"))

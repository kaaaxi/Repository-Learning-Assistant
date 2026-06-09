import json
from pathlib import Path

from app.config import ANALYSES_DIR


def _filepath(analysis_id: str) -> Path:
    return ANALYSES_DIR / f"{analysis_id}.json"


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

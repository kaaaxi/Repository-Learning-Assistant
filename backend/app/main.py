import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import (
    ALLOWED_ORIGINS,
    API_KEY_SOURCE,
    DEEPSEEK_MODEL,
    DEMO_MODE,
    api_key_hint,
)
from app.models import AnalyzeRequest, AnalysisResult, ChatRequest, ChatResponse, HealthResponse
from app.services import analysis_service, llm_service, repo_service, storage_service

logger = logging.getLogger("repoassistant.api")

app = FastAPI(title="RepoAssistant", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", response_model=HealthResponse)
def health():
    """只暴露模型名和密钥来源，任何情况下都不返回密钥内容。"""
    return {
        "status": "ok",
        "demo_mode": DEMO_MODE,
        "model": DEEPSEEK_MODEL,
        "api_key_source": API_KEY_SOURCE,
        "cache_ttl_seconds": analysis_service.ANALYSIS_CACHE_TTL,
    }


@app.post("/api/analyze", response_model=AnalysisResult)
def analyze(req: AnalyzeRequest):
    try:
        return analysis_service.analyze(req.repo_url, force_refresh=req.force_refresh)
    except repo_service.InvalidRepoUrl as e:
        raise HTTPException(status_code=400, detail=str(e))
    except repo_service.CloneFailed as e:
        raise HTTPException(status_code=422, detail=str(e))
    except storage_service.UnsafeAnalysisId:
        raise HTTPException(status_code=400, detail="非法的仓库地址")
    except Exception as e:
        logger.exception("分析失败")
        raise HTTPException(status_code=500, detail=f"分析失败：{e}")


@app.get("/api/analysis/{analysis_id}", response_model=AnalysisResult)
def get_analysis(analysis_id: str):
    try:
        return storage_service.load_analysis(analysis_id)
    except storage_service.UnsafeAnalysisId:
        raise HTTPException(status_code=400, detail="非法的 analysis_id")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Analysis not found: {analysis_id}")


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")
    try:
        analysis = storage_service.load_analysis(req.analysis_id)
    except storage_service.UnsafeAnalysisId:
        raise HTTPException(status_code=400, detail="非法的 analysis_id")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Analysis not found: {req.analysis_id}")

    answer, llm_error = llm_service.chat_with_llm(analysis, req.question)
    if answer is not None:
        return {"answer": answer, "source": "llm", "llm_error": None}

    if llm_error and not DEMO_MODE:
        logger.warning("问答回退到规则回答：%s", llm_error)
    return analysis_service.chat_fallback(analysis, req.question, reason=llm_error)

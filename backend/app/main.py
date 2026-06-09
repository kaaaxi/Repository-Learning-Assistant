import subprocess

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import DEMO_MODE
from app.models import AnalyzeRequest, ChatRequest
from app.services import analysis_service, storage_service

app = FastAPI(title="RepoAssistant", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok", "demo_mode": DEMO_MODE}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    try:
        result = analysis_service.analyze(req.repo_url)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=422, detail=f"Clone failed: {e.stderr.strip()}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/analysis/{analysis_id}")
def get_analysis(analysis_id: str):
    try:
        return storage_service.load_analysis(analysis_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Analysis not found: {analysis_id}")


@app.post("/api/chat")
def chat(req: ChatRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")
    try:
        analysis = storage_service.load_analysis(req.analysis_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Analysis not found: {req.analysis_id}")
    return analysis_service.chat_fallback(analysis, req.question)

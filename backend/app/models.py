from pydantic import BaseModel, Field, field_validator

from app.urls import is_github_repo_url


class AnalyzeRequest(BaseModel):
    repo_url: str = Field(..., description="GitHub 仓库地址，例如 https://github.com/owner/repo")
    force_refresh: bool = Field(False, description="忽略缓存，强制重新克隆并分析")

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, value: str) -> str:
        if not is_github_repo_url(value):
            raise ValueError("仅支持 GitHub 仓库地址，例如 https://github.com/owner/repo")
        return value.strip()


class ChatRequest(BaseModel):
    analysis_id: str = Field(..., min_length=1, max_length=64)
    # 不加约束的 str 会忽略 min_length，空问题必须在入口就被拒
    question: str = Field(..., min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    answer: str
    source: str
    llm_error: str | None = None


class HealthResponse(BaseModel):
    status: str
    demo_mode: bool
    model: str
    api_key_source: str = Field(..., description="密钥来源：env / file / none，本身不含密钥内容")
    cache_ttl_seconds: int


class CodeMapItem(BaseModel):
    name: str
    description: str


class KeyModule(BaseModel):
    name: str
    description: str
    key_files: list[str]


class LearningPathStep(BaseModel):
    step: int
    title: str
    description: str


class ContributionSuggestion(BaseModel):
    title: str
    description: str
    difficulty: str


class AnalysisResult(BaseModel):
    """对外返回结构。LLM 输出也会经过这里的校验，缺字段时自动回退到规则分析。"""

    analysis_id: str
    project_name: str
    repo_url: str
    overview: str
    directory_tree: str
    architecture_summary: str
    code_map: list[CodeMapItem]
    key_modules: list[KeyModule]
    learning_path: list[LearningPathStep]
    contribution_suggestions: list[ContributionSuggestion]
    developer_workflow: str
    coding_conventions: str
    analysis_mode: str = "demo"
    analyzed_at: str | None = None
    llm_error: str | None = None

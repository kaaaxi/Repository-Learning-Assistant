from pydantic import BaseModel, HttpUrl


class AnalyzeRequest(BaseModel):
    repo_url: str


class ChatRequest(BaseModel):
    analysis_id: str
    question: str


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
    analysis_id: str
    project_name: str
    repo_url: str
    overview: str
    architecture_summary: str
    code_map: list[CodeMapItem]
    key_modules: list[KeyModule]
    learning_path: list[LearningPathStep]
    contribution_suggestions: list[ContributionSuggestion]
    developer_workflow: str
    coding_conventions: str

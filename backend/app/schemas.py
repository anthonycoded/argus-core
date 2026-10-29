
from pydantic import BaseModel


class TopicStatusUpdate(BaseModel):
    status: str


class StoryAnalysisResult(BaseModel):
    summary: str
    relevance: int
    research_priority: int
    rationale: str
    research_questions: list[str]

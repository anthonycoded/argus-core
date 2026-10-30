from typing import Literal
from pydantic import BaseModel, Field


class TopicStatusUpdate(BaseModel):
    status: str


class StoryAnalysisResult(BaseModel):
    summary: str
    relevance: int
    research_priority: int
    rationale: str
    research_questions: list[str]


class ResearchPlan(BaseModel):
    objective: str
    research_questions: list[str] = Field(
        min_length=1,
        max_length=8,
    )


class ResearchDecision(BaseModel):
    decision: Literal["continue", "finish"]
    reasoning: str
    missing_information: list[str] = Field(
        default_factory=list
    )


class ResearchAgentResult(BaseModel):
    topic: str
    objective: str
    status: Literal["completed", "incomplete", "failed"]

    rounds_completed: int
    research_questions: list[str]

    sources_discovered: int
    sources: list[dict] = Field(default_factory=list)

    metrics: dict = Field(default_factory=dict)
    review_queue: list[dict] = Field(default_factory=list)
    decision_history: list[dict] = Field(default_factory=list)

    findings: str
    limitations: list[str] = Field(default_factory=list)
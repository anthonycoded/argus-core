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

    
class EvidenceItem(BaseModel):
    source_title: str
    source_url: str
    publisher: str | None = None
    claim: str
    evidence_type: Literal[
        "reported_fact",
        "attributed_claim",
        "analysis",
    ]
    confidence: Literal[
        "high",
        "medium",
        "low",
    ]


class ResearchQuestionFinding(BaseModel):
    question: str
    finding: str
    evidence: list[EvidenceItem] = Field(
        default_factory=list
    )
    unanswered_aspects: list[str] = Field(
        default_factory=list
    )


class TimelineEvent(BaseModel):
    date: str | None = None
    event: str
    source_urls: list[str] = Field(
        default_factory=list
    )


class KeyActor(BaseModel):
    name: str
    role: str
    source_urls: list[str] = Field(
        default_factory=list
    )


class ConflictingAccount(BaseModel):
    issue: str
    accounts: list[str] = Field(
        default_factory=list
    )
    source_urls: list[str] = Field(
        default_factory=list
    )


class ResearchSynthesis(BaseModel):
    topic: str
    executive_summary: str

    timeline: list[TimelineEvent] = Field(
        default_factory=list
    )

    key_actors: list[KeyActor] = Field(
        default_factory=list
    )

    question_findings: list[ResearchQuestionFinding] = Field(
        default_factory=list
    )

    conflicting_accounts: list[ConflictingAccount] = Field(
        default_factory=list
    )

    evidence_gaps: list[str] = Field(
        default_factory=list
    )

    limitations: list[str] = Field(
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
    synthesis: ResearchSynthesis | None = None
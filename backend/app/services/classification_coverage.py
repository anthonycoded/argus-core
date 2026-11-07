"""Request/job-owned, bounded classification accounting; no durable storage."""

import time

from app.clients.argus_core_client import CoreClientError
from app.clients.classification_contracts import ClassificationCoverage, CoverageSource, UnclassifiedCandidate, MAX_OBSERVATIONS
from app.global_chess_config import global_chess_settings


class ClassificationFailure(CoreClientError):
    def __init__(self, error: CoreClientError, *, reason: str, attempted: bool, systemic: bool):
        super().__init__(error.status_code, error.detail, error.retry_after, reason=reason)
        self.attempted = attempted
        self.systemic = systemic


class ClassificationContext:
    def __init__(self, *, max_candidates=None, max_seconds=None, clock=time.monotonic):
        self.max_candidates = max_candidates if max_candidates is not None else global_chess_settings.classification_max_candidates
        self.max_seconds = max_seconds if max_seconds is not None else global_chess_settings.classification_max_seconds
        if type(self.max_candidates) is not int or not 1 <= self.max_candidates <= 150:
            raise ValueError("Classification candidate bound must be 1-150")
        if isinstance(self.max_seconds, bool) or not 0 < self.max_seconds <= 600:
            raise ValueError("Classification time bound must be 0-600 seconds")
        self.clock = clock
        self.elapsed = 0.0
        self.evaluated = 0
        self.attempted = 0
        self.classified = 0
        self.unclassified = []
        self.stop_reason = None
        self.error = None
        # Internal diagnostics/evidence survive cancellation when the caller
        # retains this context. Neither execution diagnostics nor arbitrary
        # candidate metadata is projected into coverage responses.
        self.models = []
        self.completed_sources = []
        self.review_queue = []

    def limit(self):
        if self.evaluated >= self.max_candidates:
            return "candidate_limit"
        if self.elapsed >= self.max_seconds:
            return "workload_deadline"
        return None

    def stop(self, reason, error=None):
        self.stop_reason = reason
        self.error = error or CoreClientError(
            504 if reason == "workload_deadline" else 503,
            "Research classification time limit reached." if reason == "workload_deadline"
            else "Research classification candidate limit reached.",
        )

    def record(self, source: CoverageSource, *, reason, attempted=False):
        if len(self.unclassified) >= MAX_OBSERVATIONS:
            raise ValueError("Classification coverage observation bound exceeded")
        self.unclassified.append(UnclassifiedCandidate(
            source=source, reason=reason, status="failed" if attempted else "not_attempted",
        ))
        if attempted:
            self.attempted += 1

    def snapshot(self):
        return ClassificationCoverage(
            complete=not self.unclassified and self.stop_reason is None,
            attempted=self.attempted, classified=self.classified,
            failed=sum(item.status == "failed" for item in self.unclassified),
            not_attempted=sum(item.status == "not_attempted" for item in self.unclassified),
            stop_reason=self.stop_reason, retry_after=self.error.retry_after if self.error else None,
            unclassified=self.unclassified,
        )

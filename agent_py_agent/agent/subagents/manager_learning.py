
from __future__ import annotations

"""Compatibility facade for self-learning draft candidate services."""

from .learning_similarity import _learning_similarity, _learning_tokens, _normalize_learning_text
from .services.learning import (
    SubAgentLearningService,
    UpdateLearningCandidateParams,
    _candidate_confidence,
    _find_best_candidate,
    _normalize_candidate,
)


class SubAgentLearningMixin:
    """Thin compatibility wrapper around SubAgentLearningService."""

    def _learning_service(self) -> SubAgentLearningService:
        service = getattr(self, "_learning_service_instance", None)
        if service is None:
            service = SubAgentLearningService(self)
            self._learning_service_instance = service
        return service

    def learning_drafts_dir(self):
        return self._learning_service().learning_drafts_dir()

    def learning_enabled(self) -> bool:
        return self._learning_service().learning_enabled()

    def list_learning_candidates(self):
        return self._learning_service().list_learning_candidates()

    def list_learning_candidates_report(self):
        return self._learning_service().list_learning_candidates_report()

    def load_learning_candidate(self, candidate_id: str):
        return self._learning_service().load_learning_candidate(candidate_id)

    def save_learning_candidate(self, candidate):
        return self._learning_service().save_learning_candidate(candidate)

    def record_learning_candidates(self, task, lessons: list[str]):
        return self._learning_service().record_learning_candidates(task, lessons)

    def set_learning_candidate_status(self, candidate_id: str, status: str):
        return self._learning_service().set_learning_candidate_status(candidate_id, status)

    def learning_stats(self):
        return self._learning_service().learning_stats()


__all__ = [
    "SubAgentLearningMixin",
    "SubAgentLearningService",
    "UpdateLearningCandidateParams",
    "_candidate_confidence",
    "_find_best_candidate",
    "_learning_similarity",
    "_learning_tokens",
    "_normalize_candidate",
    "_normalize_learning_text",
]

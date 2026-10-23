from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Situation(StrEnum):
    REPAIR = "repair_appointment"
    SCHOOL_ADMIN = "school_admin_appointment"
    SHIFT = "shift_clarification"


class CriterionResult(StrEnum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    UNKNOWN = "unknown"


class OutcomeStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CriterionEvidence:
    result: CriterionResult
    evidence_present: bool


@dataclass(frozen=True)
class Rubric:
    version: str
    required: Mapping[Situation, tuple[str, ...]]
    optional: Mapping[Situation, tuple[str, ...]]


@dataclass(frozen=True)
class AttemptEvaluationInput:
    situation: Situation
    criteria: Mapping[str, CriterionEvidence]
    asr_confidence: float
    minimization_complete: bool
    interaction_ended: bool
    learner_disputed: bool = False
    diarization_ambiguous: bool = False
    assistance_used: bool = False


@dataclass(frozen=True)
class OutcomeEvaluation:
    status: OutcomeStatus
    confirmation_required: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class SkillState:
    alpha: float = 1.0
    beta: float = 1.0
    authentic_attempt_count: int = 0

    @property
    def mastery(self) -> float:
        return self.alpha / (self.alpha + self.beta)

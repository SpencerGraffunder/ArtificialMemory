"""Shared pydantic models: wire protocol + extractor contract. Used by API, store, harness."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


FactType = Literal[
    "event", "commitment", "preference", "plan", "personal_fact", "opinion", "question", "other"
]
EntityKind = Literal["person", "place", "project", "org", "acronym", "other"]
ClarificationKind = Literal[
    "speaker_identity", "unknown_entity", "ambiguous_ref", "low_confidence_fact", "conflict"
]


class UtteranceLocation(BaseModel):
    lat: float
    lon: float
    accuracy_m: float = 0.0


class UtteranceEvent(BaseModel):
    """Spec §3.3 — what the phone emits. Idempotent by `id`."""

    id: str
    conversation_hint: str | None = None
    revision: int = 0
    start_ts: datetime
    end_ts: datetime | None = None
    speaker_id: str
    speaker_label: str
    speaker_confidence: float = 0.0
    text: str
    asr_confidence: float | None = None
    is_whisper: bool = False
    location: UtteranceLocation | None = None
    device: str = "iphone"

    @field_validator("text")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class IngestBatchRequest(BaseModel):
    utterances: list[UtteranceEvent] = Field(default_factory=list)


class IngestBatchResponse(BaseModel):
    accepted_ids: list[str]
    duplicate_ids: list[str]
    server_time: datetime


class Card(BaseModel):
    """Spec §5.4 — server-generated; the phone renders and logs feedback."""

    id: str
    kind: Literal["relevance", "lookup", "query"] = "relevance"
    memory_id: str | None = None
    headline: str
    detail: str = ""
    source_chip: str = ""           # "Jasmine · 2d ago" or "web · example.com"
    origin: Literal["memory", "web"] = "memory"
    url: str | None = None
    score: float = 0.0
    priority: bool = False          # whisper/direct-query results jump the stack
    source_utterance_ids: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    debug_features: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class FeedbackMessage(BaseModel):
    card_id: str
    kind: Literal["dismiss", "thumb_up", "thumb_down", "pin", "unpin", "expand"]
    at: datetime = Field(default_factory=utc_now)


class ClientEnvelope(BaseModel):
    """WS client → server."""

    type: Literal["utterance", "utterance_revision", "heartbeat", "feedback"]
    utterance: UtteranceEvent | None = None
    feedback: FeedbackMessage | None = None
    ts: datetime = Field(default_factory=utc_now)


class ServerEnvelope(BaseModel):
    """WS server → client."""

    type: Literal["card", "card_update", "card_dismiss", "status"]
    card: Card | None = None
    card_id: str | None = None
    status: dict[str, Any] | None = None
    ts: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------- extraction contract


class EntityRef(BaseModel):
    name: str
    kind: EntityKind = "other"
    aliases: list[str] = Field(default_factory=list)


class ExtractedFact(BaseModel):
    content: str                       # "dinner will be Chili"
    who_said: str | None = None        # speaker label/name as spoken
    said_to: list[str] = Field(default_factory=list)
    type: FactType = "other"
    time_phrase: str | None = None     # original words ("next Friday")
    event_time: str | None = None      # ISO date/datetime resolved by the LLM (cross-checked)
    event_time_end: str | None = None
    time_resolved: str | None = None   # filled by deterministic resolver (authoritative)
    time_resolved_end: str | None = None
    place: str | None = None
    entities: list[EntityRef] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.5, ge=0, le=1)


class Commitment(BaseModel):
    owner: str | None = None           # who committed (speaker label/name)
    text: str
    due_phrase: str | None = None
    due_date: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)


class Unknown(BaseModel):
    kind: ClarificationKind
    text: str                          # what is unknown ("ARTES", "Speaker-2")
    context: str = ""                  # transcript snippet
    importance: float = Field(default=0.5, ge=0, le=1)


class ExtractionResult(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    entities: list[EntityRef] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------- memories API


class MemoryOut(BaseModel):
    id: str
    text: str
    type: str
    importance: float
    confidence: float
    event_start: str | None
    event_end: str | None
    valid_from: str
    valid_to: str | None
    recorded_at: str
    status: str
    source: str
    speaker_id: str | None
    speaker_name: str | None
    conversation_id: str | None
    source_utterance_ids: list[str]
    entities: list[dict[str, str]] = Field(default_factory=list)
    structured: dict[str, Any] = Field(default_factory=dict)


class QueryRequest(BaseModel):
    q: str
    synthesize: bool | None = None     # default from config query.synthesize_answer
    speaker_id: str | None = None


class QueryResponse(BaseModel):
    card: Card
    memories: list[MemoryOut] = Field(default_factory=list)
    answer: str | None = None


class ClarificationAnswer(BaseModel):
    answer: str
    source: Literal["text", "voice"] = "text"


class SpeakerOut(BaseModel):
    id: str
    label: str
    name: str | None
    is_owner: bool
    status: str
    first_seen: str | None
    last_seen: str | None
    utterance_count: int = 0


class SpeakerPatch(BaseModel):
    name: str | None = None
    is_owner: bool | None = None


class SpeakerMerge(BaseModel):
    from_speaker_id: str
    into_speaker_id: str


# ---------------------------------------------------------------- small LLM outputs


class QuestionClassification(BaseModel):
    is_question: bool = False
    addressed_to_someone: bool = False
    topic: str = ""


class JudgeDecision(BaseModel):
    worth_surfacing: bool = False
    reason: str = ""


class ContradictDecision(BaseModel):
    relation: Literal["same", "replaces", "unrelated"] = "unrelated"


class ClarifyQuestion(BaseModel):
    kind: ClarificationKind
    question: str
    subject: str = ""
    importance: float = Field(default=0.5, ge=0, le=1)
    uncertainty: float = Field(default=0.5, ge=0, le=1)


class ClarifyList(BaseModel):
    questions: list[ClarifyQuestion] = Field(default_factory=list)


class DisambiguationResult(BaseModel):
    match_id: str | None = None

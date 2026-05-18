from dataclasses import dataclass


@dataclass
class TranscriptSegmentV2:
    speaker: str
    start: float
    end: float
    text_raw: str
    text_clean: str


@dataclass
class MeetingSummaryV2:
    summary: str
    key_decisions: list[str]
    risks: list[str]
    next_steps: list[str]


@dataclass
class TaskV2:
    title: str
    assignee: str | None
    due_date: str | None
    priority: str
    confidence: float
    source_quote: str

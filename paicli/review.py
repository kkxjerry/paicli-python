"""Structured reviewer protocol for Team orchestration.

Reviewer output is a typed verdict rather than an arbitrary success string.
Invalid JSON gets one bounded repair attempt. A reviewer error or exhausted
changes request never silently marks the task complete.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum

from .agents.models import AgentOutcome
from .subagents import SubAgent, TaskPacket


class ReviewVerdict(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass(frozen=True)
class ReviewResult:
    verdict: ReviewVerdict
    summary: str
    issues: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    raw_response: str = ""
    error: str = ""

    @property
    def approved(self) -> bool:
        return self.verdict is ReviewVerdict.APPROVED

    def feedback(self) -> str:
        parts = [self.summary]
        if self.issues:
            parts.append("Issues:\n" + "\n".join(f"- {item}" for item in self.issues))
        if self.suggestions:
            parts.append(
                "Suggested fixes:\n"
                + "\n".join(f"- {item}" for item in self.suggestions)
            )
        return "\n\n".join(part for part in parts if part.strip())


@dataclass(frozen=True)
class ReviewRun:
    result: ReviewResult
    model_outcomes: tuple[AgentOutcome, ...] = ()


class ReviewerAgent:
    """Review one worker artifact with a real isolated LLM sub-agent."""

    OUTPUT_SCHEMA = """Return only one JSON object:
{
  "verdict": "approved | changes_requested | rejected",
  "summary": "short factual assessment",
  "issues": ["specific unmet condition"],
  "suggestions": ["concrete local fix"],
  "evidence": ["file/tool/result supporting the verdict"]
}
Use approved only when the task and every acceptance criterion are supported by
observable evidence. Use changes_requested when the current task can be fixed
locally. Use rejected when the task is unsafe, based on a false prerequisite,
or cannot be repaired without changing the plan. Do not wrap JSON in markdown.
"""

    def __init__(
        self,
        subagent: SubAgent,
        *,
        max_repair_attempts: int = 1,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError("review repair attempts cannot be negative")
        self.subagent = subagent
        self.max_repair_attempts = max_repair_attempts

    def review(
        self,
        packet: TaskPacket,
        worker_outcome: AgentOutcome,
    ) -> ReviewRun:
        prompt = self._review_prompt(packet, worker_outcome)
        outcomes: list[AgentOutcome] = []
        last_error = ""
        for attempt in range(self.max_repair_attempts + 1):
            outcome = self.subagent.run_prompt(prompt)
            outcomes.append(outcome)
            if not outcome.succeeded:
                error = outcome.error or outcome.finish_reason.value
                return ReviewRun(
                    ReviewResult(
                        ReviewVerdict.ERROR,
                        "Reviewer execution failed.",
                        raw_response=outcome.content,
                        error=error,
                    ),
                    tuple(outcomes),
                )
            try:
                result = self.parse(outcome.content)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                if attempt >= self.max_repair_attempts:
                    break
                prompt = (
                    "Your previous review response was invalid: "
                    + last_error
                    + "\nReturn a corrected JSON object only, using the exact "
                    "schema and the same evidence."
                )
                continue
            return ReviewRun(result, tuple(outcomes))

        return ReviewRun(
            ReviewResult(
                ReviewVerdict.ERROR,
                "Reviewer did not return a valid structured verdict.",
                raw_response=outcomes[-1].content if outcomes else "",
                error=last_error,
            ),
            tuple(outcomes),
        )

    @classmethod
    def parse(cls, raw: str) -> ReviewResult:
        cleaned = _extract_json_object(raw)
        root = json.loads(cleaned)
        if not isinstance(root, dict):
            raise ValueError("review root must be a JSON object")

        raw_verdict = root.get("verdict")
        if raw_verdict is None and isinstance(root.get("approved"), bool):
            raw_verdict = (
                ReviewVerdict.APPROVED.value
                if root["approved"]
                else ReviewVerdict.CHANGES_REQUESTED.value
            )
        if not isinstance(raw_verdict, str):
            raise ValueError("review verdict must be a string")
        try:
            verdict = ReviewVerdict(raw_verdict.strip().lower())
        except ValueError as exc:
            raise ValueError(
                "review verdict must be approved, changes_requested, or rejected"
            ) from exc
        if verdict is ReviewVerdict.ERROR:
            raise ValueError("the model cannot emit the internal error verdict")

        summary = root.get("summary", "")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("review summary must be a non-empty string")
        issues = _string_tuple(root.get("issues", []), "issues")
        suggestions = _string_tuple(root.get("suggestions", []), "suggestions")
        evidence = _string_tuple(root.get("evidence", []), "evidence")
        if verdict is not ReviewVerdict.APPROVED and not issues:
            raise ValueError("a non-approved review must include at least one issue")
        return ReviewResult(
            verdict,
            summary.strip(),
            issues,
            suggestions,
            evidence,
            raw_response=raw,
        )

    def _review_prompt(
        self,
        packet: TaskPacket,
        worker_outcome: AgentOutcome,
    ) -> str:
        tool_evidence = [
            {
                "tool": result.tool_name,
                "ok": result.ok,
                "error_type": result.error_type.value if result.error_type else None,
                "content": _truncate(result.content, 2_000),
                "changed_files": list(result.changed_files),
            }
            for result in worker_outcome.tool_results
        ]
        payload = {
            "goal": packet.goal,
            "task": {
                "id": packet.task_id,
                "type": packet.task_type.value,
                "description": packet.description,
                "acceptance_criteria": list(packet.acceptance_criteria),
                "attempt": packet.attempt,
            },
            "direct_dependency_results": [
                {
                    "task_id": item.task_id,
                    "result": _truncate(item.result, 4_000),
                    "changed_files": list(item.changed_files),
                }
                for item in packet.dependencies
            ],
            "worker": {
                "status": worker_outcome.status.value,
                "finish_reason": worker_outcome.finish_reason.value,
                "answer": _truncate(worker_outcome.content, 8_000),
                "error": worker_outcome.error,
                "changed_files": list(worker_outcome.changed_files),
                "tool_results": tool_evidence,
            },
        }
        return (
            self.OUTPUT_SCHEMA
            + "\nReview this artifact. You may use exposed read-only tools to "
            "inspect changed files before deciding.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )


def _extract_json_object(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("reviewer returned empty content")
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("review output does not contain a JSON object")
    return cleaned[start : end + 1]


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"review {field} must be a string array")
    cleaned = tuple(item.strip() for item in value if item.strip())
    return cleaned


def _truncate(value: str, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "\n...(truncated)"


__all__ = [
    "ReviewResult",
    "ReviewRun",
    "ReviewVerdict",
    "ReviewerAgent",
]

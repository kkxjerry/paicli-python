"""Independent reviewer contract and artifact-evidence quality gate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from .agents.models import AgentOutcome
from .subagents import SubAgent, TaskPacket
from .tool_contracts import ResourceMode


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
    error: str = ""
    retryable: bool = False

    def feedback(self) -> str:
        parts = [self.summary]
        if self.issues:
            parts.append("Issues:\n" + "\n".join(f"- {item}" for item in self.issues))
        if self.suggestions:
            parts.append(
                "Suggestions:\n"
                + "\n".join(f"- {item}" for item in self.suggestions)
            )
        return "\n\n".join(part for part in parts if part.strip())


@dataclass(frozen=True)
class ReviewRun:
    result: ReviewResult
    model_outcomes: tuple[AgentOutcome, ...] = ()


class ReviewerAgent:
    """Run a read-only reviewer and parse one bounded JSON repair."""

    def __init__(self, subagent: SubAgent, *, max_repair_attempts: int = 1) -> None:
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts cannot be negative")
        self.subagent = subagent
        self.max_repair_attempts = max_repair_attempts

    def review(self, packet: TaskPacket, worker: AgentOutcome) -> ReviewRun:
        prompt = self._prompt(packet, worker)
        outcomes: list[AgentOutcome] = []
        last_error = ""
        for attempt in range(self.max_repair_attempts + 1):
            outcome = self.subagent.run_prompt(prompt)
            outcomes.append(outcome)
            if not outcome.succeeded:
                detail = outcome.error or outcome.finish_reason.value
                return ReviewRun(
                    ReviewResult(
                        ReviewVerdict.ERROR,
                        "Reviewer model did not complete successfully.",
                        error=detail,
                        retryable=False,
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
                    "Your prior review response was invalid: "
                    + last_error
                    + "\nReturn exactly one corrected JSON object matching the "
                    "requested review schema. Do not repeat hidden reasoning."
                )
                continue

            result = _normalize_locally_repairable_rejection(packet, worker, result)
            missing = _missing_changed_file_evidence(worker, outcomes)
            if result.verdict is ReviewVerdict.APPROVED and missing:
                return ReviewRun(
                    ReviewResult(
                        ReviewVerdict.CHANGES_REQUESTED,
                        "Approval requires direct inspection of every changed artifact.",
                        issues=tuple(
                            f"Reviewer did not read changed artifact: {path}"
                            for path in missing
                        ),
                        suggestions=(
                            "Use read_file or another read-only repository tool to "
                            "inspect the actual changed artifact, then review again.",
                        ),
                        evidence=result.evidence,
                        retryable=True,
                    ),
                    tuple(outcomes),
                )
            return ReviewRun(result, tuple(outcomes))

        return ReviewRun(
            ReviewResult(
                ReviewVerdict.ERROR,
                "Reviewer output remained invalid after bounded repair.",
                error=last_error,
                retryable=False,
            ),
            tuple(outcomes),
        )

    @staticmethod
    def parse(raw: str) -> ReviewResult:
        root = json.loads(_extract_json(raw))
        if not isinstance(root, dict):
            raise ValueError("review root must be a JSON object")
        try:
            verdict = ReviewVerdict(str(root.get("verdict", "")).lower())
        except ValueError as exc:
            raise ValueError(
                "review verdict must be approved, changes_requested, or rejected"
            ) from exc
        if verdict is ReviewVerdict.ERROR:
            raise ValueError("the model cannot emit the internal error verdict")
        summary = root.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("review summary must be a non-empty string")
        issues = _string_array(root.get("issues", []), "issues")
        suggestions = _string_array(root.get("suggestions", []), "suggestions")
        evidence = _string_array(root.get("evidence", []), "evidence")
        retry_raw = root.get("retryable")
        if retry_raw is not None and not isinstance(retry_raw, bool):
            raise ValueError("review retryable must be a boolean")
        retryable = (
            retry_raw
            if isinstance(retry_raw, bool)
            else verdict is ReviewVerdict.CHANGES_REQUESTED
        )
        if verdict is ReviewVerdict.APPROVED and issues:
            raise ValueError("approved review cannot contain unresolved issues")
        if verdict is ReviewVerdict.REJECTED:
            retryable = False
        return ReviewResult(
            verdict=verdict,
            summary=summary.strip(),
            issues=issues,
            suggestions=suggestions,
            evidence=evidence,
            retryable=retryable,
        )

    @staticmethod
    def _prompt(packet: TaskPacket, worker: AgentOutcome) -> str:
        payload = {
            "goal": packet.goal,
            "task": {
                "id": packet.task_id,
                "type": packet.task_type.value,
                "description": packet.description,
                "acceptance_criteria": list(packet.acceptance_criteria),
                "attempt": packet.attempt,
            },
            "worker": {
                "status": worker.status.value,
                "finish_reason": worker.finish_reason.value,
                "answer": worker.content,
                "changed_files": list(worker.changed_files),
                "tool_results": [
                    {
                        "tool": result.tool_name,
                        "ok": result.ok,
                        "error_type": (
                            result.error_type.value if result.error_type else None
                        ),
                        "content": _truncate(result.content, 4_000),
                        "changed_files": list(result.changed_files),
                    }
                    for result in worker.tool_results
                ],
            },
            "dependencies": [
                {
                    "task_id": item.task_id,
                    "result": _truncate(item.result, 4_000),
                    "changed_files": list(item.changed_files),
                }
                for item in packet.dependencies
            ],
        }
        return (
            "Review the task against its acceptance criteria and actual "
            "repository evidence. Do not trust the worker narrative alone. "
            "Use read-only tools to inspect changed files before approval.\n\n"
            "Return exactly one JSON object:\n"
            '{"verdict":"approved|changes_requested|rejected",'
            '"summary":"...","issues":[],"suggestions":[],'
            '"evidence":[],"retryable":true}\n'
            "Use changes_requested with retryable=true for any defect that can "
            "be fixed by redoing only this assigned task, including an incorrect "
            "implementation in a changed file. Use rejected only for unsafe work, "
            "a false prerequisite, an out-of-scope request, or a problem that "
            "requires changing the plan.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )


def _normalize_locally_repairable_rejection(
    packet: TaskPacket,
    worker: AgentOutcome,
    result: ReviewResult,
) -> ReviewResult:
    """Keep model wording but enforce the review protocol's local-repair rule."""

    if result.verdict is not ReviewVerdict.REJECTED or not worker.changed_files:
        return result
    text = " ".join((result.summary, *result.issues)).lower()
    terminal_markers = (
        "unsafe",
        "unauthorized",
        "permission",
        "false prerequisite",
        "out of scope",
        "out-of-scope",
        "change the plan",
        "replan",
        "cannot be repaired",
    )
    if any(marker in text for marker in terminal_markers):
        return result
    return ReviewResult(
        verdict=ReviewVerdict.CHANGES_REQUESTED,
        summary=result.summary,
        issues=result.issues or (
            f"Task {packet.task_id} did not satisfy its acceptance criteria.",
        ),
        suggestions=result.suggestions,
        evidence=result.evidence,
        error=result.error,
        retryable=True,
    )


def _missing_changed_file_evidence(
    worker: AgentOutcome,
    review_outcomes: list[AgentOutcome],
) -> tuple[str, ...]:
    changed = tuple(dict.fromkeys(worker.changed_files))
    if not changed:
        return ()
    observed: set[str] = set()
    for outcome in review_outcomes:
        for result in outcome.tool_results:
            if not result.ok:
                continue
            for access in result.accesses:
                if access.mode is ResourceMode.READ:
                    observed.add(access.resource)
    return tuple(
        path
        for path in changed
        if not any(_covers(observation, path) for observation in observed)
    )


def _covers(observation: str, changed: str) -> bool:
    if observation == changed:
        return True
    left = PurePosixPath(observation).parts
    right = PurePosixPath(changed).parts
    return bool(left) and len(left) <= len(right) and right[: len(left)] == left


def _extract_json(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("reviewer returned empty content")
    value = raw.strip()
    if value.startswith("```"):
        first_newline = value.find("\n")
        value = value[first_newline + 1 :] if first_newline >= 0 else value
        if value.endswith("```"):
            value = value[:-3]
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("reviewer output does not contain a JSON object")
    return value[start : end + 1]


def _string_array(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"review {name} must be a string array")
    return tuple(item.strip() for item in value if item.strip())


def _truncate(value: str, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "\n...(truncated)"


__all__ = [
    "ReviewResult",
    "ReviewRun",
    "ReviewVerdict",
    "ReviewerAgent",
]

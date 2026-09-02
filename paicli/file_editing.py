"""Atomic, optimistic file editing primitives used by Coding Agent tools."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True)
class TextEdit:
    path: str
    old_text: str
    new_text: str
    expected_replacements: int = 1
    expected_sha256: str = ""


@dataclass(frozen=True)
class FileMutation:
    path: str
    before: str | None
    after: str | None

    @property
    def diff(self) -> str:
        return unified_diff(self.path, self.before or "", self.after or "")


@dataclass(frozen=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: tuple[PatchHunk, ...]

    @property
    def effective_path(self) -> str:
        value = self.new_path if self.new_path is not None else self.old_path
        if not value:
            raise ValueError("patch has no effective path")
        return value


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unified_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def replace_text_content(
    content: str,
    old_text: str,
    new_text: str,
    *,
    expected_replacements: int = 1,
) -> str:
    if not old_text:
        raise ValueError("old_text cannot be empty")
    if expected_replacements < 1:
        raise ValueError("expected_replacements must be positive")
    actual = content.count(old_text)
    if actual != expected_replacements:
        raise ValueError(
            "replacement precondition failed: expected "
            f"{expected_replacements} occurrence(s), found {actual}"
        )
    return content.replace(old_text, new_text, expected_replacements)


def prepare_text_edits(
    root: Path,
    edits: Iterable[TextEdit],
) -> tuple[FileMutation, ...]:
    """Validate every edit before returning any mutation."""

    pending: dict[str, str] = {}
    originals: dict[str, str] = {}
    order: list[str] = []
    for edit in edits:
        path = _safe_path(root, edit.path)
        key = path.relative_to(root).as_posix()
        if key not in pending:
            if not path.is_file():
                raise ValueError(f"not a file: {edit.path}")
            content = path.read_text(encoding="utf-8")
            originals[key] = content
            pending[key] = content
            order.append(key)
        if edit.expected_sha256:
            actual_hash = sha256_text(pending[key])
            if actual_hash.lower() != edit.expected_sha256.lower():
                raise ValueError(
                    f"file hash changed for {key}: expected {edit.expected_sha256}, "
                    f"found {actual_hash}"
                )
        pending[key] = replace_text_content(
            pending[key],
            edit.old_text,
            edit.new_text,
            expected_replacements=edit.expected_replacements,
        )
    return tuple(
        FileMutation(key, originals[key], pending[key])
        for key in order
        if originals[key] != pending[key]
    )


def write_mutations(root: Path, mutations: Iterable[FileMutation]) -> tuple[str, ...]:
    """Write all prepared mutations atomically per file.

    Validation and patch application happen before this function.  Each final
    file replacement uses a sibling temporary file and ``os.replace`` so readers
    never observe a partially-written UTF-8 file.
    """

    changed: list[str] = []
    for mutation in mutations:
        path = _safe_path(root, mutation.path)
        if mutation.after is None:
            if path.exists() or path.is_symlink():
                path.unlink()
                changed.append(mutation.path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, mutation.after)
        changed.append(mutation.path)
    return tuple(changed)


def parse_unified_patch(raw: str) -> tuple[FilePatch, ...]:
    """Parse the text-file subset of unified diff format."""

    lines = str(raw).splitlines(keepends=True)
    patches: list[FilePatch] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        old_path = _header_path(lines[index][4:])
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("patch file header is missing +++ line")
        new_path = _header_path(lines[index][4:])
        index += 1
        hunks: list[PatchHunk] = []
        while index < len(lines) and not lines[index].startswith("--- "):
            if not lines[index].startswith("@@ "):
                if lines[index].strip():
                    raise ValueError(
                        f"unexpected patch line outside a hunk: {lines[index].rstrip()}"
                    )
                index += 1
                continue
            match = re.match(
                r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
                lines[index],
            )
            if match is None:
                raise ValueError(f"invalid patch hunk header: {lines[index].rstrip()}")
            index += 1
            hunk_lines: list[str] = []
            while index < len(lines):
                line = lines[index]
                if line.startswith("@@ ") or line.startswith("--- "):
                    break
                if line.startswith((" ", "+", "-", "\\")):
                    hunk_lines.append(line)
                    index += 1
                    continue
                raise ValueError(f"invalid patch hunk line: {line.rstrip()}")
            hunks.append(
                PatchHunk(
                    int(match.group(1)),
                    int(match.group(2) or 1),
                    int(match.group(3)),
                    int(match.group(4) or 1),
                    tuple(hunk_lines),
                )
            )
        if not hunks:
            raise ValueError("patch file contains no hunks")
        if old_path is None and new_path is None:
            raise ValueError("patch cannot use /dev/null for both paths")
        patches.append(FilePatch(old_path, new_path, tuple(hunks)))
    if not patches:
        raise ValueError("patch contains no file changes")
    return tuple(patches)


def patch_paths(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(patch.effective_path for patch in parse_unified_patch(raw)))


def prepare_patch(
    root: Path,
    raw: str,
    *,
    expected_sha256: Mapping[str, str] | None = None,
) -> tuple[FileMutation, ...]:
    hashes = {str(key): str(value) for key, value in dict(expected_sha256 or {}).items()}
    mutations: list[FileMutation] = []
    seen: set[str] = set()
    for patch in parse_unified_patch(raw):
        path_key = patch.effective_path
        if path_key in seen:
            raise ValueError(f"patch contains duplicate file section: {path_key}")
        seen.add(path_key)
        path = _safe_path(root, path_key)
        if patch.old_path is None:
            if path.exists():
                raise ValueError(f"patch create target already exists: {path_key}")
            before: str | None = None
            source = ""
        else:
            old_path = _safe_path(root, patch.old_path)
            if not old_path.is_file():
                raise ValueError(f"patch source is not a file: {patch.old_path}")
            before = old_path.read_text(encoding="utf-8")
            source = before
            expected = hashes.get(path_key) or hashes.get(patch.old_path)
            if expected:
                actual = sha256_text(source)
                if actual.lower() != expected.lower():
                    raise ValueError(
                        f"file hash changed for {patch.old_path}: expected {expected}, "
                        f"found {actual}"
                    )
        after = _apply_hunks(source, patch.hunks)
        if patch.new_path is None:
            after_value: str | None = None
        else:
            after_value = after
        mutations.append(FileMutation(path_key, before, after_value))
    return tuple(mutations)


def patch_preview(root: Path, raw: str) -> str:
    mutations = prepare_patch(root, raw)
    return "".join(mutation.diff for mutation in mutations)


def _apply_hunks(source: str, hunks: tuple[PatchHunk, ...]) -> str:
    source_lines = source.splitlines(keepends=True)
    output: list[str] = []
    source_index = 0
    for hunk in hunks:
        target = max(0, hunk.old_start - 1)
        if target < source_index or target > len(source_lines):
            raise ValueError("patch hunks overlap or start outside the source file")
        output.extend(source_lines[source_index:target])
        source_index = target
        old_seen = 0
        new_seen = 0
        for raw_line in hunk.lines:
            if raw_line.startswith("\\"):
                if output:
                    output[-1] = output[-1].rstrip("\r\n")
                continue
            marker = raw_line[0]
            text = raw_line[1:]
            if marker in {" ", "-"}:
                if source_index >= len(source_lines):
                    raise ValueError("patch expects content beyond end of file")
                if source_lines[source_index] != text:
                    raise ValueError(
                        "patch context mismatch at source line "
                        f"{source_index + 1}: expected {text.rstrip()!r}, "
                        f"found {source_lines[source_index].rstrip()!r}"
                    )
                if marker == " ":
                    output.append(text)
                    new_seen += 1
                source_index += 1
                old_seen += 1
            elif marker == "+":
                output.append(text)
                new_seen += 1
            else:
                raise ValueError(f"unsupported patch marker: {marker!r}")
        if old_seen != hunk.old_count or new_seen != hunk.new_count:
            raise ValueError(
                "patch hunk count mismatch: "
                f"header old/new={hunk.old_count}/{hunk.new_count}, "
                f"body={old_seen}/{new_seen}"
            )
    output.extend(source_lines[source_index:])
    return "".join(output)


def _header_path(raw: str) -> str | None:
    value = raw.rstrip("\r\n").split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value or value.startswith("/"):
        raise ValueError(f"patch path must be project-relative: {value!r}")
    normalized = Path(value).as_posix()
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("patch path escapes project root")
    return normalized


def _safe_path(root: Path, raw_path: str) -> Path:
    path = (root / str(raw_path)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes project root")
    return path


def _atomic_write_text(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            try:
                os.chmod(temporary, path.stat().st_mode & 0o777)
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "FileMutation",
    "FilePatch",
    "PatchHunk",
    "TextEdit",
    "parse_unified_patch",
    "patch_paths",
    "patch_preview",
    "prepare_patch",
    "prepare_text_edits",
    "replace_text_content",
    "sha256_file",
    "sha256_text",
    "unified_diff",
    "write_mutations",
]

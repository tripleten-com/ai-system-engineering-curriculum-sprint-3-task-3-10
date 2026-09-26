"""Coldline.

===================

File:              tests/contract/submission_validation.py
Component:         Contract tests — Submission Validation
Purpose:           Validate Task 3.10 answers, the cache record, and the permitted change paths.
Interacts With:    Published interfaces and repository boundaries
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Compatibility, ownership, export safety
Tools:             Python 3.12, pytest
"""

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator

# Task 3.10's pull request carries the two wiring points, the TTL, the probe output, the
# answer sheet, and the cache record. The cache adapter, the status route, the worker loop,
# the store, the probe, the transport adapters, the tests, and the workflows stay protected:
# the lesson says the two supplied functions and the one key are the student's, so these
# six files are the whole editable surface. The probe output is generated evidence:
# permitted here so it can be committed, and checked for an unedited digest by
# test_cache_contract.py.
ALLOWED_PATHS = frozenset(
    {
        "src/api/status_cache.py",
        "src/worker/status_invalidation.py",
        "config/cache.yaml",
        "docs/student/cache/probe.json",
        "docs/student/task-3-10-cache-record.md",
        "submission.yaml",
    }
)
# No directory prefix: additions under tests/student/ stayed closed when Task 3.7 closed
# them, and this Task's code lives in two named files.
ALLOWED_PREFIXES: tuple[str, ...] = ()
# The cache record supports the defense; the check only refuses the untouched template.
BASELINE_PATH: str | None = "docs/student/task-3-10-cache-record.md"
BASELINE_MARKERS: frozenset[str] = frozenset({"_Write your evidence here._"})


_JSON_YAML_TAGS = frozenset(
    {
        "tag:yaml.org,2002:map",
        "tag:yaml.org,2002:seq",
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:null",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
    }
)


class _RestrictedYamlLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Load the Task's small YAML profile without YAML-only conveniences."""

    def compose_node(self, parent: object, index: object) -> yaml.Node:
        # A prior anchor is already rejected below, but deny aliases directly too.
        if self.check_event(yaml.AliasEvent):
            event = self.get_event()
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML aliases are not permitted",
                event.start_mark,
            )
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML anchors are not permitted",
                event.start_mark,
            )
        return super().compose_node(parent, index)

    def construct_object(self, node: yaml.Node, deep: bool = False) -> object:
        if node.tag not in _JSON_YAML_TAGS:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "non-JSON YAML tags are not permitted",
                node.start_mark,
            )
        return super().construct_object(node, deep=deep)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, object]:
        mapping: dict[str, object] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    "YAML merge keys are not permitted",
                    key_node.start_mark,
                )
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    "YAML mapping keys must be strings",
                    key_node.start_mark,
                )
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"duplicate YAML key: {key}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class SubmissionError(ValueError):
    """Report one actionable public-verification failure."""


def main(root: Path | None = None, *, changed_paths: list[str] | None = None) -> int:
    """Validate the current answer sheet, the record template, and the path boundary.

    The optional arguments keep this entrypoint testable without changing the
    process working directory or creating a temporary Git repository.
    """
    task_root = Path.cwd() if root is None else root
    try:
        validate_submission(
            task_root / "submission.yaml",
            task_root / "docs/contracts/submission.schema.json",
            sample_path=task_root / "submission-sample.yaml",
            task_root=task_root,
        )
        if BASELINE_PATH is not None:
            validate_baseline(task_root / BASELINE_PATH)
        validate_changed_paths(
            _changed_paths(task_root) if changed_paths is None else changed_paths
        )
    except (SubmissionError, RuntimeError) as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    print("Task 3.10 answer verification passed.")
    return 0


def validate_submission(
    submission_path: Path,
    schema_path: Path,
    *,
    sample_path: Path | None = None,
    task_root: Path | None = None,
) -> None:
    """Validate YAML shape, placeholders, schema, and sample-copy behavior.

    ``task_root`` names the repository that owns the published contract. It
    defaults to the answer sheet's own directory, which is correct for a
    student checkout. A curriculum-owned evaluator validating a reference sheet
    stored elsewhere passes the trusted Task root explicitly.
    """
    submission = _load_one_document(submission_path)
    answers = submission.get("answers") if isinstance(submission, dict) else None
    if not isinstance(answers, dict):
        raise SubmissionError("answers must be one mapping")

    # The freshness window nests one level deep, so the placeholder walk recurses rather
    # than stopping at the first nested mapping as earlier Tasks' checks did.
    _reject_placeholders(answers, "answers")

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(submission), key=lambda error: list(error.path)
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "submission"
        raise SubmissionError(f"{location}: {error.message}")

    if sample_path is not None and submission == _load_one_document(sample_path):
        raise SubmissionError("submission must not copy the fictional sample answers")


def _reject_placeholders(mapping: dict[str, Any], prefix: str) -> None:
    """Name the first blank or placeholder string anywhere under the answers mapping."""
    for field, value in mapping.items():
        location = f"{prefix}.{field}"
        if isinstance(value, str) and (not value.strip() or "Replace this line" in value):
            raise SubmissionError(f"{location} is incomplete")
        if isinstance(value, dict):
            _reject_placeholders(value, location)


def validate_baseline(baseline_path: Path) -> None:
    """Reject the untouched evidence template without grading its prose."""
    text = baseline_path.read_text(encoding="utf-8")
    remaining = sorted(marker for marker in BASELINE_MARKERS if marker in text)
    if remaining:
        raise SubmissionError(f"{baseline_path.name} still contains template markers")


def validate_changed_paths(paths: list[str]) -> None:
    """Reject changed paths outside this Task's student-editable surfaces."""
    normalized = {PurePosixPath(path.replace("\\", "/")).as_posix() for path in paths}
    protected = sorted(
        path for path in normalized - ALLOWED_PATHS if not path.startswith(ALLOWED_PREFIXES)
    )
    if protected:
        raise SubmissionError(f"protected path changed: {', '.join(protected)}")


def _changed_paths(root: Path) -> list[str]:
    """Return changes since the commit this checkout branched from."""
    try:
        repository_root = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        ).resolve()
        if root.resolve() != repository_root:
            # The authoring snapshot is nested. Only a generated repository's
            # own history defines student changes.
            return []
        baseline = _baseline_commit(repository_root)
        result = subprocess.run(
            ["git", "diff", "--name-only", baseline],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Git history is unavailable for protected-path validation") from exc
    return [line for line in result.stdout.splitlines() if line]


def _baseline_commit(repository_root: Path) -> str:
    """Return the commit a student's changes are measured against.

    Student work happens ahead of `main` - on a branch, or as uncommitted
    edits - and `main` itself keeps moving as this repository receives
    updates after a student has already forked from it. The protected
    boundary is therefore the commit a student actually started from
    (their merge-base with `main`), not the repository's very first
    commit, which a later update may have moved past.
    """
    for candidate in ("origin/main", "main"):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=repository_root,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            merge_base = subprocess.run(
                ["git", "merge-base", "HEAD", candidate],
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
            )
            return merge_base.stdout.strip()
    # No `main` branch is reachable - fall back to the repository's single
    # root commit.
    roots = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if len(roots) != 1:
        raise RuntimeError("repository must have exactly one protected root commit")
    return roots[0]


def _load_one_document(path: Path) -> dict[str, Any]:
    """Load exactly one plain JSON-compatible YAML mapping."""
    try:
        documents = list(
            yaml.load_all(path.read_text(encoding="utf-8"), Loader=_RestrictedYamlLoader)
        )
    except yaml.YAMLError as exc:
        raise SubmissionError(f"{path.name} must contain restricted YAML") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise SubmissionError(f"{path.name} must contain exactly one YAML mapping")
    return documents[0]


if __name__ == "__main__":
    raise SystemExit(main())

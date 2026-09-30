"""Coldline.

===================

File:              tests/contract/test_submission.py
Component:         Contract tests — Test Submission
Purpose:           Tests for the public answer and path checks for this Task's submission.
Interacts With:    Published interfaces and repository boundaries
Sprint/Task:       Sprint 3 — Project 3
Concepts:          Compatibility, ownership, export safety
Tools:             Python 3.12, pytest
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.contract.submission_validation import (
    SubmissionError,
    _load_one_document,
    main,
    validate_changed_paths,
    validate_submission,
)

ROOT = Path(__file__).parents[2]
SCHEMA = ROOT / "docs/contracts/submission.schema.json"
RECORD = "docs/student/task-3-10-cache-record.md"
PERMITTED = (
    "src/api/status_cache.py",
    "src/worker/status_invalidation.py",
    "config/cache.yaml",
    "docs/student/cache/probe.json",
    RECORD,
    "submission.yaml",
)


def _window(**overrides: Any) -> dict[str, Any]:
    """Return one well-formed freshness window."""
    entry: dict[str, Any] = {
        "reads_that_may_be_stale": "none",
        "max_staleness_seconds": 30,
        "acceptable_for_recipient_acceptance": False,
        "note": "Fictional format example; it states no conclusion about the status view.",
    }
    entry.update(overrides)
    return entry


def valid_answers(**overrides: Any) -> dict[str, object]:
    """Return a complete answer sheet in the published shape."""
    answers: dict[str, Any] = {
        "status_ttl_seconds": 30,
        "first_read_outcome": "miss",
        "second_read_outcome": "hit",
        "post_update_read_outcome": "miss",
        "post_update_read_state": "COMPLETED",
        "stale_reads_after_update": 0,
        "freshness_window": _window(),
    }
    answers.update(overrides)
    return {"answers": answers}


def _task_root(tmp_path: Path, submission_text: str) -> Path:
    """Stage a minimal Task root the public verifier can validate."""
    (tmp_path / "docs/contracts").mkdir(parents=True)
    (tmp_path / "submission.yaml").write_text(submission_text, encoding="utf-8")
    (tmp_path / "submission-sample.yaml").write_text(
        (ROOT / "submission-sample.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "docs/contracts/submission.schema.json").write_text(
        SCHEMA.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


def _with_record(root: Path, text: str) -> None:
    """Place a cache record with the given text beside the staged answer sheet."""
    (root / "docs/student").mkdir(parents=True, exist_ok=True)
    (root / RECORD).write_text(text, encoding="utf-8")


def test_a_complete_sheet_is_well_formed(tmp_path: Path) -> None:
    """The public schema accepts a complete sheet without judging its correctness."""
    root = _task_root(tmp_path, yaml.safe_dump(valid_answers()))

    validate_submission(root / "submission.yaml", SCHEMA)


def test_blank_template_fails_with_field_address(tmp_path: Path) -> None:
    """An untouched answer sheet must identify the first incomplete field."""
    root = _task_root(
        tmp_path, (ROOT / "tests/fixtures/submission-template.yaml").read_text(encoding="utf-8")
    )

    with pytest.raises(SubmissionError, match="answers.first_read_outcome"):
        validate_submission(root / "submission.yaml", SCHEMA)


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"status_ttl_seconds": 4}, "status_ttl_seconds"),
        ({"status_ttl_seconds": 121}, "status_ttl_seconds"),
        ({"status_ttl_seconds": 30.5}, "status_ttl_seconds"),
        ({"status_ttl_seconds": "thirty"}, "status_ttl_seconds"),
        ({"first_read_outcome": "cached"}, "first_read_outcome"),
        ({"second_read_outcome": "HIT"}, "second_read_outcome"),
        ({"post_update_read_outcome": True}, "post_update_read_outcome"),
        ({"post_update_read_state": "completed"}, "post_update_read_state"),
        ({"post_update_read_state": "DONE"}, "post_update_read_state"),
        ({"stale_reads_after_update": -1}, "stale_reads_after_update"),
        ({"stale_reads_after_update": 1.5}, "stale_reads_after_update"),
        ({"stale_reads_after_update": "none"}, "stale_reads_after_update"),
    ],
    ids=[
        "ttl-below-range",
        "ttl-above-range",
        "ttl-fraction",
        "ttl-prose",
        "unknown-outcome",
        "uppercase-outcome",
        "boolean-outcome",
        "lowercase-state",
        "unknown-state",
        "negative-stale-count",
        "fractional-stale-count",
        "prose-stale-count",
    ],
)
def test_values_outside_the_published_contract_are_rejected(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    """The public schema must name the field it rejected, and reject the right ones."""
    root = _task_root(tmp_path, yaml.safe_dump(valid_answers(**overrides)))

    with pytest.raises(SubmissionError, match=message):
        validate_submission(root / "submission.yaml", SCHEMA)


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"reads_that_may_be_stale": "some"}, "reads_that_may_be_stale"),
        ({"max_staleness_seconds": -1}, "max_staleness_seconds"),
        ({"max_staleness_seconds": 2.5}, "max_staleness_seconds"),
        ({"acceptable_for_recipient_acceptance": "yes"}, "acceptable_for_recipient_acceptance"),
        ({"note": "n" * 401}, "freshness_window.note"),
        ({"note": ""}, "freshness_window.note"),
    ],
    ids=[
        "unknown-choice",
        "negative-staleness",
        "fractional-staleness",
        "prose-boolean",
        "long-note",
        "blank-note",
    ],
)
def test_freshness_window_outside_the_published_contract_is_rejected(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    """A nested enumeration, number, boolean, or note cannot hide behind the top-level checks."""
    root = _task_root(
        tmp_path, yaml.safe_dump(valid_answers(freshness_window=_window(**overrides)))
    )

    with pytest.raises(SubmissionError, match=message):
        validate_submission(root / "submission.yaml", SCHEMA)


def test_a_missing_window_field_is_rejected(tmp_path: Path) -> None:
    """All four window fields are required; three of them is not a window."""
    window = _window()
    del window["acceptable_for_recipient_acceptance"]
    root = _task_root(tmp_path, yaml.safe_dump(valid_answers(freshness_window=window)))

    with pytest.raises(SubmissionError, match="acceptable_for_recipient_acceptance"):
        validate_submission(root / "submission.yaml", SCHEMA)


@pytest.mark.parametrize(
    "field",
    ["probe_passed", "instructor_approved", "defense_recording_url", "notes"],
)
def test_no_self_attestation_or_recording_field_is_accepted(tmp_path: Path, field: str) -> None:
    """Reject a self-approval, a pass boolean, or a recording URL."""
    answers = valid_answers()
    mapping = answers["answers"]
    assert isinstance(mapping, dict)
    mapping[field] = True
    root = _task_root(tmp_path, yaml.safe_dump(answers))

    with pytest.raises(SubmissionError, match="Additional properties"):
        validate_submission(root / "submission.yaml", SCHEMA)


def test_exact_sample_copy_is_rejected(tmp_path: Path) -> None:
    """The published sample must not be accepted as a student submission."""
    root = _task_root(tmp_path, (ROOT / "submission-sample.yaml").read_text(encoding="utf-8"))

    with pytest.raises(SubmissionError, match="fictional sample"):
        validate_submission(
            root / "submission.yaml",
            SCHEMA,
            sample_path=root / "submission-sample.yaml",
        )


def test_only_the_six_permitted_paths_may_change() -> None:
    """The two wiring points, the TTL, the probe output, the record, the sheet; nothing else."""
    validate_changed_paths(list(PERMITTED))

    for protected in (
        "src/api/routes.py",
        "src/api/bootstrap.py",
        "src/worker/runtime.py",
        "src/worker/bootstrap.py",
        "src/adapters/cache/redis_status_cache.py",
        "src/adapters/cache/memory.py",
        "src/domain/status_cache.py",
        "infra/cache/cache_probe.py",
        "compose.yaml",
        "config/student/retrieval.yaml",
        "docs/student/cache/notes.json",
        "tests/contract/test_cache_contract.py",
        "tests/student/test_my_cache.py",
        ".github/workflows/task.yml",
        "docs/student/runbook.md",
        "pyproject.toml",
        "README.md",
    ):
        with pytest.raises(SubmissionError, match="protected path changed"):
            validate_changed_paths([protected])


def test_public_entrypoint_reports_an_incomplete_answer_sheet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catch a verifier entrypoint that skips the real submission contract."""
    root = _task_root(
        tmp_path, (ROOT / "tests/fixtures/submission-template.yaml").read_text(encoding="utf-8")
    )
    _with_record(root, (ROOT / RECORD).read_text(encoding="utf-8"))

    assert main(root, changed_paths=[]) == 1
    assert "answers.first_read_outcome is incomplete" in capsys.readouterr().err


def test_public_entrypoint_rejects_an_untouched_cache_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A complete answer sheet with the template record still in place is incomplete."""
    root = _task_root(tmp_path, yaml.safe_dump(valid_answers()))
    _with_record(root, (ROOT / RECORD).read_text(encoding="utf-8"))

    assert main(root, changed_paths=[]) == 1
    assert "template markers" in capsys.readouterr().err


def test_public_entrypoint_accepts_a_completed_sheet_and_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With every marker replaced and the paths inside the boundary, the check passes."""
    root = _task_root(tmp_path, yaml.safe_dump(valid_answers()))
    _with_record(root, "# Task 3.10 cache record\n\n## Step 1\n\nMy own TTL and reason.\n")

    assert main(root, changed_paths=list(PERMITTED)) == 0
    assert "Task 3.10 answer verification passed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "answers: {value: first, value: second}\n",
        "answers: &answer {value: fictional}\n",
        "answers: *missing\n",
        "answers: {<<: {value: fictional}}\n",
        "answers: {value: 2026-09-04}\n",
        "answers: {value: !custom fictional}\n",
        "answers: {1: fictional}\n",
    ],
    ids=["duplicate-key", "anchor", "alias", "merge-key", "date", "custom-tag", "non-string-key"],
)
def test_non_json_yaml_constructs_are_rejected(tmp_path: Path, unsafe_text: str) -> None:
    """Reject restricted syntax before schema validation can mask a parser defect."""
    submission = tmp_path / "submission.yaml"
    submission.write_text(unsafe_text, encoding="utf-8")

    with pytest.raises(SubmissionError, match="restricted YAML"):
        _load_one_document(submission)


def test_multiple_yaml_documents_are_rejected(tmp_path: Path) -> None:
    """A second document cannot supply or replace the answer mapping."""
    submission = tmp_path / "submission.yaml"
    submission.write_text("answers: {}\n---\nanswers: {}\n", encoding="utf-8")

    with pytest.raises(SubmissionError, match="exactly one YAML mapping"):
        _load_one_document(submission)

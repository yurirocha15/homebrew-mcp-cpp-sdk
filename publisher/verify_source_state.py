#!/usr/bin/env python3
"""Verify the immutable source release and the distinct dispatch workflow run."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import quote


SOURCE_REPOSITORY = "yurirocha15/mcp-cpp-sdk"
SOURCE_WORKFLOW = ".github/workflows/release.yml"
SHA = re.compile(r"[0-9a-f]{40}")
DECIMAL = re.compile(r"[1-9][0-9]*")


class Api(Protocol):
    def __call__(self, endpoint: str) -> object: ...


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"GitHub response repeats field: {key}")
        result[key] = value
    return result


class GhApi:
    """Small read-only GitHub API boundary with a fixed API version."""

    def __call__(self, endpoint: str) -> object:
        if not endpoint.startswith("repos/"):
            raise ValueError("GitHub endpoint is outside the approved repository API")
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                "-H",
                "X-GitHub-Api-Version: 2026-03-10",
                endpoint,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            return json.loads(
                result.stdout.decode("utf-8"), object_pairs_hook=_strict_pairs
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("GitHub returned malformed JSON") from error


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is not a JSON object")
    return value


def verify(
    api: Api,
    *,
    source_repository_id: int,
    source_owner_id: int,
    source_tag: str,
    source_workflow_head_sha: str,
    github_release_id: int,
    source_workflow_run_id: int,
    attempts: int = 30,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Fail unless live public state proves the exact immutable source handoff."""

    if min(source_repository_id, source_owner_id, github_release_id, source_workflow_run_id) < 1:
        raise ValueError("GitHub numeric identities must be positive")
    if SHA.fullmatch(source_workflow_head_sha) is None:
        raise ValueError("source workflow head is not a full commit SHA")
    if attempts < 1 or attempts > 30:
        raise ValueError("source run polling attempt count is invalid")

    repository = _mapping(api(f"repos/{SOURCE_REPOSITORY}"), "source repository")
    owner = _mapping(repository.get("owner"), "source repository owner")
    if (
        repository.get("id") != source_repository_id
        or repository.get("full_name") != SOURCE_REPOSITORY
        or repository.get("default_branch") != "main"
        or repository.get("archived") is not False
        or repository.get("disabled") is not False
        or owner.get("id") != source_owner_id
    ):
        raise ValueError("source repository identity or state differs from policy")

    release = _mapping(
        api(f"repos/{SOURCE_REPOSITORY}/releases/tags/{quote(source_tag, safe='')}"),
        "source release",
    )
    if (
        release.get("id") != github_release_id
        or release.get("tag_name") != source_tag
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or release.get("immutable") is not True
    ):
        raise ValueError("source release is not the exact immutable stable release")

    endpoint = f"repos/{SOURCE_REPOSITORY}/actions/runs/{source_workflow_run_id}"
    run: Mapping[str, object] = {}
    for attempt in range(attempts):
        run = _mapping(api(endpoint), "source workflow run")
        status = run.get("status")
        if status == "completed":
            break
        if status not in {"queued", "in_progress"}:
            raise ValueError("source workflow run has an invalid status")
        if attempt + 1 < attempts:
            sleeper(10)
    repository_state = _mapping(run.get("repository"), "source run repository")
    if (
        run.get("id") != source_workflow_run_id
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
        or run.get("head_sha") != source_workflow_head_sha
        or run.get("path") != SOURCE_WORKFLOW
        or repository_state.get("id") != source_repository_id
        or repository_state.get("full_name") != SOURCE_REPOSITORY
    ):
        raise ValueError("source workflow run identity or conclusion differs from handoff")


def _positive(value: str, label: str) -> int:
    if DECIMAL.fullmatch(value) is None:
        raise ValueError(f"{label} is not a positive canonical decimal")
    return int(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repository-id", type=int, required=True)
    parser.add_argument("--source-owner-id", type=int, required=True)
    parser.add_argument("--source-tag", required=True)
    parser.add_argument("--source-workflow-head-sha", required=True)
    parser.add_argument("--github-release-id", required=True)
    parser.add_argument("--source-workflow-run-id", required=True)
    args = parser.parse_args(argv)
    try:
        verify(
            GhApi(),
            source_repository_id=args.source_repository_id,
            source_owner_id=args.source_owner_id,
            source_tag=args.source_tag,
            source_workflow_head_sha=args.source_workflow_head_sha,
            github_release_id=_positive(args.github_release_id, "GitHub release ID"),
            source_workflow_run_id=_positive(
                args.source_workflow_run_id, "source workflow run ID"
            ),
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"verify-source-state: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

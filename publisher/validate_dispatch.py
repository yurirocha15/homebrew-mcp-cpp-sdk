#!/usr/bin/env python3
"""Validate the trusted Homebrew workflow's exact dispatch contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from uuid import NAMESPACE_URL, UUID, uuid5


FIELDS = (
    "source_tag",
    "source_commit_sha",
    "source_workflow_head_sha",
    "provider_control_sha",
    "github_release_id",
    "source_workflow_run_id",
    "release_manifest_sha256",
    "request_uuid",
)
TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
DECIMAL = re.compile(r"[1-9][0-9]*")
SOURCE_REPOSITORY = "yurirocha15/mcp-cpp-sdk"
PUBLISHER_REPOSITORY = "yurirocha15/homebrew-mcp-cpp-sdk"
CONTROL_TAG = "release-control-v2"
WORKFLOW = "publish-bottles.yml"


def expected_request_uuid(source_tag: str, manifest_sha256: str) -> str:
    """Return the source dispatcher's deterministic Homebrew request identity."""

    return str(
        uuid5(
            NAMESPACE_URL,
            f"{SOURCE_REPOSITORY}:{source_tag}:{manifest_sha256}:homebrew",
        )
    )


def validate(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError("dispatch must contain exactly the approved fields")
    result: dict[str, str] = {}
    for field in FIELDS:
        item = value[field]
        if not isinstance(item, str) or not item or len(item) > 160:
            raise ValueError(f"{field} is not a bounded non-empty string")
        if any(character.isspace() or ord(character) < 0x20 for character in item):
            raise ValueError(f"{field} contains whitespace or control characters")
        result[field] = item
    if TAG.fullmatch(result["source_tag"]) is None:
        raise ValueError("source_tag is not a canonical stable tag")
    for field in (
        "source_commit_sha",
        "source_workflow_head_sha",
        "provider_control_sha",
    ):
        if SHA.fullmatch(result[field]) is None:
            raise ValueError(f"{field} is not a lowercase full commit SHA")
    if DIGEST.fullmatch(result["release_manifest_sha256"]) is None:
        raise ValueError("release_manifest_sha256 is not canonical")
    for field in ("github_release_id", "source_workflow_run_id"):
        if DECIMAL.fullmatch(result[field]) is None:
            raise ValueError(f"{field} is not a positive canonical decimal")
    try:
        request_id = UUID(result["request_uuid"])
    except ValueError as error:
        raise ValueError("request_uuid is invalid") from error
    if str(request_id) != result["request_uuid"]:
        raise ValueError("request_uuid is not canonical lowercase")
    if result["request_uuid"] != expected_request_uuid(
        result["source_tag"], result["release_manifest_sha256"]
    ):
        raise ValueError("request_uuid does not bind the exact Homebrew request")
    return result


def validate_execution_context(value: dict[str, str], environment: dict[str, str]) -> None:
    """Bind the request to this repository's immutable control workflow."""

    expected = {
        "GITHUB_REPOSITORY": PUBLISHER_REPOSITORY,
        "GITHUB_REF": f"refs/tags/{CONTROL_TAG}",
        "GITHUB_SHA": value["provider_control_sha"],
        "GITHUB_WORKFLOW_REF": (
            f"{PUBLISHER_REPOSITORY}/.github/workflows/{WORKFLOW}"
            f"@refs/tags/{CONTROL_TAG}"
        ),
    }
    for name, expected_value in expected.items():
        if environment.get(name) != expected_value:
            raise ValueError(f"{name} does not match immutable provider control")


def main() -> int:
    try:
        value = validate(json.loads(os.environ["DISPATCH_JSON"]))
        validate_execution_context(value, os.environ)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            for field in FIELDS:
                output.write(f"{field}={value[field]}\n")
    except (KeyError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"validate-dispatch: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

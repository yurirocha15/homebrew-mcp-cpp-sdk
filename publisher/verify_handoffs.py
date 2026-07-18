#!/usr/bin/env python3
"""Verify same-run bottle handoffs and produce a canonical publication ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


DIGEST = re.compile(r"[0-9a-f]{64}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(
    directory: Path, run_id: str, run_attempt: str, expected_tags: set[str] | None = None
) -> dict[str, object]:
    handoffs = []
    seen_tags = set()
    for path in sorted(directory.glob("*/handoff.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        expected = {"schema_version", "run_id", "run_attempt", "bottle_tag", "bottle_file", "bottle_sha256", "json_file", "json_sha256"}
        if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != 1:
            raise ValueError(f"invalid handoff schema: {path}")
        if value["run_id"] != run_id or value["run_attempt"] != run_attempt:
            raise ValueError(f"handoff belongs to another workflow run: {path}")
        tag = value["bottle_tag"]
        if not isinstance(tag, str) or not re.fullmatch(r"[a-z0-9_]+", tag) or tag in seen_tags:
            raise ValueError(f"invalid or duplicate bottle tag: {path}")
        seen_tags.add(tag)
        for name_field, digest_field in (("bottle_file", "bottle_sha256"), ("json_file", "json_sha256")):
            name = value[name_field]
            digest = value[digest_field]
            if not isinstance(name, str) or Path(name).name != name or not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
                raise ValueError(f"invalid artifact identity: {path}")
            artifact = path.parent / name
            if sha256(artifact) != digest:
                raise ValueError(f"artifact digest mismatch: {artifact}")
        metadata = json.loads((path.parent / value["json_file"]).read_text(encoding="utf-8"))
        serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        if tag not in serialized or value["bottle_sha256"] not in serialized:
            raise ValueError(f"bottle metadata does not bind its tag and archive digest: {path}")
        if "https://ghcr.io/v2/yurirocha15/mcp-cpp-sdk" not in serialized:
            raise ValueError(f"bottle metadata has an unexpected root URL: {path}")
        handoffs.append(value)
    if not handoffs:
        raise ValueError("no bottle handoffs found")
    if expected_tags is not None and seen_tags != expected_tags:
        raise ValueError("bottle tag set does not match the approved matrix")
    return {"schema_version": 1, "run_id": run_id, "run_attempt": run_attempt, "bottles": handoffs}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--expected-tag", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        expected = set(args.expected_tag) if args.expected_tag else None
        ledger = verify(args.directory, args.run_id, args.run_attempt, expected)
        args.output.write_text(json.dumps(ledger, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"verify-handoffs: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

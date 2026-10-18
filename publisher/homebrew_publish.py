#!/usr/bin/env python3
"""Idempotently publish verified Homebrew bottles and their formula pull request."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Mapping, Protocol, Sequence
from urllib.parse import quote, urlencode


REPOSITORY = "yurirocha15/homebrew-mcp-cpp-sdk"
PACKAGE = "yurirocha15/mcp-cpp-sdk/mcp-cpp-sdk"
FORMULA_PATH = "Formula/mcp-cpp-sdk.rb"
BOTTLE_TAGS = {"sequoia", "arm64_sequoia", "x86_64_linux", "arm64_linux"}
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class Api(Protocol):
    def request(
        self,
        method: str,
        endpoint: str,
        payload: Mapping[str, object] | None = None,
        *,
        allow_missing: bool = False,
    ) -> object | None: ...


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON response repeats field: {key}")
        result[key] = value
    return result


class GitHubApi:
    """Bounded GitHub REST client that never places credentials on a command line."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("GitHub token is missing")
        self._token = token

    def request(
        self,
        method: str,
        endpoint: str,
        payload: Mapping[str, object] | None = None,
        *,
        allow_missing: bool = False,
    ) -> object | None:
        if method not in {"GET", "POST", "PUT"} or not endpoint.startswith("repos/"):
            raise ValueError("invalid internal GitHub API request")
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "mcp-cpp-sdk-homebrew-publisher/1",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        if payload is not None:
            body = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPSConnection("api.github.com", timeout=30)
        try:
            connection.request(method, f"/{endpoint}", body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
        finally:
            connection.close()
        if len(raw) > 4 * 1024 * 1024 or 300 <= response.status < 400:
            raise ValueError("GitHub response is oversized or redirected")
        if response.status == 404 and allow_missing:
            return None
        expected = {"GET": {200}, "POST": {200, 201}, "PUT": {200, 201}}[method]
        if response.status not in expected:
            raise ValueError(f"GitHub API request failed with HTTP {response.status}")
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("GitHub returned malformed JSON") from error


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is not an object")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_pairs)


def load_bottles(path: Path, *, run_id: str, run_attempt: str) -> list[dict[str, str]]:
    value = _load_json(path)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "run_id", "run_attempt", "bottles"}
        or value.get("schema_version") != 1
        or value.get("run_id") != run_id
        or not isinstance(value.get("run_attempt"), str)
        or re.fullmatch(r"[1-9][0-9]*", value["run_attempt"]) is None
        or int(value["run_attempt"]) > int(run_attempt)
        or not isinstance(value.get("bottles"), list)
    ):
        raise ValueError("bottle ledger schema or workflow identity is invalid")
    bottles: list[dict[str, str]] = []
    for item in value["bottles"]:
        if not isinstance(item, dict) or set(item) != {
            "schema_version", "run_id", "run_attempt", "bottle_tag", "bottle_file",
            "bottle_sha256", "json_file", "json_sha256",
        }:
            raise ValueError("bottle ledger entry schema is invalid")
        string_fields = {
            "run_id", "run_attempt", "bottle_tag", "bottle_file", "bottle_sha256",
            "json_file", "json_sha256",
        }
        if any(not isinstance(item[field], str) for field in string_fields):
            raise ValueError("bottle ledger entry fields are not strings")
        if (
            item["schema_version"] != 1
            or item["run_id"] != run_id
            or re.fullmatch(r"[1-9][0-9]*", item["run_attempt"]) is None
            or int(item["run_attempt"]) > int(run_attempt)
        ):
            raise ValueError("bottle ledger entry belongs to another workflow run")
        if (
            item["bottle_tag"] not in BOTTLE_TAGS
            or Path(item["bottle_file"]).name != item["bottle_file"]
            or SHA256.fullmatch(item["bottle_sha256"]) is None
            or Path(item["json_file"]).name != item["json_file"]
            or SHA256.fullmatch(item["json_sha256"]) is None
        ):
            raise ValueError("bottle ledger entry identity is malformed")
        bottles.append(dict(item))
    if {item["bottle_tag"] for item in bottles} != BOTTLE_TAGS or len(bottles) != 4:
        raise ValueError("bottle ledger tag inventory is not exact")
    return sorted(bottles, key=lambda item: item["bottle_tag"])


def verify_sha256sums(directory: Path) -> None:
    records: dict[str, str] = {}
    for line in (directory / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)", line)
        if match is None or match.group(2) in records or match.group(2) == "SHA256SUMS":
            raise ValueError("publication checksum record is malformed or duplicated")
        records[match.group(2)] = match.group(1)
    files = {path.name for path in directory.iterdir() if path.is_file()} - {"SHA256SUMS"}
    if set(records) != files:
        raise ValueError("publication checksum inventory is not exact")
    for name, digest in records.items():
        if _sha256_bytes((directory / name).read_bytes()) != digest:
            raise ValueError(f"publication checksum differs: {name}")


def release_branch(source_tag: str) -> str:
    if re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", source_tag) is None:
        raise ValueError("source tag is not a canonical stable version")
    return f"release/mcp-cpp-sdk-{source_tag}"


def _content(api: Api, branch: str) -> tuple[str, bytes] | None:
    endpoint = (
        f"repos/{REPOSITORY}/contents/{FORMULA_PATH}?"
        + urlencode({"ref": branch})
    )
    value = api.request("GET", endpoint, allow_missing=True)
    if value is None:
        return None
    item = _mapping(value, "formula content")
    if (
        item.get("path") != FORMULA_PATH
        or item.get("encoding") != "base64"
        or not isinstance(item.get("sha"), str)
        or SHA1.fullmatch(item["sha"]) is None
        or not isinstance(item.get("content"), str)
    ):
        raise ValueError("GitHub formula content response is malformed")
    try:
        content = base64.b64decode(item["content"], validate=True)
    except ValueError as error:
        raise ValueError("GitHub formula content is not canonical base64") from error
    return str(item["sha"]), content


def _pulls(api: Api, branch: str) -> list[Mapping[str, object]]:
    query = urlencode({"state": "all", "head": f"yurirocha15:{branch}", "base": "main", "per_page": "100"})
    value = api.request("GET", f"repos/{REPOSITORY}/pulls?{query}")
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError("GitHub pull request response is malformed")
    if len(value) > 1:
        raise ValueError("multiple release pull requests use the deterministic branch")
    return value


def publication_body(
    *, source_tag: str, request_uuid: str, manifest_sha256: str, formula_sha256: str,
    manifests: list[dict[str, str]],
) -> str:
    value = {
        "formula_sha256": formula_sha256,
        "ghcr_manifests": manifests,
        "manifest_sha256": manifest_sha256,
        "request_uuid": request_uuid,
        "schema_version": 1,
        "source_tag": source_tag,
    }
    return "<!-- mcp-cpp-sdk-release " + json.dumps(value, sort_keys=True, separators=(",", ":")) + " -->"


def plan_formula_pr(
    api: Api, *, branch: str, formula: bytes, expected_body: str | None = None
) -> tuple[str, Mapping[str, object] | None]:
    """Validate existing state and return the base SHA plus an optional exact PR."""

    main_ref = _mapping(api.request("GET", f"repos/{REPOSITORY}/git/ref/heads/main"), "main ref")
    main_object = _mapping(main_ref.get("object"), "main ref object")
    base_sha = main_object.get("sha")
    if main_ref.get("ref") != "refs/heads/main" or not isinstance(base_sha, str) or SHA1.fullmatch(base_sha) is None:
        raise ValueError("protected main reference is malformed")
    branch_ref = api.request(
        "GET", f"repos/{REPOSITORY}/git/ref/heads/{quote(branch, safe='')}", allow_missing=True
    )
    pulls = _pulls(api, branch)
    pull = pulls[0] if pulls else None
    if branch_ref is None:
        if pull is not None:
            raise ValueError("release pull request exists without its deterministic branch")
        return base_sha, None
    ref = _mapping(branch_ref, "release branch ref")
    ref_object = _mapping(ref.get("object"), "release branch object")
    head_sha = ref_object.get("sha")
    if ref.get("ref") != f"refs/heads/{branch}" or not isinstance(head_sha, str) or SHA1.fullmatch(head_sha) is None:
        raise ValueError("release branch reference is malformed")
    current = _content(api, branch)
    if current is None or current[1] != formula:
        if head_sha != base_sha or pull is not None:
            raise ValueError("release branch contains conflicting state")
        return base_sha, None
    if pull is None:
        if head_sha == base_sha:
            raise ValueError("published formula already exists on main without release evidence")
        return base_sha, None
    head = _mapping(pull.get("head"), "pull request head")
    base = _mapping(pull.get("base"), "pull request base")
    head_repository = _mapping(head.get("repo"), "pull request head repository")
    author = _mapping(pull.get("user"), "pull request author")
    if (
        (
            pull.get("state") != "open"
            and not (pull.get("state") == "closed" and pull.get("merged") is True)
        )
        or base.get("ref") != "main"
        or head.get("ref") != branch
        or head.get("sha") != head_sha
        or head_repository.get("full_name") != REPOSITORY
        or author.get("type") != "Bot"
        or author.get("login") != "mcp-cpp-sdk-homebrew-publisher[bot]"
        or pull.get("title") != f"brew: publish {branch.removeprefix('release/mcp-cpp-sdk-')}"
        or (expected_body is not None and pull.get("body") != expected_body)
    ):
        raise ValueError("release pull request differs from deterministic publication")
    return base_sha, pull


def _package_records(value: object, bottles: list[dict[str, str]], version: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or any(not isinstance(page, list) for page in value):
        raise ValueError("GitHub package version pages are malformed")
    versions = [item for page in value for item in page]
    records: list[dict[str, str]] = []
    for bottle in bottles:
        tag = f"{version}.{bottle['bottle_tag']}"
        matches = []
        for item in versions:
            if not isinstance(item, Mapping):
                raise ValueError("GitHub package version is not an object")
            metadata = _mapping(item.get("metadata"), "package metadata")
            container = _mapping(metadata.get("container"), "package container metadata")
            tags = container.get("tags")
            if not isinstance(tags, list) or any(not isinstance(value, str) for value in tags):
                raise ValueError("GitHub package version tags are malformed")
            if tag in tags:
                matches.append(item)
        if len(matches) > 1:
            raise ValueError(f"multiple GHCR manifests have tag {tag}")
        if matches:
            digest = matches[0].get("name")
            if not isinstance(digest, str) or OCI_DIGEST.fullmatch(digest) is None:
                raise ValueError("GHCR package version digest is malformed")
            records.append({
                "bottle_sha256": bottle["bottle_sha256"],
                "digest": digest,
                "tag": tag,
            })
    if len(records) not in {0, len(bottles)}:
        raise ValueError("GHCR contains a partial conflicting bottle publication")
    return sorted(records, key=lambda item: item["tag"])


def _package_pages(token: str) -> object:
    env = dict(os.environ, GH_TOKEN=token)
    result = subprocess.run(
        [
            "gh", "api", "--paginate", "--slurp", "-H",
            "X-GitHub-Api-Version: 2026-03-10",
            "/users/yurirocha15/packages/container/mcp-cpp-sdk%2Fmcp-cpp-sdk/versions?per_page=100",
        ],
        check=True, capture_output=True, env=env, timeout=60,
    )
    return json.loads(result.stdout.decode("utf-8"), object_pairs_hook=_strict_pairs)


def _upload_bottles(directory: Path, token: str) -> None:
    subprocess.run(
        ["docker", "login", "ghcr.io", "-u", "yurirocha15", "--password-stdin"],
        input=token.encode(), check=True, capture_output=True, timeout=60,
    )
    metadata = sorted(str(path) for path in directory.glob("*.bottle.json"))
    if len(metadata) != 4:
        raise ValueError("publication bundle does not contain four bottle metadata files")
    env = dict(os.environ, HOMEBREW_GITHUB_API_TOKEN=token)
    subprocess.run(
        ["brew", "pr-upload", f"--root-url=https://ghcr.io/v2/yurirocha15/mcp-cpp-sdk", *metadata],
        check=True, capture_output=True, env=env, timeout=600,
    )


def _write_formula_and_pr(
    api: Api, *, branch: str, base_sha: str, formula: bytes, body: str
) -> tuple[str, str]:
    branch_endpoint = f"repos/{REPOSITORY}/git/ref/heads/{quote(branch, safe='')}"
    if api.request("GET", branch_endpoint, allow_missing=True) is None:
        api.request("POST", f"repos/{REPOSITORY}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
    current = _content(api, branch)
    if current is None or current[1] != formula:
        payload: dict[str, object] = {
            "message": f"brew: publish {branch.removeprefix('release/mcp-cpp-sdk-')}",
            "content": base64.b64encode(formula).decode("ascii"),
            "branch": branch,
        }
        if current is not None:
            payload["sha"] = current[0]
        response = _mapping(
            api.request("PUT", f"repos/{REPOSITORY}/contents/{FORMULA_PATH}", payload),
            "formula update",
        )
        commit = _mapping(response.get("commit"), "formula commit")
        head_sha = commit.get("sha")
    else:
        ref = _mapping(api.request("GET", branch_endpoint), "release branch ref")
        head_sha = _mapping(ref.get("object"), "release branch object").get("sha")
    if not isinstance(head_sha, str) or SHA1.fullmatch(head_sha) is None:
        raise ValueError("formula update did not return a full commit SHA")
    pulls = _pulls(api, branch)
    if pulls:
        pull = pulls[0]
    else:
        pull = _mapping(
            api.request(
                "POST", f"repos/{REPOSITORY}/pulls",
                {
                    "title": f"brew: publish {branch.removeprefix('release/mcp-cpp-sdk-')}",
                    "head": branch,
                    "base": "main",
                    "body": body,
                    "maintainer_can_modify": False,
                },
            ),
            "created pull request",
        )
    number = pull.get("number")
    if type(number) is not int or number < 1:
        raise ValueError("release pull request number is invalid")
    return str(number), head_sha


def publish(
    *, directory: Path, source_tag: str, request_uuid: str, manifest_sha256: str,
    run_id: str, run_attempt: str, api: Api, packages_token: str, output: Path,
) -> None:
    verify_sha256sums(directory)
    bottles = load_bottles(directory / "bottle-ledger.json", run_id=run_id, run_attempt=run_attempt)
    formula = (directory / "mcp-cpp-sdk.rb").read_bytes()
    formula_sha = _sha256_bytes(formula)
    branch = release_branch(source_tag)
    version = source_tag.removeprefix("v")
    manifests = _package_records(_package_pages(packages_token), bottles, version)
    base_sha, _ = plan_formula_pr(api, branch=branch, formula=formula)
    if not manifests:
        _upload_bottles(directory, packages_token)
        manifests = _package_records(_package_pages(packages_token), bottles, version)
        if len(manifests) != 4:
            raise ValueError("GHCR did not expose the complete uploaded bottle set")
    body = publication_body(
        source_tag=source_tag,
        request_uuid=request_uuid,
        manifest_sha256=manifest_sha256,
        formula_sha256=formula_sha,
        manifests=manifests,
    )
    # Recheck any existing PR now that the final body is known.
    _, existing = plan_formula_pr(api, branch=branch, formula=formula, expected_body=body)
    if existing is not None:
        number = str(existing["number"])
        head_sha = str(_mapping(existing["head"], "pull request head")["sha"])
    else:
        number, head_sha = _write_formula_and_pr(
            api, branch=branch, base_sha=base_sha, formula=formula, body=body
        )
    ledger = {
        "schema_version": 1,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "source_tag": source_tag,
        "request_uuid": request_uuid,
        "manifest_sha256": manifest_sha256,
        "formula_sha256": formula_sha,
        "formula_branch": branch,
        "formula_pr_number": number,
        "formula_head_sha": head_sha,
        "ghcr_manifests": manifests,
        "bottles": bottles,
    }
    output.write_text(json.dumps(ledger, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="ascii") as github_output:
        github_output.write(f"formula_pr_number={number}\n")
        github_output.write(f"formula_head_sha={head_sha}\n")


def _registry_get(host: str, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPSConnection(host, timeout=30)
    try:
        connection.request("GET", path, headers=dict(headers))
        response = connection.getresponse()
        body = response.read(4 * 1024 * 1024 + 1)
        response_headers = {key.lower(): value for key, value in response.getheaders()}
    finally:
        connection.close()
    if len(body) > 4 * 1024 * 1024 or 300 <= response.status < 400:
        raise ValueError("GHCR response is oversized or redirected")
    return response.status, response_headers, body


def verify_public_manifests(manifests: object) -> None:
    if not isinstance(manifests, list) or len(manifests) != 4:
        raise ValueError("finalization ledger has no exact GHCR manifest inventory")
    query = urlencode({"scope": f"repository:{PACKAGE}:pull", "service": "ghcr.io"})
    status, _, body = _registry_get(
        "ghcr.io", f"/token?{query}", {"User-Agent": "mcp-cpp-sdk-finalizer/1"}
    )
    if status != 200:
        raise ValueError("anonymous GHCR token request failed")
    try:
        token = _mapping(
            json.loads(body.decode("utf-8"), object_pairs_hook=_strict_pairs),
            "GHCR token response",
        ).get("token")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("anonymous GHCR token response is malformed") from error
    if not isinstance(token, str) or not token:
        raise ValueError("anonymous GHCR token response has no token")
    seen = set()
    for entry in manifests:
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"bottle_sha256", "digest", "tag"}
            or not isinstance(entry.get("digest"), str)
            or OCI_DIGEST.fullmatch(entry["digest"]) is None
            or not isinstance(entry.get("bottle_sha256"), str)
            or SHA256.fullmatch(entry["bottle_sha256"]) is None
            or not isinstance(entry.get("tag"), str)
            or entry["tag"] in seen
        ):
            raise ValueError("GHCR manifest identity is malformed or duplicated")
        seen.add(entry["tag"])
        status, headers, manifest_body = _registry_get(
            "ghcr.io",
            f"/v2/{PACKAGE}/manifests/{entry['digest']}",
            {
                "Accept": "application/vnd.oci.image.manifest.v1+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "mcp-cpp-sdk-finalizer/1",
            },
        )
        if status != 200 or headers.get("docker-content-digest") != entry["digest"]:
            raise ValueError("anonymous GHCR manifest digest differs from publication")
        try:
            manifest = _mapping(
                json.loads(manifest_body.decode("utf-8"), object_pairs_hook=_strict_pairs),
                "GHCR manifest",
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("GHCR manifest is malformed JSON") from error
        layers = manifest.get("layers")
        expected_layer = f"sha256:{entry['bottle_sha256']}"
        if (
            manifest.get("schemaVersion") != 2
            or not isinstance(layers, list)
            or sum(
                1
                for layer in layers
                if isinstance(layer, Mapping) and layer.get("digest") == expected_layer
            )
            != 1
        ):
            raise ValueError("GHCR manifest does not contain the exact bottle archive")


def finalize(*, ledger_path: Path, api: Api) -> None:
    ledger = _load_json(ledger_path)
    fields = {
        "schema_version", "run_id", "run_attempt", "source_tag", "request_uuid",
        "manifest_sha256", "formula_sha256", "formula_branch", "formula_pr_number",
        "formula_head_sha", "ghcr_manifests", "bottles",
    }
    if (
        not isinstance(ledger, dict)
        or set(ledger) != fields
        or ledger.get("schema_version") != 1
        or SHA256.fullmatch(str(ledger.get("formula_sha256", ""))) is None
        or SHA1.fullmatch(str(ledger.get("formula_head_sha", ""))) is None
        or not str(ledger.get("formula_pr_number", "")).isdigit()
    ):
        raise ValueError("finalization ledger schema is not exact")
    verify_public_manifests(ledger["ghcr_manifests"])
    pr_number = str(ledger["formula_pr_number"])
    pull = _mapping(
        api.request("GET", f"repos/{REPOSITORY}/pulls/{pr_number}"),
        "release pull request",
    )
    head = _mapping(pull.get("head"), "pull request head")
    base = _mapping(pull.get("base"), "pull request base")
    head_repository = _mapping(head.get("repo"), "pull request head repository")
    author = _mapping(pull.get("user"), "pull request author")
    expected_body = publication_body(
        source_tag=str(ledger["source_tag"]),
        request_uuid=str(ledger["request_uuid"]),
        manifest_sha256=str(ledger["manifest_sha256"]),
        formula_sha256=str(ledger["formula_sha256"]),
        manifests=list(ledger["ghcr_manifests"]),
    )
    if (
        base.get("ref") != "main"
        or head.get("ref") != ledger["formula_branch"]
        or head.get("sha") != ledger["formula_head_sha"]
        or head_repository.get("full_name") != REPOSITORY
        or author.get("type") != "Bot"
        or author.get("login") != "mcp-cpp-sdk-homebrew-publisher[bot]"
        or pull.get("body") != expected_body
        or pull.get("title") != f"brew: publish {ledger['source_tag']}"
    ):
        raise ValueError("release pull request differs from finalization ledger")
    if pull.get("state") == "open" and pull.get("merged") is False:
        merged = _mapping(
            api.request(
                "PUT",
                f"repos/{REPOSITORY}/pulls/{pr_number}/merge",
                {"merge_method": "squash", "sha": ledger["formula_head_sha"]},
            ),
            "pull request merge",
        )
        if merged.get("merged") is not True:
            raise ValueError("GitHub refused the exact formula pull request merge")
    elif pull.get("state") != "closed" or pull.get("merged") is not True:
        raise ValueError("release pull request is neither open nor already merged")
    main_formula = _content(api, "main")
    if main_formula is None or _sha256_bytes(main_formula[1]) != ledger["formula_sha256"]:
        raise ValueError("protected main does not contain the finalized formula")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    publish_command = commands.add_parser("publish")
    publish_command.add_argument("--directory", type=Path, required=True)
    publish_command.add_argument("--source-tag", required=True)
    publish_command.add_argument("--request-uuid", required=True)
    publish_command.add_argument("--manifest-sha256", required=True)
    publish_command.add_argument("--run-id", required=True)
    publish_command.add_argument("--run-attempt", required=True)
    publish_command.add_argument("--output", type=Path, required=True)
    finalize_command = commands.add_parser("finalize")
    finalize_command.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        api = GitHubApi(os.environ["GH_TOKEN"])
        if args.command == "publish":
            if SHA256.fullmatch(args.manifest_sha256) is None:
                raise ValueError("release manifest SHA-256 is malformed")
            publish(
                directory=args.directory,
                source_tag=args.source_tag,
                request_uuid=args.request_uuid,
                manifest_sha256=args.manifest_sha256,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                api=api,
                packages_token=os.environ["PACKAGES_TOKEN"],
                output=args.output,
            )
        else:
            finalize(ledger_path=args.ledger, api=api)
    except (
        KeyError, OSError, UnicodeError, http.client.HTTPException,
        json.JSONDecodeError, subprocess.SubprocessError, ValueError
    ) as error:
        print(f"homebrew-publish: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Verify source release assets against exact externally configured identities."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Mapping


CONTROL = {"release-manifest.json", "release-manifest.json.asc", "SHA256SUMS", "SHA256SUMS.asc"}
FINGERPRINT = re.compile(r"[0-9A-F]{40}|[0-9A-F]{64}")
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MANIFEST_FIELDS = {
    "schema_version", "package", "version", "tag", "commit", "source_tree_sha256",
    "signers", "channel_capabilities", "payloads", "dependency_closure",
    "conan_requirements", "provenance_subjects",
}
STABLE_CAPABILITIES = [
    "github", "conan2", "apt", "rpm", "aur", "homebrew", "chocolatey"
]
PUBLISHER_REPOSITORY = "yurirocha15/homebrew-mcp-cpp-sdk"
PUBLISHER_WORKFLOW = "publish-bottles.yml"
CONTROL_TAG = "release-control-v1"
FORMULA_ASSET = "homebrew-mcp-cpp-sdk.rb"
CONTRACT_ASSET = "release-publication-contract.json"


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON document repeats field: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_pairs)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(
    directory: Path,
    *,
    tag: str,
    commit: str,
    manifest_sha256: str,
    primary_fingerprint: str,
    tag_fingerprint: str,
    artifact_fingerprint: str,
    provider_control_sha: str,
    publisher_repository_id: str,
    formula_output: Path | None = None,
) -> None:
    key = directory / "release-signing-key.asc"
    _verify_key(key, primary_fingerprint, artifact_fingerprint)
    manifest_path = directory / "release-manifest.json"
    if sha256(manifest_path) != manifest_sha256:
        raise ValueError("release manifest digest mismatch")
    manifest = _read_json(manifest_path)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != MANIFEST_FIELDS
        or manifest.get("schema_version") != 2
        or manifest.get("package") != "mcp-cpp-sdk"
        or manifest.get("version") != tag.removeprefix("v")
        or manifest.get("tag") != tag
        or manifest.get("commit") != commit
        or not isinstance(manifest.get("source_tree_sha256"), str)
        or SHA256.fullmatch(manifest["source_tree_sha256"]) is None
        or manifest.get("channel_capabilities") != STABLE_CAPABILITIES
        or not isinstance(manifest.get("dependency_closure"), list)
        or not isinstance(manifest.get("conan_requirements"), list)
        or not isinstance(manifest.get("provenance_subjects"), list)
    ):
        raise ValueError("release manifest identity or schema is not exact")
    signers = manifest.get("signers")
    if signers != {
        "primary_fingerprint": primary_fingerprint,
        "tag_subkey_fingerprint": tag_fingerprint,
        "artifact_subkey_fingerprint": artifact_fingerprint,
    }:
        raise ValueError("release manifest signer identity is not exact")

    payloads = manifest.get("payloads")
    if not isinstance(payloads, list):
        raise ValueError("release manifest payloads are missing")
    records = {}
    for item in payloads:
        if (
            not isinstance(item, dict)
            or not {"name", "size", "sha256", "role"} <= set(item)
            or set(item) - {"name", "size", "sha256", "role", "build_tuple"}
        ):
            raise ValueError("release manifest payload record is malformed")
        name = item["name"]
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in CONTROL
            or name in records
            or type(item.get("size")) is not int
            or item["size"] < 1
            or not isinstance(item.get("sha256"), str)
            or SHA256.fullmatch(item["sha256"]) is None
            or not isinstance(item.get("role"), str)
            or not item["role"]
            or (
                "build_tuple" in item
                and (not isinstance(item["build_tuple"], str) or not item["build_tuple"])
            )
        ):
            raise ValueError("release manifest payload name is unsafe or duplicated")
        records[name] = item
    if list(records) != sorted(records):
        raise ValueError("release manifest payload records are not canonically ordered")
    for required in (FORMULA_ASSET, CONTRACT_ASSET, "release-signing-key.asc"):
        if required not in records or records[required]["role"] != "publisher-input":
            raise ValueError(f"required publisher input is absent: {required}")
    detached_signatures = {
        f"{name}.asc" for name in records if name.endswith((".tar.gz", ".zip"))
    }
    downloaded = {path.name for path in directory.iterdir() if path.is_file()}
    if downloaded != set(records) | CONTROL | detached_signatures:
        raise ValueError("release manifest, payload, and detached-signature sets differ")
    for name, item in records.items():
        path = directory / name
        if path.stat().st_size != item["size"] or sha256(path) != item["sha256"]:
            raise ValueError(f"release manifest payload mismatch: {name}")

    checksums = _checksums((directory / "SHA256SUMS").read_text(encoding="utf-8"))
    expected_checksums = {path.name for path in directory.iterdir() if path.is_file()} - {"SHA256SUMS", "SHA256SUMS.asc"}
    if set(checksums) != expected_checksums:
        raise ValueError("SHA256SUMS asset set mismatch")
    for name, digest in checksums.items():
        if sha256(directory / name) != digest:
            raise ValueError(f"SHA256SUMS mismatch: {name}")
    signed_assets = [
        (manifest_path, directory / "release-manifest.json.asc"),
        (directory / "SHA256SUMS", directory / "SHA256SUMS.asc"),
    ]
    signed_assets.extend(
        (directory / name, directory / f"{name}.asc")
        for name in sorted(records)
        if name.endswith((".tar.gz", ".zip"))
    )
    for signed, signature in signed_assets:
        _signature(signed, signature, key, primary_fingerprint, artifact_fingerprint)

    _verify_publication_contract(
        _read_json(directory / CONTRACT_ASSET),
        provider_control_sha=provider_control_sha,
        publisher_repository_id=publisher_repository_id,
    )
    _verify_formula((directory / FORMULA_ASSET).read_text(encoding="utf-8"), tag)
    if formula_output is not None:
        formula_output.parent.mkdir(parents=True, exist_ok=True)
        formula_output.write_bytes((directory / FORMULA_ASSET).read_bytes())


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"publication contract {label} is not an object")
    return value


def _verify_publication_contract(
    value: object, *, provider_control_sha: str, publisher_repository_id: str
) -> None:
    fields = {
        "schema_version", "native_targets", "native_builders", "channel_targets",
        "destinations", "conan_requirements", "provider_controls",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema_version") != 1
        or not isinstance(value.get("native_targets"), list)
        or not isinstance(value.get("native_builders"), list)
        or not isinstance(value.get("conan_requirements"), list)
    ):
        raise ValueError("publication contract schema is not exact")
    targets = _mapping(value.get("channel_targets"), "channel targets")
    if set(targets) != {"aur", "homebrew", "chocolatey"} or targets.get("homebrew") != {
        "tap": PUBLISHER_REPOSITORY,
        "bottle_tags": ["sequoia", "arm64_sequoia", "x86_64_linux", "arm64_linux"],
    }:
        raise ValueError("publication contract Homebrew target differs from publisher")
    destinations = _mapping(value.get("destinations"), "destinations")
    if set(destinations) != {"cloudsmith", "homebrew", "chocolatey", "conan_control"}:
        raise ValueError("publication contract destination inventory is not exact")
    destination = _mapping(destinations.get("homebrew"), "Homebrew destination")
    if set(destination) != {"repository", "repository_id"} or destination != {
        "repository": PUBLISHER_REPOSITORY,
        "repository_id": publisher_repository_id,
    }:
        raise ValueError("publication contract Homebrew destination differs from publisher")
    controls = _mapping(value.get("provider_controls"), "provider controls")
    if set(controls) != {"homebrew", "chocolatey", "conan"}:
        raise ValueError("publication contract provider inventory is not exact")
    control = _mapping(controls.get("homebrew"), "Homebrew provider control")
    if set(control) != {"repository", "control_tag", "control_commit", "workflow"} or control != {
        "repository": PUBLISHER_REPOSITORY,
        "control_tag": CONTROL_TAG,
        "control_commit": provider_control_sha,
        "workflow": PUBLISHER_WORKFLOW,
    }:
        raise ValueError("publication contract Homebrew control differs from execution")


def _verify_formula(text: str, tag: str) -> None:
    required = (
        "class McpCppSdk < Formula",
        f'  version "{tag.removeprefix("v")}"',
        "-DMCP_CPP_SDK_BUILD_SHARED=ON",
        "-DMCP_CPP_SDK_BUILD_STATIC=ON",
        "find_package(mcp-cpp-sdk CONFIG REQUIRED COMPONENTS shared static)",
        "target_link_libraries(shared-consumer PRIVATE mcp::sdk_shared)",
        "target_link_libraries(static-consumer PRIVATE mcp::sdk_static)",
    )
    if any(text.count(fragment) != 1 for fragment in required):
        raise ValueError("signed Homebrew formula does not validate static and shared consumers")


def _verify_key(path: Path, primary_fingerprint: str, artifact_fingerprint: str) -> None:
    result = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--show-keys", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError("release public key is unreadable")
    fingerprints = [line.split(":")[9] for line in result.stdout.splitlines() if line.startswith("fpr:")]
    if not fingerprints or fingerprints[0] != primary_fingerprint or artifact_fingerprint not in fingerprints:
        raise ValueError("release public key fingerprint mismatch")


def _checksums(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)", line)
        if match is None or match.group(2) in result:
            raise ValueError("SHA256SUMS record is malformed or duplicated")
        result[match.group(2)] = match.group(1)
    return result


def _signature(signed: Path, signature: Path, key: Path, primary: str, artifact: str) -> None:
    with tempfile.TemporaryDirectory() as home:
        imported = subprocess.run(
            ["gpg", "--batch", "--homedir", home, "--import", str(key)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if imported.returncode != 0:
            raise ValueError("release key import failed")
        result = subprocess.run(
            ["gpgv", "--homedir", home, "--status-fd", "1", "--keyring", f"{home}/pubring.kbx", str(signature), str(signed)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    signatures = [line.split() for line in result.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    if result.returncode != 0 or len(signatures) != 1 or len(signatures[0]) < 12:
        raise ValueError(f"signature verification failed: {signed.name}")
    if signatures[0][2] != artifact or signatures[0][-1] != primary:
        raise ValueError(f"signature identity mismatch: {signed.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--primary-fingerprint", required=True)
    parser.add_argument("--tag-fingerprint", required=True)
    parser.add_argument("--artifact-fingerprint", required=True)
    parser.add_argument("--provider-control-sha", required=True)
    parser.add_argument("--publisher-repository-id", required=True)
    parser.add_argument("--formula-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if SHA256.fullmatch(args.manifest_sha256) is None:
            raise ValueError("manifest SHA-256 is malformed")
        if (
            FINGERPRINT.fullmatch(args.primary_fingerprint) is None
            or FINGERPRINT.fullmatch(args.tag_fingerprint) is None
            or FINGERPRINT.fullmatch(args.artifact_fingerprint) is None
            or len(
                {
                    args.primary_fingerprint,
                    args.tag_fingerprint,
                    args.artifact_fingerprint,
                }
            )
            != 3
        ):
            raise ValueError("configured release fingerprint is malformed")
        if SHA1.fullmatch(args.commit) is None or SHA1.fullmatch(args.provider_control_sha) is None:
            raise ValueError("source or provider commit SHA is malformed")
        if re.fullmatch(r"[1-9][0-9]*", args.publisher_repository_id) is None:
            raise ValueError("publisher repository ID is malformed")
        verify(
            args.directory,
            tag=args.tag,
            commit=args.commit,
            manifest_sha256=args.manifest_sha256,
            primary_fingerprint=args.primary_fingerprint,
            tag_fingerprint=args.tag_fingerprint,
            artifact_fingerprint=args.artifact_fingerprint,
            provider_control_sha=args.provider_control_sha,
            publisher_repository_id=args.publisher_repository_id,
            formula_output=args.formula_output,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError, ValueError) as error:
        print(f"verify-source-release: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

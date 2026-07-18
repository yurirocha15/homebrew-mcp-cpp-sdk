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


CONTROL = {"release-manifest.json", "release-manifest.json.asc", "SHA256SUMS", "SHA256SUMS.asc"}
FINGERPRINT = re.compile(r"[0-9A-F]{40}|[0-9A-F]{64}")


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
    artifact_fingerprint: str,
) -> None:
    key = directory / "release-signing-key.asc"
    _verify_key(key, primary_fingerprint, artifact_fingerprint)
    manifest_path = directory / "release-manifest.json"
    if sha256(manifest_path) != manifest_sha256:
        raise ValueError("release manifest digest mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("tag") != tag or manifest.get("commit") != commit:
        raise ValueError("release manifest source identity mismatch")
    signers = manifest.get("signers")
    if not isinstance(signers, dict):
        raise ValueError("release manifest signer map is missing")
    if signers.get("primary_fingerprint") != primary_fingerprint:
        raise ValueError("release manifest primary fingerprint mismatch")
    if signers.get("artifact_subkey_fingerprint") != artifact_fingerprint:
        raise ValueError("release manifest artifact fingerprint mismatch")

    payloads = manifest.get("payloads")
    if not isinstance(payloads, list):
        raise ValueError("release manifest payloads are missing")
    records = {}
    for item in payloads:
        if not isinstance(item, dict) or not {"name", "size", "sha256"} <= set(item):
            raise ValueError("release manifest payload record is malformed")
        name = item["name"]
        if not isinstance(name, str) or Path(name).name != name or name in CONTROL or name in records:
            raise ValueError("release manifest payload name is unsafe or duplicated")
        records[name] = item
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
    parser.add_argument("--artifact-fingerprint", required=True)
    args = parser.parse_args()
    try:
        if re.fullmatch(r"[0-9a-f]{64}", args.manifest_sha256) is None:
            raise ValueError("manifest SHA-256 is malformed")
        if FINGERPRINT.fullmatch(args.primary_fingerprint) is None or FINGERPRINT.fullmatch(args.artifact_fingerprint) is None:
            raise ValueError("configured release fingerprint is malformed")
        verify(
            args.directory,
            tag=args.tag,
            commit=args.commit,
            manifest_sha256=args.manifest_sha256,
            primary_fingerprint=args.primary_fingerprint,
            artifact_fingerprint=args.artifact_fingerprint,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError, ValueError) as error:
        print(f"verify-source-release: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

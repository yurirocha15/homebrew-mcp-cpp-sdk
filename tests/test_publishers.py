from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from publisher.homebrew_publish import (
    BOTTLE_TAGS,
    FORMULA_PATH,
    _package_records,
    finalize,
    load_bottles,
    plan_formula_pr,
    publication_body,
    release_branch,
    verify_public_manifests,
    verify_sha256sums,
)
from publisher.validate_dispatch import (
    expected_request_uuid,
    validate,
    validate_execution_context,
)
from publisher.verify_git_status import verify as verify_git_status
from publisher.verify_handoffs import verify as verify_handoffs
from publisher.verify_source_release import (
    _verify_formula,
    _verify_publication_contract,
)
from publisher.verify_source_state import verify as verify_source_state


SOURCE_SHA = "0" * 40
WORKFLOW_SHA = "1" * 40
CONTROL_SHA = "2" * 40
MANIFEST_SHA = "3" * 64


def dispatch() -> dict[str, str]:
    return {
        "source_tag": "v0.2.0",
        "source_commit_sha": SOURCE_SHA,
        "source_workflow_head_sha": WORKFLOW_SHA,
        "provider_control_sha": CONTROL_SHA,
        "github_release_id": "1",
        "source_workflow_run_id": "2",
        "release_manifest_sha256": MANIFEST_SHA,
        "request_uuid": expected_request_uuid("v0.2.0", MANIFEST_SHA),
    }


class DispatchTests(unittest.TestCase):
    def test_accepts_exact_source_request_and_control_context(self) -> None:
        request = dispatch()
        self.assertEqual(validate(request), request)
        validate_execution_context(
            request,
            {
                "GITHUB_REPOSITORY": "yurirocha15/homebrew-mcp-cpp-sdk",
                "GITHUB_REF": "refs/tags/release-control-v1",
                "GITHUB_SHA": CONTROL_SHA,
                "GITHUB_WORKFLOW_REF": (
                    "yurirocha15/homebrew-mcp-cpp-sdk/.github/workflows/"
                    "publish-bottles.yml@refs/tags/release-control-v1"
                ),
            },
        )

    def test_rejects_every_missing_or_extra_field(self) -> None:
        request = dispatch()
        for field in tuple(request):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate({name: value for name, value in request.items() if name != field})
        with self.assertRaises(ValueError):
            validate({**request, "formula_pr_number": "3"})

    def test_rejects_arbitrary_uuid_and_main_execution(self) -> None:
        request = dispatch()
        with self.assertRaisesRegex(ValueError, "does not bind"):
            validate({**request, "request_uuid": "00000000-0000-0000-0000-000000000001"})
        with self.assertRaisesRegex(ValueError, "GITHUB_REF"):
            validate_execution_context(
                request,
                {
                    "GITHUB_REPOSITORY": "yurirocha15/homebrew-mcp-cpp-sdk",
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_SHA": CONTROL_SHA,
                    "GITHUB_WORKFLOW_REF": "ignored",
                },
            )


class SourceStateTests(unittest.TestCase):
    def api(self, workflow_sha: str = WORKFLOW_SHA):
        responses = {
            "repos/yurirocha15/mcp-cpp-sdk": {
                "id": 10,
                "full_name": "yurirocha15/mcp-cpp-sdk",
                "default_branch": "main",
                "archived": False,
                "disabled": False,
                "owner": {"id": 20},
            },
            "repos/yurirocha15/mcp-cpp-sdk/releases/tags/v0.2.0": {
                "id": 30,
                "tag_name": "v0.2.0",
                "draft": False,
                "prerelease": False,
                "immutable": True,
            },
            "repos/yurirocha15/mcp-cpp-sdk/actions/runs/40": {
                "id": 40,
                "status": "completed",
                "conclusion": "success",
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": workflow_sha,
                "path": ".github/workflows/release.yml",
                "repository": {"id": 10, "full_name": "yurirocha15/mcp-cpp-sdk"},
            },
        }
        return responses.__getitem__

    def test_workflow_sha_is_verified_separately_from_release_commit(self) -> None:
        verify_source_state(
            self.api(),
            source_repository_id=10,
            source_owner_id=20,
            source_tag="v0.2.0",
            source_workflow_head_sha=WORKFLOW_SHA,
            github_release_id=30,
            source_workflow_run_id=40,
            sleeper=lambda _: None,
        )
        self.assertNotEqual(SOURCE_SHA, WORKFLOW_SHA)

    def test_rejects_wrong_workflow_head(self) -> None:
        with self.assertRaisesRegex(ValueError, "workflow run"):
            verify_source_state(
                self.api(SOURCE_SHA),
                source_repository_id=10,
                source_owner_id=20,
                source_tag="v0.2.0",
                source_workflow_head_sha=WORKFLOW_SHA,
                github_release_id=30,
                source_workflow_run_id=40,
                sleeper=lambda _: None,
            )


def contract(control_sha: str = CONTROL_SHA) -> dict[str, object]:
    return {
        "schema_version": 1,
        "native_targets": [],
        "native_builders": [],
        "channel_targets": {
            "aur": {},
            "homebrew": {
                "tap": "yurirocha15/homebrew-mcp-cpp-sdk",
                "bottle_tags": ["sequoia", "arm64_sequoia", "x86_64_linux", "arm64_linux"],
            },
            "chocolatey": {},
        },
        "destinations": {
            "cloudsmith": {},
            "homebrew": {
                "repository": "yurirocha15/homebrew-mcp-cpp-sdk",
                "repository_id": "50",
            },
            "chocolatey": {},
            "conan_control": {},
        },
        "conan_requirements": [],
        "provider_controls": {
            "homebrew": {
                "repository": "yurirocha15/homebrew-mcp-cpp-sdk",
                "control_tag": "release-control-v1",
                "control_commit": control_sha,
                "workflow": "publish-bottles.yml",
            },
            "chocolatey": {},
            "conan": {},
        },
    }


FORMULA = '''class McpCppSdk < Formula
  version "0.2.0"
  -DMCP_CPP_SDK_BUILD_SHARED=ON
  -DMCP_CPP_SDK_BUILD_STATIC=ON
  find_package(mcp-cpp-sdk CONFIG REQUIRED COMPONENTS shared static)
  target_link_libraries(shared-consumer PRIVATE mcp::sdk_shared)
  target_link_libraries(static-consumer PRIVATE mcp::sdk_static)
'''


class SignedPolicyTests(unittest.TestCase):
    def test_contract_binds_destination_and_executing_control(self) -> None:
        _verify_publication_contract(
            contract(), provider_control_sha=CONTROL_SHA, publisher_repository_id="50"
        )
        with self.assertRaisesRegex(ValueError, "control"):
            _verify_publication_contract(
                contract(SOURCE_SHA),
                provider_control_sha=CONTROL_SHA,
                publisher_repository_id="50",
            )

    def test_formula_requires_static_and_shared_consumers(self) -> None:
        _verify_formula(FORMULA, "v0.2.0")
        with self.assertRaisesRegex(ValueError, "static and shared"):
            _verify_formula(FORMULA.replace("MCP_CPP_SDK_BUILD_STATIC=ON", "MCP_CPP_SDK_BUILD_STATIC=OFF"), "v0.2.0")

    def test_git_status_requires_exact_subkey_and_primary(self) -> None:
        tag = "A" * 40
        primary = "B" * 40
        verify_git_status(f"[GNUPG:] VALIDSIG {tag} 1 2 3 4 5 6 7 8 {primary}", tag, primary)
        with self.assertRaises(ValueError):
            verify_git_status("[GNUPG:] BADSIG dead", tag, primary)


class FakeApi:
    def __init__(self, responses: dict[tuple[str, str], object | None]):
        self.responses = responses

    def request(self, method, endpoint, payload=None, *, allow_missing=False):
        key = (method, endpoint)
        if key not in self.responses:
            if allow_missing:
                return None
            raise AssertionError(key)
        return self.responses[key]


class PublicationTests(unittest.TestCase):
    def test_checksum_inventory_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "asset").write_bytes(b"payload")
            digest = hashlib.sha256(b"payload").hexdigest()
            (root / "SHA256SUMS").write_text(f"{digest}  asset\n", encoding="ascii")
            verify_sha256sums(root)
            (root / "extra").write_bytes(b"bad")
            with self.assertRaisesRegex(ValueError, "inventory"):
                verify_sha256sums(root)

    def test_bottle_ledger_requires_exact_tags_and_run(self) -> None:
        bottles = [
            {
                "schema_version": 1,
                "run_id": "9",
                "run_attempt": "1",
                "bottle_tag": tag,
                "bottle_file": f"{tag}.tar.gz",
                "bottle_sha256": "a" * 64,
                "json_file": f"{tag}.json",
                "json_sha256": "b" * 64,
            }
            for tag in sorted(BOTTLE_TAGS)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.json"
            path.write_text(json.dumps({"schema_version": 1, "run_id": "9", "run_attempt": "1", "bottles": bottles}))
            self.assertEqual(len(load_bottles(path, run_id="9", run_attempt="2")), 4)
            with self.assertRaises(ValueError):
                load_bottles(path, run_id="10", run_attempt="1")

    def test_package_inventory_rejects_partial_existing_state(self) -> None:
        bottles = [
            {"bottle_tag": tag, "bottle_sha256": "a" * 64}
            for tag in sorted(BOTTLE_TAGS)
        ]
        pages = [[{
            "name": "sha256:" + "b" * 64,
            "metadata": {"container": {"tags": ["0.2.0.sequoia"]}},
        }]]
        with self.assertRaisesRegex(ValueError, "partial"):
            _package_records(pages, bottles, "0.2.0")

    def test_formula_plan_allows_only_absent_or_exact_branch(self) -> None:
        branch = release_branch("v0.2.0")
        main = {"ref": "refs/heads/main", "object": {"sha": SOURCE_SHA}}
        branch_endpoint = (
            "repos/yurirocha15/homebrew-mcp-cpp-sdk/git/ref/heads/"
            "release%2Fmcp-cpp-sdk-v0.2.0"
        )
        pulls_endpoint = (
            "repos/yurirocha15/homebrew-mcp-cpp-sdk/pulls?state=all&"
            "head=yurirocha15%3Arelease%2Fmcp-cpp-sdk-v0.2.0&base=main&per_page=100"
        )
        api = FakeApi({
            ("GET", "repos/yurirocha15/homebrew-mcp-cpp-sdk/git/ref/heads/main"): main,
            ("GET", branch_endpoint): None,
            ("GET", pulls_endpoint): [],
        })
        self.assertEqual(plan_formula_pr(api, branch=branch, formula=b"formula"), (SOURCE_SHA, None))

    def test_publication_body_is_canonical_and_identity_bound(self) -> None:
        body = publication_body(
            source_tag="v0.2.0",
            request_uuid=dispatch()["request_uuid"],
            manifest_sha256=MANIFEST_SHA,
            formula_sha256="4" * 64,
            manifests=[],
        )
        self.assertTrue(body.startswith("<!-- mcp-cpp-sdk-release {"))
        self.assertIn(MANIFEST_SHA, body)

    def test_public_registry_verification_binds_each_bottle_layer(self) -> None:
        entries = [
            {
                "bottle_sha256": str(index) * 64,
                "digest": "sha256:" + chr(ord("a") + index) * 64,
                "tag": f"0.2.0.{tag}",
            }
            for index, tag in enumerate(sorted(BOTTLE_TAGS), start=1)
        ]
        responses = [(200, {}, b'{"token":"public"}')]
        responses.extend(
            (
                200,
                {"docker-content-digest": entry["digest"]},
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "layers": [{"digest": "sha256:" + entry["bottle_sha256"]}],
                    }
                ).encode(),
            )
            for entry in entries
        )
        with mock.patch("publisher.homebrew_publish._registry_get", side_effect=responses):
            verify_public_manifests(entries)
        responses[-1] = (
            200,
            {"docker-content-digest": entries[-1]["digest"]},
            b'{"schemaVersion":2,"layers":[{"digest":"sha256:' + b"f" * 64 + b'"}]}',
        )
        with (
            mock.patch("publisher.homebrew_publish._registry_get", side_effect=responses),
            self.assertRaisesRegex(ValueError, "exact bottle"),
        ):
            verify_public_manifests(entries)

    def test_finalizer_treats_an_exact_merged_pr_as_idempotent(self) -> None:
        formula = b"published formula"
        formula_sha = hashlib.sha256(formula).hexdigest()
        branch = release_branch("v0.2.0")
        manifests: list[dict[str, str]] = []
        body = publication_body(
            source_tag="v0.2.0",
            request_uuid=dispatch()["request_uuid"],
            manifest_sha256=MANIFEST_SHA,
            formula_sha256=formula_sha,
            manifests=manifests,
        )
        pull = {
            "state": "closed",
            "merged": True,
            "title": "brew: publish v0.2.0",
            "body": body,
            "head": {"ref": branch, "sha": CONTROL_SHA, "repo": {"full_name": "yurirocha15/homebrew-mcp-cpp-sdk"}},
            "base": {"ref": "main"},
            "user": {"type": "Bot", "login": "mcp-cpp-sdk-homebrew-publisher[bot]"},
        }
        content = {
            "path": FORMULA_PATH,
            "encoding": "base64",
            "sha": SOURCE_SHA,
            "content": base64.b64encode(formula).decode(),
        }
        api = FakeApi({
            ("GET", "repos/yurirocha15/homebrew-mcp-cpp-sdk/pulls/7"): pull,
            ("GET", "repos/yurirocha15/homebrew-mcp-cpp-sdk/contents/Formula/mcp-cpp-sdk.rb?ref=main"): content,
        })
        ledger = {
            "schema_version": 1, "run_id": "1", "run_attempt": "1",
            "source_tag": "v0.2.0", "request_uuid": dispatch()["request_uuid"],
            "manifest_sha256": MANIFEST_SHA, "formula_sha256": formula_sha,
            "formula_branch": branch, "formula_pr_number": "7",
            "formula_head_sha": CONTROL_SHA, "ghcr_manifests": manifests,
            "bottles": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.json"
            path.write_text(json.dumps(ledger))
            with mock.patch("publisher.homebrew_publish.verify_public_manifests"):
                finalize(ledger_path=path, api=api)


class HandoffTests(unittest.TestCase):
    def test_rejects_non_exact_bundle_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bottle"
            root.mkdir(parents=True)
            bottle = root / "archive.tar.gz"
            bottle.write_bytes(b"bottle")
            digest = hashlib.sha256(b"bottle").hexdigest()
            metadata = {
                "root_url": "https://ghcr.io/v2/yurirocha15/mcp-cpp-sdk",
                "tags": {"sequoia": {"sha256": digest}},
            }
            metadata_path = root / "bottle.json"
            metadata_path.write_text(json.dumps(metadata))
            handoff = {
                "schema_version": 1, "run_id": "1", "run_attempt": "1",
                "bottle_tag": "sequoia", "bottle_file": bottle.name,
                "bottle_sha256": digest, "json_file": metadata_path.name,
                "json_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
            }
            (root / "handoff.json").write_text(json.dumps(handoff))
            (root / "mcp-cpp-sdk.rb").write_text("formula")
            self.assertEqual(len(verify_handoffs(root.parent, "1", "1")), 4)
            (root / "extra").write_text("bad")
            with self.assertRaisesRegex(ValueError, "inventory"):
                verify_handoffs(root.parent, "1", "1")


if __name__ == "__main__":
    unittest.main()

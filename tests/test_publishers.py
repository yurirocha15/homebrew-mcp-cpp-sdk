from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from publisher.homebrew_publish import (
    BOTTLE_TAGS,
    FORMULA_PATH,
    _content,
    _expected_manifests,
    _has_version_tag,
    _index_records,
    _package_pages,
    _upload_bottles,
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


def main_context() -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY": "yurirocha15/homebrew-mcp-cpp-sdk",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": CONTROL_SHA,
        "GITHUB_WORKFLOW_REF": (
            "yurirocha15/homebrew-mcp-cpp-sdk/.github/workflows/"
            "publish-bottles.yml@refs/heads/main"
        ),
    }


class DispatchTests(unittest.TestCase):
    def test_accepts_exact_source_request_on_main_at_dispatched_commit(self) -> None:
        request = dispatch()
        self.assertEqual(validate(request), request)
        validate_execution_context(request, main_context())

    def test_rejects_every_missing_or_extra_field(self) -> None:
        request = dispatch()
        for field in tuple(request):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate({name: value for name, value in request.items() if name != field})
        with self.assertRaises(ValueError):
            validate({**request, "formula_pr_number": "3"})

    def test_rejects_arbitrary_uuid_and_other_execution_contexts(self) -> None:
        request = dispatch()
        with self.assertRaisesRegex(ValueError, "does not bind"):
            validate({**request, "request_uuid": "00000000-0000-0000-0000-000000000001"})
        workflow = "yurirocha15/homebrew-mcp-cpp-sdk/.github/workflows/publish-bottles.yml"
        for name, value in (
            ("GITHUB_REF", "refs/tags/release-control-v3"),
            ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_SHA", SOURCE_SHA),
            ("GITHUB_WORKFLOW_REF", f"{workflow}@refs/heads/feature"),
            ("GITHUB_REPOSITORY", "yurirocha15/other"),
        ):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                validate_execution_context(request, {**main_context(), name: value})


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

    def run_api(self, statuses, conclusion="success"):
        """API whose run endpoint yields the given statuses or exceptions in order."""
        base = self.api()
        queue = list(statuses)
        endpoint = "repos/yurirocha15/mcp-cpp-sdk/actions/runs/40"

        def api(path):
            if path != endpoint:
                return base(path)
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            run = dict(base(endpoint))
            run["status"] = item
            run["conclusion"] = conclusion if item == "completed" else None
            return run

        return api, queue

    def poll(self, api, sleeps, **kwargs):
        verify_source_state(
            api,
            source_repository_id=10,
            source_owner_id=20,
            source_tag="v0.2.0",
            source_workflow_head_sha=WORKFLOW_SHA,
            github_release_id=30,
            source_workflow_run_id=40,
            sleeper=sleeps.append,
            **kwargs,
        )

    def test_waits_through_approval_gated_statuses_for_two_hours(self) -> None:
        statuses = ["queued", "in_progress", "waiting", "requested", "pending", "completed"]
        api, queue = self.run_api(statuses)
        sleeps: list[float] = []
        self.poll(api, sleeps)
        self.assertEqual(queue, [])
        self.assertEqual(sleeps, [30] * 5)
        # 240 attempts at 30 seconds cover two hours.
        api, queue = self.run_api(["waiting"] * 240)
        sleeps = []
        with self.assertRaisesRegex(ValueError, "workflow run"):
            self.poll(api, sleeps)
        self.assertEqual((len(sleeps), queue), (239, []))
        with self.assertRaisesRegex(ValueError, "attempt count"):
            self.poll(self.api(), [], attempts=241)

    def test_rejects_unknown_status_and_failed_conclusion(self) -> None:
        api, _ = self.run_api(["mystery"])
        with self.assertRaisesRegex(ValueError, "invalid status"):
            self.poll(api, [])
        api, _ = self.run_api(["completed"], conclusion="failure")
        with self.assertRaisesRegex(ValueError, "workflow run"):
            self.poll(api, [])

    def test_tolerates_four_consecutive_transient_failures(self) -> None:
        boom = subprocess.CalledProcessError(1, ["gh"])
        api, queue = self.run_api([boom] * 4 + ["in_progress", boom, boom, "completed"])
        sleeps: list[float] = []
        self.poll(api, sleeps)
        self.assertEqual(queue, [])
        api, queue = self.run_api([boom] * 5 + ["completed"])
        with self.assertRaises(subprocess.CalledProcessError):
            self.poll(api, [])
        self.assertEqual(queue, ["completed"])

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


def contract() -> dict[str, object]:
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
    }


FORMULA = '''class McpCppSdk < Formula
  url "https://github.com/yurirocha15/mcp-cpp-sdk/releases/download/v0.2.0/mcp-cpp-sdk-0.2.0.tar.gz"
  -DMCP_CPP_SDK_BUILD_SHARED=ON
  -DMCP_CPP_SDK_BUILD_STATIC=ON
  find_package(mcp-cpp-sdk CONFIG REQUIRED COMPONENTS shared static)
  target_link_libraries(shared-consumer PRIVATE mcp::sdk_shared)
  target_link_libraries(static-consumer PRIVATE mcp::sdk_static)
'''


class SignedPolicyTests(unittest.TestCase):
    def test_contract_binds_destination(self) -> None:
        _verify_publication_contract(contract(), publisher_repository_id="50")
        with self.assertRaisesRegex(ValueError, "destination"):
            _verify_publication_contract(contract(), publisher_repository_id="51")
        with self.assertRaisesRegex(ValueError, "schema"):
            _verify_publication_contract(
                {**contract(), "unexpected": {}}, publisher_repository_id="50"
            )

    def test_contract_with_former_provider_controls_is_accepted(self) -> None:
        _verify_publication_contract(
            {
                **contract(),
                "provider_controls": {
                    "homebrew": {
                        "repository": "yurirocha15/homebrew-mcp-cpp-sdk",
                        "control_tag": "release-control-v3",
                        "control_commit": SOURCE_SHA,
                        "workflow": "publish-bottles.yml",
                    },
                    "chocolatey": {},
                    "conan": {},
                },
            },
            publisher_repository_id="50",
        )

    def test_formula_requires_static_and_shared_consumers(self) -> None:
        _verify_formula(FORMULA, "v0.2.0")
        self.assertNotIn("version ", FORMULA)
        with self.assertRaisesRegex(ValueError, "static and shared"):
            _verify_formula(FORMULA.replace("v0.2.0", "v0.2.1"), "v0.2.0")
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

    def index(self, bottles, *, skip=()):
        return {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + chr(ord("a") + number) * 64,
                    "annotations": {
                        "org.opencontainers.image.ref.name": f"0.2.0.{bottle['bottle_tag']}",
                        "sh.brew.bottle.digest": bottle["bottle_sha256"],
                    },
                }
                for number, bottle in enumerate(bottles)
                if bottle["bottle_tag"] not in skip
            ],
        }

    def test_index_records_match_manifests_by_ref_name_annotation(self) -> None:
        bottles = [
            {"bottle_tag": tag, "bottle_sha256": str(number) * 64}
            for number, tag in enumerate(sorted(BOTTLE_TAGS), start=1)
        ]
        expected = _expected_manifests(bottles, "0.2.0")
        records = _index_records(self.index(bottles), expected)
        self.assertEqual([item["tag"] for item in records], sorted(f"0.2.0.{t}" for t in BOTTLE_TAGS))
        with self.assertRaisesRegex(ValueError, "partial"):
            _index_records(self.index(bottles, skip={"sequoia"}), expected)
        wrong = self.index(bottles)
        wrong["manifests"][0]["annotations"]["sh.brew.bottle.digest"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "exact bottle digest"):
            _index_records(wrong, expected)

    def test_version_tag_marks_an_existing_publication(self) -> None:
        index_version = {"name": "sha256:" + "b" * 64, "metadata": {"container": {"tags": ["0.2.0"]}}}
        untagged = {"name": "sha256:" + "c" * 64, "metadata": {"container": {"tags": []}}}
        self.assertTrue(_has_version_tag([[untagged, index_version]], "0.2.0"))
        self.assertFalse(_has_version_tag([[untagged]], "0.2.0"))
        self.assertFalse(_has_version_tag([], "0.2.0"))

    def test_missing_package_before_first_upload_means_no_versions(self) -> None:
        missing = subprocess.CompletedProcess([], 1, b'{"message":"Not Found"}', b"gh: Not Found (HTTP 404)")
        with mock.patch("publisher.homebrew_publish.subprocess.run", return_value=missing):
            self.assertEqual(_package_pages("token"), [])
        broken = subprocess.CompletedProcess([], 1, b"", b"gh: Server Error (HTTP 500)")
        with (
            mock.patch("publisher.homebrew_publish.subprocess.run", return_value=broken),
            self.assertRaisesRegex(ValueError, "listing failed"),
        ):
            _package_pages("token")

    def test_contents_api_base64_with_newlines_is_decoded(self) -> None:
        formula = b"class McpCppSdk < Formula\n" * 20
        encoded = base64.encodebytes(formula).decode()
        self.assertIn("\n", encoded.strip())
        api = FakeApi({("GET", "repos/yurirocha15/homebrew-mcp-cpp-sdk/contents/Formula/mcp-cpp-sdk.rb?ref=main"): {
            "path": FORMULA_PATH, "encoding": "base64", "sha": SOURCE_SHA, "content": encoded,
        }})
        self.assertEqual(_content(api, "main"), (SOURCE_SHA, formula))
        api.responses[next(iter(api.responses))]["content"] = "!!!!"
        with self.assertRaisesRegex(ValueError, "base64"):
            _content(api, "main")

    def test_upload_uses_pr_upload_upload_only_in_a_scratch_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for tag in sorted(BOTTLE_TAGS):
                (root / f"mcp-cpp-sdk--0.2.0.{tag}.bottle.json").write_text("{}")
                (root / f"mcp-cpp-sdk--0.2.0.{tag}.bottle.tar.gz").write_bytes(b"x")
            seen = {}

            def fake_run(command, **kwargs):
                seen["command"] = command
                seen["env"] = kwargs["env"]
                seen["files"] = sorted(path.name for path in Path(kwargs["cwd"]).iterdir())
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.dict("os.environ", {"GH_TOKEN": "app"}), mock.patch(
                "publisher.homebrew_publish.subprocess.run", side_effect=fake_run
            ) as run:
                _upload_bottles(root, "secret-token")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(seen["command"][:3], ["brew", "pr-upload", "--upload-only"])
            self.assertEqual(len(seen["command"]), 4)  # no named arguments
            self.assertEqual(seen["env"]["HOMEBREW_GITHUB_PACKAGES_USER"], "yurirocha15")
            self.assertEqual(seen["env"]["HOMEBREW_GITHUB_PACKAGES_TOKEN"], "secret-token")
            self.assertNotIn("GH_TOKEN", seen["env"])
            self.assertEqual(len(seen["files"]), 8)
            self.assertEqual(sorted(path.name for path in root.iterdir()).__len__(), 8)
            failed = subprocess.CompletedProcess([], 1, "", "boom secret-token")
            with (
                mock.patch("publisher.homebrew_publish.subprocess.run", return_value=failed),
                self.assertRaisesRegex(ValueError, r"boom \*\*\*"),
            ):
                _upload_bottles(root, "secret-token")

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
        index_body = json.dumps({
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": entry["digest"],
                    "annotations": {
                        "org.opencontainers.image.ref.name": entry["tag"],
                        "sh.brew.bottle.digest": entry["bottle_sha256"],
                    },
                }
                for entry in entries
            ],
        }).encode()
        index_response = (
            200,
            {"docker-content-digest": "sha256:" + hashlib.sha256(index_body).hexdigest()},
            index_body,
        )
        responses = [(200, {}, b'{"token":"public"}'), index_response]
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
        with mock.patch("publisher.homebrew_publish._registry_get", side_effect=responses) as get:
            verify_public_manifests(entries, "0.2.0")
        self.assertTrue(get.call_args_list[1].args[1].endswith("/manifests/0.2.0"))
        responses[-1] = (
            200,
            {"docker-content-digest": entries[-1]["digest"]},
            b'{"schemaVersion":2,"layers":[{"digest":"sha256:' + b"f" * 64 + b'"}]}',
        )
        with (
            mock.patch("publisher.homebrew_publish._registry_get", side_effect=responses),
            self.assertRaisesRegex(ValueError, "exact bottle"),
        ):
            verify_public_manifests(entries, "0.2.0")
        tampered = [(200, {}, b'{"token":"public"}'), (200, {"docker-content-digest": "sha256:" + "0" * 64}, index_body)]
        with (
            mock.patch("publisher.homebrew_publish._registry_get", side_effect=tampered),
            self.assertRaisesRegex(ValueError, "digest differs"),
        ):
            verify_public_manifests(entries, "0.2.0")

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

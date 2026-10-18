from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from publisher.validate_dispatch import FIELDS


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
FULL_SHA_ACTION = re.compile(
    r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})(?:\s|$)"
)


def job(workflow: str, name: str, following: str | None) -> str:
    block = workflow.split(f"\n  {name}:\n", 1)[1]
    if following is not None:
        block = block.split(f"\n  {following}:\n", 1)[0]
    return block


class SecurityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = (WORKFLOWS / "publish-bottles.yml").read_text()
        self.ci_workflow = (WORKFLOWS / "ci.yml").read_text()
        self.manifest = json.loads((ROOT / "repository-settings.json").read_text())

    def test_actions_are_full_sha_allowlisted(self) -> None:
        allowed = self.manifest["actions"]["allowed_actions"]
        observed = set()
        for path in WORKFLOWS.glob("*.yml"):
            for line in path.read_text().splitlines():
                if "uses:" not in line:
                    continue
                match = FULL_SHA_ACTION.search(line)
                self.assertIsNotNone(match, line)
                self.assertEqual(allowed.get(match.group(1)), match.group(2))
                observed.add(match.group(1))
        self.assertEqual(observed, set(allowed))

    def test_dispatch_inputs_exactly_match_source_contract(self) -> None:
        header = self.workflow.split("\npermissions:\n", 1)[0]
        observed = tuple(
            match.group(1)
            for match in re.finditer(r"^      ([a-z][a-z0-9_]+):$", header, re.MULTILINE)
        )
        self.assertEqual(observed, FIELDS)
        for obsolete in ("formula_pr_number", "formula_branch", "formula_head_sha"):
            self.assertNotIn(f"      {obsolete}:\n", header)

    def test_dispatch_executes_only_the_immutable_direct_control_tag(self) -> None:
        verify = job(self.workflow, "verify", "bottle")
        self.assertNotIn("\n    if:", verify)
        self.assertIn("Fail closed unless executing the immutable provider control", verify)
        self.assertIn('test "${GITHUB_REF}" = refs/tags/release-control-v1', verify)
        self.assertIn('test "${GITHUB_SHA}" = "${EXPECTED_CONTROL_SHA}"', verify)
        self.assertIn("@refs/tags/release-control-v1", verify)
        self.assertIn("ref: ${{ inputs.provider_control_sha }}", verify)

    def test_source_and_control_identities_are_all_consumed(self) -> None:
        verify = job(self.workflow, "verify", "bottle")
        for field in FIELDS:
            self.assertIn(f"inputs.{field}", verify)
        self.assertIn("publisher/verify_source_state.py", verify)
        self.assertIn("--source-workflow-head-sha", verify)
        self.assertIn("--provider-control-sha", verify)
        self.assertIn("--publisher-repository-id", verify)

    def test_only_mutation_jobs_receive_environment_secrets(self) -> None:
        verify = job(self.workflow, "verify", "bottle")
        bottle = job(self.workflow, "bottle", "assemble")
        assemble = job(self.workflow, "assemble", "publish")
        publish = job(self.workflow, "publish", "finalize")
        finalize = job(self.workflow, "finalize", None)
        for block in (verify, bottle, assemble):
            self.assertNotIn("environment:", block)
            self.assertNotIn("secrets.", block)
            self.assertNotIn("permission-contents: write", block)
        self.assertIn("environment: homebrew-publish", publish)
        self.assertIn("environment: homebrew-finalize", finalize)
        for block in (publish, finalize):
            self.assertIn("persist-credentials: false", block)
            self.assertIn("ref: ${{ needs.verify.outputs.provider_control_sha }}", block)
            self.assertNotIn("brew install", block)
            self.assertNotIn("cmake", block)

    def test_finalizer_cannot_build_or_upload(self) -> None:
        finalize = job(self.workflow, "finalize", None)
        self.assertNotIn("brew pr-upload", finalize)
        self.assertNotIn("packages: write", finalize)
        self.assertIn("gh pr checks", finalize)
        self.assertIn("homebrew_publish.py finalize", finalize)

    def test_workflow_contains_no_embedded_python_programs(self) -> None:
        self.assertNotIn("python3 -I -S -", self.workflow)
        self.assertNotIn("<<'PY'", self.workflow)

    def test_environments_allow_only_the_control_tag(self) -> None:
        environments = self.manifest["environments"]
        self.assertEqual({item["name"] for item in environments}, {"homebrew-publish", "homebrew-finalize"})
        for environment in environments:
            self.assertEqual(environment["deployment_ref"], "refs/tags/release-control-v1")
            self.assertEqual(environment["required_reviewer_count"], 1)
            self.assertFalse(environment["administrator_bypass"])

    def test_formula_audit_is_skipped_only_while_no_formula_exists(self) -> None:
        self.assertIn('formula=Formula/mcp-cpp-sdk.rb', self.ci_workflow)
        self.assertIn(
            '[[ "${GITHUB_EVENT_NAME}" == push && "${GITHUB_REF_NAME}" == main ]]',
            self.ci_workflow,
        )
        self.assertIn(
            '[[ "${GITHUB_EVENT_NAME}" == pull_request ]] && ! git cat-file -e "HEAD^1:${formula}"',
            self.ci_workflow,
        )
        self.assertIn("fetch-depth: 2", self.ci_workflow)
        self.assertIn('brew style "${formula}"', self.ci_workflow)
        self.assertIn('brew audit --strict "${formula}"', self.ci_workflow)

    def test_actionlint_download_is_version_and_checksum_pinned(self) -> None:
        script = (ROOT / "scripts/lint_workflows.sh").read_text()
        self.assertIn('readonly version="1.7.12"', script)
        self.assertRegex(script, r'readonly digest="[0-9a-f]{64}"')
        self.assertIn("sha256sum --check --status", script)


if __name__ == "__main__":
    unittest.main()

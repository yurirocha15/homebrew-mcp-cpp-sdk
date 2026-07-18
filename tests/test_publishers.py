from __future__ import annotations

import unittest

from publisher.validate_dispatch import validate


class DispatchTests(unittest.TestCase):
    def test_stable_request(self) -> None:
        request = {
            "source_tag": "v0.2.0",
            "source_commit_sha": "0" * 40,
            "github_release_id": "1",
            "source_workflow_run_id": "2",
            "release_manifest_sha256": "1" * 64,
            "formula_pr_number": "3",
            "formula_branch": "release/mcp-cpp-sdk-v0.2.0",
            "formula_head_sha": "2" * 40,
            "request_uuid": "00000000-0000-0000-0000-000000000001",
        }
        self.assertEqual(validate(request), request)

    def test_rejects_wrong_branch(self) -> None:
        with self.assertRaises(ValueError):
            validate({"formula_branch": "main"})

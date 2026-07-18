# Homebrew tap bootstrap

This template becomes the public `yurirocha15/homebrew-mcp-cpp-sdk` tap. Its
trusted workflow accepts only a source-App-authored formula PR branch and exact
head, verifies the immutable source release, builds bottles without publisher
credentials, uploads only digest-bound same-run artifacts, and pauses in a
second protected environment before finalization.

Bottle builders checkout only the prevalidated exact formula head and receive
no environment secret or package-write permission. The upload job receives no
checkout and consumes only same-run artifacts whose IDs and SHA-256 values are
recorded in its handoff. The finalizer belongs to the same original workflow
run, performs no build or upload, revalidates the PR head and immutable GHCR
digests (including an anonymous pull), and only then merges the formula PR.

If artifacts expire, the PR head changes, GHCR cannot be made public, or the
original run cannot be resumed, finalization stops as `BLOCKED_MANUAL_ACTION`;
a new dispatch may not adopt or finalize the old bottles.

`repository-settings.json` is a public code-policy contract containing only
generic variable/secret names and expected controls. Operator identities,
provider object IDs, and provisioning evidence remain outside tracked source.

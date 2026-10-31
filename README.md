# Homebrew tap bootstrap

This repository is the public `yurirocha15/homebrew-mcp-cpp-sdk` tap. Its
trusted workflow runs only from protected `main`, at the exact commit the source
dispatched. It accepts the source release pipeline's generic signed handoff,
verifies the source repository, workflow commit, signed tag, immutable release,
manifest, publication contract, and deterministic request identity, then
extracts the source-signed formula.

Bottle builders consume only that verified formula and receive no publisher
credential or package-write permission. They build and run both static and
shared consumers. The protected publisher uploads only digest-bound same-run
artifacts and creates or exactly reuses the deterministic formula branch and
pull request. Conflicting existing state fails before it is overwritten.
The separately protected finalizer performs no build or upload; it validates
the public GHCR manifest and bottle layer digests before merging the exact PR.

If artifacts expire, the PR head changes, only part of a GHCR publication
exists, or GHCR cannot be made public, publication stops. An exact retry is a
no-op or resumes from the verified state; a non-identical retry is rejected.

`repository-settings.json` is a public code-policy contract containing only
generic variable and secret names plus expected controls. Provider object IDs,
credentials, and provisioning evidence remain outside tracked source.

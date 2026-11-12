# Internal E2E coverage

This directory belongs to the `e2e-coverage` branch. It contains the internal
Oracle application acceptance harness, deployment/quality investigations,
failure and concurrency coverage, and the live RAG browser demo.

The branch was created from `upstream-port` at SDK correction commit
`d47ccbf86e7c5affeeb5aca1a6f1cf16b97c6e26`. Its additional commits are internal
test assets, separate from the SDK changes intended for upstream.

## Branch workflow

- Use `e2e-coverage` for internal harness changes and test execution.
- Use `upstream-port` as the source for the eventual upstream SDK pull request.
  Do not merge `e2e-coverage` into that branch or into upstream.
- Bring later approved SDK changes into this test branch as needed; rebuild and
  select matching package artifacts before starting a fresh test campaign.
- A branch name does not restrict repository visibility. Share this branch only
  through a private repository accessible to the internal testing team.

Before running the test guides, select this branch from the repository root:

```bash
git switch e2e-coverage
```

## Test guides

- [Oracle application harness](oracledb-app/README.md)
- [Live-model browser testing](oracledb-app/BROWSER_LIVE.md)
- [Coverage and acceptance findings](oracledb-app/COVERAGE.md)
- [Deployment matrix](oracledb-app/DEPLOYMENT_COVERAGE.md)
- [Exact retrieval correction validation](oracledb-app/EXPLICIT_RETRIEVAL.md)

Credentials, signing keys, wallets, virtual environments and generated evidence
are not branch contents. Keep credentials outside the repository. Ignored,
ownership-guarded retained fixtures and reports remain available on the test
environment; switching branches does not authorize their cleanup.

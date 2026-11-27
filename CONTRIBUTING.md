# Contributing to ReWii

## Branches

| Branch | What it is |
|---|---|
| `dev` | Integration branch and the repo default. Everything lands here first. |
| `main` | Released code only. Every commit on it is a release merge, tagged `vX.Y.Z`. |
| `feature/…`, `fix/…`, `chore/…` | Short-lived work branches, cut from `dev`. |
| `hotfix/…` | Urgent fixes to a release, cut from `main`. |

Both `dev` and `main` are protected: changes arrive only through pull requests whose
build (tests, lint and type checks, and the single-file app's self-test on Windows,
macOS and Linux) has passed.

## Everyday work

1. `git switch dev && git pull`, then `git switch -c feature/short-name`.
2. Commit, push, and open a pull request into `dev`.
3. When the build is green, merge it (merge commit; delete the branch).
4. Add a line under `## [Unreleased]` in `CHANGELOG.md` for anything a user would notice.

## Releasing

1. On a branch from `dev`: set the new version in `pyproject.toml` and move the
   `[Unreleased]` notes into a new `## [X.Y.Z] - date` section of `CHANGELOG.md`.
   Merge that into `dev`.
2. Open a pull request from `dev` into `main` titled "Release X.Y.Z" and merge it.
3. Tag the merge commit on `main` and push the tag:
   `git switch main && git pull && git tag -a vX.Y.Z -m "ReWii X.Y.Z" && git push origin vX.Y.Z`
4. The build publishes the GitHub release with the four downloads, `SHA256SUMS.txt`
   and the changelog section as its text. It refuses a tag that doesn't match the
   version in `pyproject.toml`, isn't on `main`, or has no changelog section.
5. Bring `main` back into `dev` (`git switch dev && git merge main && git push`) so the
   release merge is in both.

A hotfix goes the same way from `main`: branch `hotfix/…`, PR into `main`, tag a patch
version, then merge `main` back into `dev`.

## Checks to run before pushing

```
uv run pytest -q
uv run ruff check src tests
uv run mypy src/rewii
```

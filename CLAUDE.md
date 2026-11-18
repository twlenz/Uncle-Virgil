# Working on Uncle Virgil

## Keeping GitHub up to date

The owner's standing instruction (given November 2026): always keep GitHub up
to date without being asked. After any change, commit, push the working
branch, and fast-forward `main` to it (`git push origin <branch>:main`). Do not
wait to be told to push or merge. If `main` cannot be fast-forwarded (it has
commits the branch lacks), merge `main` into the branch first, then update
`main`; never force-push `main`.

## Checks before pushing

```bash
pytest
```

With the speech models available, also run the tests that use them:

```bash
UNCLE_VIRGIL_TEST_MODELS=/path/to/models pytest
```

GitHub Actions (`.github/workflows/tests.yml`) runs the full suite on Windows
(Python 3.12 and 3.13) and Linux, opens the real window with `--self-test`, and
builds the standalone Windows app. The owner uses Windows; check the Windows
jobs after pushing.

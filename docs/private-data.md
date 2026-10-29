# Keeping private data out of the repository

This repository is public. Real packages, server and client names, and anything personal stay
on your machine, in a folder git never sees:

```
private/                  gitignored (or set ONPREM_PRIVATE_DIR to a folder outside the repo)
  denylist.txt            terms that must never be committed or pushed
  fixtures/               real .dtsx, .ispac and .dtproj files from the field
  notes/                  anything else
```

## Real packages as local tests

`uv run pytest` also runs `tests/test_private_fixtures.py` over everything in
`private/fixtures/` (any depth): every package must parse, outline, and hash the same after
SSISDB's re-save (what overwrite protection relies on); every `.ispac` must survive being
stored; every `.dtproj` must build or refuse with a reason. Test ids are numbers, not file
names. Without private fixtures (as in CI) these tests are skipped.

When a real package breaks a tool, reproduce it with a made-up package in `tests/fixtures/`
before fixing it; never copy the real one in.

## The leak guard

`scripts/privacy_check.py` checks file contents (including inside `.ispac` archives and
UTF-16 files), file names, commit messages and author identities against:

- built-in patterns: e-mail addresses (except `example.com` and GitHub/Anthropic noreply
  addresses), AWS account ids, access keys, private keys, AWS-generated host names
  (`EC2AMAZ-...`), home-directory paths;
- your `private/denylist.txt`: one term per line, case-insensitive, `re:` for a regular
  expression, `#` for comments. Put client and company names, server, domain and database
  names, people's names and your own name and e-mail there. Findings never print a denylisted term.

```bash
python scripts/privacy_check.py --install-hooks   # once per clone: pre-commit and pre-push hooks
python scripts/privacy_check.py --files           # the files as they are now
python scripts/privacy_check.py --all             # files and the whole history
```

The pre-commit hook also checks your git identity; use the GitHub noreply address in this
repository (`git config user.email <id>+<user>@users.noreply.github.com`). CI runs `--all` with
the built-in patterns only, since the denylist never leaves your machine.

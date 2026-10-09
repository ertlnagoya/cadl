# Contributing to CADL

Thanks for your interest in CADL. Bug reports, example models and pull
requests are welcome.

## Reporting a problem

Open an issue at <https://github.com/ertlnagoya/cadl/issues> with:

- the `.cadl` file (or the smallest fragment that shows the problem),
- the command you ran and its full output,
- `cadl --version` and your Python version.

For questions about the language itself, see the
[specification](https://ertlnagoya.github.io/cadl-spec/).

## Development setup

```bash
git clone https://github.com/ertlnagoya/cadl
cd cadl
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Python 3.9 or later is required; CI runs 3.9 through 3.13.

## Before you open a pull request

- `pytest` passes.
- Every file in `examples/` still passes `cadl verify`
  (`tests/test_examples_verify.py` checks this).
- New behaviour has a test. Changes to `grammar.lark` need a case in
  `tests/test_expr_parser.py` and a round-trip case in
  `tests/test_unparse.py`.
- User-visible changes are listed under `[Unreleased]` in `CHANGELOG.md`.

The generators carry expressions as text in several places (labels,
simulator configs). Use `cadl.unparse.expr_to_source()` for that rather than
formatting AST nodes by hand.

## Releases

The version lives in `src/cadl/__init__.py` only. A release is a commit that
sets it, moves the `[Unreleased]` notes under the new version in
`CHANGELOG.md`, and is tagged `vX.Y.Z`.

Publishing a GitHub Release for that tag runs `.github/workflows/publish.yml`,
which uploads the distributions to PyPI. The workflow fails if the tag and
the package version differ.

## License

By contributing you agree that your contribution is licensed under the MIT
License (see `LICENSE`).

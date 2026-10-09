# Contributing

Use Python 3.10+ in an isolated venv. Install `python -m pip install -e '.[dev]'`,
run `python -m pytest -q` and `python -m build`. For MCP, follow README to install a digest-verified approved CLI wheel or
reviewed immutable commit from `Pachecodes/pictureme-cli-public` first in a fresh
venv. Do not resolve the shared CLI by name from an index; ownership/publication
is not established. CI builds the explicit public CLI checkout before MCP.
Tests must stay offline with synthetic credentials and example.com data. Never
call paid generation in CI. Include security regression tests. Contributions
must be yours to license under MIT; preserve all third-party notices.

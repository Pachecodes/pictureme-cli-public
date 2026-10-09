# Install

Requires Python 3.10+. From this standalone checkout:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
pictureme --help
```

Public target: https://github.com/Pachecodes/pictureme-cli-public. This is a
publication target, not a claim that the repository is already public. Install
only a reviewed checkout or digest-verified wheel in a fresh venv. After public
publication, an immutable reviewed Git commit can also be installed with
`python -m pip install "pictureme-cli @ git+https://github.com/Pachecodes/pictureme-cli-public.git@APPROVED_CLI_COMMIT"`
(replace the placeholder with the approved SHA).

No monorepo siblings are required. Once an approved release is published to your package index, `python -m pip install pictureme-cli` is also possible; index publication is not asserted here.


[Documentation index](README.md) · [Project overview](../README.md)

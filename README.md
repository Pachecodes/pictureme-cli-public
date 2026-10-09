# pictureme-cli

Python CLI client for the hosted PictureME API, maintained by **Jesus Pacheco / Akitá Labs**. `pictureme` and `picme` are equivalent entry points. This is not the commercial backend or a self-hosted service.

## Install

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

## Usage

```bash
pictureme auth login
pictureme auth login --no-open
pictureme model list --image
pictureme model get MODEL_ID
pictureme tokens balance
pictureme generate list
pictureme generate get 12345
pictureme --json model list
```

Device login needs a PictureME account and browser approval, not a password in the CLI. Legacy `auth login --password --email you@example.com` prompts with hidden input. Public catalog reads may work without authentication; other operations require server-issued keys, scopes, permissions and credits. Client licensing does not grant service access.

Generation spends credits and uploads disclose input media to the service. Select a real model from the live registry and obtain explicit user authorization before submitting:

```bash
pictureme generate create MODEL_ID --prompt "A landscape at sunrise" --image ./photo.jpg --wait
```

This is illustrative, NOT a smoke test. `generate cost` is a local estimate from catalog fields, not a guaranteed invoice; server billing rules prevail. Cancellation does not guarantee a refund or stop an upstream provider.

## Configuration and credential safety

Default origin is `https://go.pictureme.now`. Configuration names: `PICTUREME_HOST`, `PICTUREME_API_KEY`. Inject secrets from a secret manager; never commit them. Flags override environment, which overrides stored config. Avoid `--api-key` because process listings and shell history can reveal it.

Config location comes from `platformdirs`. POSIX device login stores a plaintext credential in an atomically replaced mode-0600 file; this is not encrypted. Protect accounts, filesystems and backups. Non-POSIX persistence is refused; use environment credentials. Stored credentials are host-bound and not reused for a different host override. Remote hosts require HTTPS; HTTP is allowed only for loopback.

`auth token` masks by default. `auth token --show`, `api-key create`, and admin `--no-redact` deliberately reveal secrets: never use them in logged terminals, CI or unattended agents. JSON login/status does not emit bearer credentials. `auth logout` clears local storage only; revoke remotely in account settings.

## Permissions and limitations

`model`, `upload`, `generate`, and `tokens` are service-backed clients. Server scopes, ownership and credit checks still apply. `workflow run` is unimplemented. Some `booth` commands are placeholders or depend on service endpoints; consult help rather than assuming completeness.

`admin` is operator-only. Installing the client grants no role or permission. Personal keys must not be assumed to work on admin routes: server credential-class, role and scope enforcement determines access. `admin ale` requires a separate web-approved operator credential with explicit ALE scopes and a live authorized role. No server deployment or compatibility is asserted by offline tests. Never copy browser credentials or bypass a 403. Admin mutations can replace full records, publish or delete content; consult help and read current records first. Generic admin responses are redacted by default.

All HTTP redirects are disabled; 3xx responses are controlled errors. Passwords,
device exchange codes and media are never automatically forwarded. Admin API
redaction masks entire secret-named dict/list subtrees and conventional camelCase
keys; it is a heuristic, not proof that arbitrary output is secret-free. Explicit
`--no-redact` and documented reveal commands intentionally print secrets.

## Development and license

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m build
```

Tests are offline. See CONTRIBUTING.md, SECURITY.md and PROVENANCE.md. MIT covers original client code only; dependency licenses and service/media terms remain separate.

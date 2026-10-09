# Configuration and credential safety

Default origin is `https://go.pictureme.now`. Configuration names: `PICTUREME_HOST`, `PICTUREME_API_KEY`. Inject secrets from a secret manager; never commit them. Flags override environment, which overrides stored config. Avoid `--api-key` because process listings and shell history can reveal it.

Config location comes from `platformdirs`. POSIX device login stores a plaintext credential in an atomically replaced mode-0600 file; this is not encrypted. Protect accounts, filesystems and backups. Non-POSIX persistence is refused; use environment credentials. Stored credentials are host-bound and not reused for a different host override. Remote hosts require HTTPS; HTTP is allowed only for loopback.

`auth token` masks by default. `auth token --show`, `api-key create`, and admin `--no-redact` deliberately reveal secrets: never use them in logged terminals, CI or unattended agents. JSON login/status does not emit bearer credentials. `auth logout` clears local storage only; revoke remotely in account settings.


[Documentation index](README.md) · [Project overview](../README.md)

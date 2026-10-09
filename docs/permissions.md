# Permissions and limitations

`model`, `upload`, `generate`, and `tokens` are service-backed clients. Server scopes, ownership and credit checks still apply. `workflow run` is unimplemented. Some `booth` commands are placeholders or depend on service endpoints; consult help rather than assuming completeness.

`admin` is operator-only. Installing the client grants no role or permission. Personal keys must not be assumed to work on admin routes: server credential-class, role and scope enforcement determines access. `admin ale` requires a separate web-approved operator credential with explicit ALE scopes and a live authorized role. No server deployment or compatibility is asserted by offline tests. Never copy browser credentials or bypass a 403. Admin mutations can replace full records, publish or delete content; consult help and read current records first. Generic admin responses are redacted by default.

All HTTP redirects are disabled; 3xx responses are controlled errors. Passwords,
device exchange codes and media are never automatically forwarded. Admin API
redaction masks entire secret-named dict/list subtrees and conventional camelCase
keys; it is a heuristic, not proof that arbitrary output is secret-free. Explicit
`--no-redact` and documented reveal commands intentionally print secrets.


[Documentation index](README.md) · [Project overview](../README.md)

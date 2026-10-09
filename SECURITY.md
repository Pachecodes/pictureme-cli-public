# Security

Do not include credentials, private media, account records or internal URLs in issues.
Report vulnerabilities through GitHub private reporting if enabled; otherwise
contact maintainers through their GitHub profiles to arrange a private channel.

Use least-privilege keys and HTTPS. Environment credentials are not persisted.
On POSIX, device login atomically stores a plaintext bearer credential in a mode-0600
file in the platform-specific user config directory. It is not encrypted: protect
accounts, backups and filesystems. Non-POSIX credential persistence is refused;
use PICTUREME_API_KEY instead. Host overrides do not reuse credentials stored for
another origin. Never log explicit secret-reveal commands or key-creation output.
Logout clears local storage only; revoke keys in the service as well.
MCP can upload local files: trust the agent and restrict filesystem access.
Require user authorization before generation, which spends account credits.

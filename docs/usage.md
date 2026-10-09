# Usage

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


[Documentation index](README.md) · [Project overview](../README.md)

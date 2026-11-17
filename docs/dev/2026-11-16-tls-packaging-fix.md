# TLS packaging fix — 2026-11-16

## Reproduced cause
The source worker reached DeepSeek and returned HTTP401 with a deliberately invalid diagnostic Key. The frozen worker failed earlier with ConnectionError wrapping FileNotFoundError. A full sanitized trace located the failure in ssl.SSLContext.__new__.

The Python runtime used for this build requires codex-rejected-cas.pem beside ssl.py. PyInstaller bundled ssl.py but omitted the906-byte sidecar. The failure happened before an HTTPS request could be sent.

## Fix
- desktop.spec includes this runtime sidecar at the frozen resource root when present in the build interpreter.
- The offline frozen self-test now constructs a real SSLContext with certificate verification enabled, so future missing TLS resources fail before release.
- Transport errors keep sanitized underlying reasons; API/proxy credentials are redacted.
- The final failed retry no longer incurs an unnecessary40-second sleep.
- Build output can be directed to a separate project-local release directory, preserving a running executable.

No certificate verification was disabled. No user Key, account state, firewall or proxy configuration was changed. The original1.3 project remains unchanged.

## Evidence
- Original frozen build: repeated ConnectionError(FileNotFoundError).
- Same frozen executable + missing sidecar restored: expected HTTP401 from DeepSeek using invalid diagnostic Key.
- The running extracted release executable matched the known original SHA256; the sidecar was restored there too.
-39 offline regression tests passed. No real AI generation request was made.

- Final frozen release self-test passed TLS, GUI, concurrent local acquisition, Excel, worker IPC and stop handling with isolated Python/browser paths.
- The current extracted user release was checked with the invalid diagnostic Key and now returns the expected HTTP401 instead of ConnectionError.

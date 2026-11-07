# 6. Security model and what was tested

## Who can reach what

| Port / service | Reachable from |
|---|---|
| Hermes (Telegram) | Only your Telegram account: pairing approval plus `TELEGRAM_ALLOWED_USERS` |
| Assistant service, VM 1 :8100 | VM 1 itself and your laptop (nftables) |
| Delegation bridge, VM 2 :8000 | VM 1 only, with a bearer token |
| freellmapi, VM 2 :3001 | VM 1 and your laptop (forward chain, since it's a Docker port) |
| Ollama, VM 2 :11434 | VM 1 only |
| Laptop Ollama :11434 | Tailscale addresses only (Windows firewall rule) |
| Reminder worker | Public URL, but everything except `/health` needs the shared secret |
| Dashboard | 127.0.0.1 only |

## Escape tests run from inside a DSH task

A task was told to run these and write the output to a file. Results:

| Attempt | Result |
|---|---|
| List other workspaces | Saw only its own |
| Read the user's home or the bridge token | "No such file or directory" (not mounted in the sandbox) |
| Read `/etc/dsh/runner.env` (the key file) | Permission denied |
| Secret variables visible to its shell | 0 |
| `sudo` | Blocked by no-new-privileges |
| Write to `/etc` | Read-only file system |
| Reach the laptop over Tailscale | Blocked |
| Reach freellmapi on localhost (needed) | Allowed |

Bridge tests: no token → 401; path traversal (`../`, `%2e%2e`, absolute paths) and symlinks out of the
workspace → refused; a 30 s timeout stopped the task at 30.5 s; cancel and a mid-task bridge restart both stopped
the sandbox, and an interrupted task is never re-run automatically. The freellmapi key never appeared in `ps`.

## Known gaps

- Inside a task, DSH's own process holds the freellmapi key in its environment (its shell tools don't). The key
  only works against freellmapi, which the firewall limits to localhost, VM 1 and the laptop.
- Free model providers, and on InstaBox the TLS-inspecting proxy, can see prompts. Keep secrets out of chats.
- The reminder worker stores reminder text at Cloudflare.

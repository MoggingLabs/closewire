# Security Policy

## Reporting a vulnerability

If you find a security issue — especially anything that could **leak credentials or tokens** —
please report it privately first. Open a
[GitHub Security Advisory](https://github.com/MoggingLabs/closewire/security/advisories/new) or
contact the maintainers directly rather than filing a public issue.

We aim to acknowledge reports within a few business days.

## Secrets never belong in this repo

This is a **public** repository. The following must **never** be committed:

- Closebot API keys, OAuth secrets, or session tokens
- `.env` files, HAR/network captures, browser storage dumps
- Conversation transcripts or any lead/contact data
- GoHighLevel location IDs, account IDs, or any tenant identifier tied to a real account

Only `.env.example` — with **placeholders** — is ever committed. Real values live in a local,
gitignored `.env`.

## If a key leaks

1. **Rotate it immediately** at the source (Closebot → revoke/regenerate the API key).
2. **Purge it from git history** (`git filter-repo` or BFG) and force-push.
3. **Rotate, don't just delete** — a removed commit may already be cloned or cached.

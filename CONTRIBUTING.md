# Contributing to Closewire

Thanks for taking the time to contribute!

## Ground rules

1. **Never commit secrets.** No API keys, `.env` files, transcripts, or account identifiers. See
   [SECURITY.md](./SECURITY.md). Copy `.env.example` to `.env` locally and keep `.env` gitignored.
2. **Keep the pacing layer intact.** Closewire routes every call through a polite rate limiter that
   respects Closebot's documented limits. Don't add functionality whose purpose is to burst-hammer
   the API or mass-target third parties.
3. **Be kind.** See the [Code of Conduct](./CODE_OF_CONDUCT.md).

## Workflow

```bash
# 1. Fork and clone
git clone git@github.com:<you>/closewire.git
cd closewire

# 2. Branch from main
git checkout -b feat/<short-description>

# 3. Make your change

# 4. Commit with a clear message
git commit -m "add bot persona update tool"

# 5. Push and open a PR against main
```

### Pull requests

- Keep PRs focused — one concern per PR.
- Describe **what** changed and **why**.
- Confirm you have not committed any secrets.
- If you added or changed an API endpoint mapping, document it in the same PR.

## Reporting bugs & requesting features

Open a [GitHub Issue](https://github.com/MoggingLabs/closewire/issues). Include repro steps,
expected vs. actual behavior, and your environment (OS, Python version). **Redact any API keys or
account IDs.**

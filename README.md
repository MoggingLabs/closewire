<div align="center">

# 🎙️ Closewire

**Configure and read [Closebot.ai](https://closebot.ai) — driven from Claude.**
Build and manage AI appointment-setter bots — personas, prompts, objection handling, and
conversations — through a custom MCP server over Closebot's API. One clean tool per action.

[![Closebot](https://img.shields.io/badge/Closebot-official%20API-6366f1?style=for-the-badge)](https://closebot.ai/)
[![Claude](https://img.shields.io/badge/Claude-MCP-d97757?style=for-the-badge&logo=anthropic&logoColor=white)](https://modelcontextprotocol.io/)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-6366f1?style=for-the-badge)](./LICENSE)

</div>

---

> **Status: phases 04-09 built and validated** (pacing, Tier-0 reads, read CLI, Tier-1 writes,
> Tier-2 publish/destroy, live-message runtime + test sessions). Phases 10-13 are the plan below,
> not yet built. Per-phase logs are in `docs/validation/`. Part of
> the [MoggingLabs Internals](https://github.com/MoggingLabs/mogginglabs-internals) driver family —
> see the [Roadmap](https://github.com/MoggingLabs/mogginglabs-internals/blob/main/ROADMAP.md).

## 🎯 What we're building

Closebot is the AI chatbot that qualifies leads and books appointments for our GoHighLevel
clients. Standing up a bot per client — persona, prompt, objection handling, then wiring it to a
sub-account — is repetitive UI work. **Closewire** wraps Closebot's API as an MCP server so Claude
can create, configure, and inspect bots conversationally.

Unlike its sibling [Highwire](https://github.com/MoggingLabs/highwire) — which has to *replay*
GoHighLevel's **internal** API — Closebot exposes a **full official API** ("everything the UI can
do, the API can do"), so Closewire is a clean wrapper over documented endpoints, authenticated with
an API key.

```
  You  ──▶  Claude  ──▶  Closewire MCP  ──▶  paced client  ──▶  Closebot API  ──▶  Your bots
                                              │
                                       polite rate limiting,
                                       retries, backoff, dry-run
```

## ✨ What this accomplishes (target)

| Capability | Closewire |
| :--- | :---: |
| List / create / update bots | ✅ |
| Set persona, prompt, objection handling | ✅ |
| Connect a bot to a GoHighLevel sub-account | ✅ |
| Read conversation transcripts | ✅ |
| Read booking / qualification outcomes | ✅ |
| Drive it all conversationally from Claude | ✅ |
| Polite, documented-limit-aware pacing | ✅ |

## 🧱 How it works

1. **API client** (Python) — typed functions over Closebot's official API, API-key auth.
2. **Pacing layer** — respects Closebot's documented rate limits, with jittered delays, retries,
   exponential backoff, and a dry-run mode. Be a good API citizen.
3. **MCP server** — wraps the client so Claude gets one tool per action.

## 🗺️ Roadmap

- [x] **v0.0** scaffold — three importable packages, `.env` config loading, CLI + MCP skeleton
- [ ] **v0.1** read-only — list bots, read transcripts + booking/qualification outcomes
- [ ] **v0.2** write — create/update a bot, push persona/prompt/objection handling
- [ ] **v0.3** wiring — connect a bot to a GHL sub-account end-to-end

## 🔐 Security — no keys in git

Your Closebot API key lives **only** in a local, gitignored `.env`. This repo ships
`.env.example` with **placeholders only**. See [SECURITY.md](./SECURITY.md). If a key leaks,
**rotate it at the source** — deleting the commit is not enough.

## ⚖️ Responsible use

Closewire automates **your own** Closebot account through its official API. Not affiliated with,
endorsed by, or sponsored by Closebot or Anthropic. MIT, no warranty.

## 🤝 Contributing

PRs welcome — see [CONTRIBUTING.md](./CONTRIBUTING.md). Keep the pacing layer intact and never
commit secrets.

## 📄 License

[MIT](./LICENSE) © MoggingLabs.

<div align="center"><sub>Part of <a href="https://github.com/MoggingLabs/mogginglabs-internals">MoggingLabs Internals</a> · one wire per platform 🎪</sub></div>

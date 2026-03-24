# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Discord bot that exports server messages to JSON and generates AI-powered daily team digests (via Claude Haiku) saved as Obsidian-compatible markdown. Digests are organized into monthly subfolders and optionally pushed to GitHub.

The main goal of this project is to automate the ingestion of complex daily decision making and status updates into our Obsidian knowledge base, so that I, as the product lead and project manager can make quicker and more informed decisions. Full automation with strong context retrieval is the ideal scenario.

## Commands

```bash
pip install -r requirements.txt   # Install deps
python bot.py                      # Run the bot
docker build -t discord-exporter-bot .  # Build Docker image
docker run --env-file .env discord-exporter-bot  # Run in Docker
```

No test suite or linter exists.

## Architecture

**Single-file application** — everything lives in `bot.py` (~710 lines), organized into sections:

1. **Config** — `Config` dataclass with `from_env()` factory, loads all env vars
2. **Utilities** — Timezone conversion (Eastern Time via `zoneinfo`), mention resolution, message serialization, stats
3. **Core Export** — Concurrent channel export to JSON via `asyncio.gather`
4. **Claude Integration** — Transcript preparation and Anthropic API call for digest generation
5. **Obsidian Output** — Frontmatter formatting, file saving into `YYYY-MM/` subfolders, wikilink navigation between days
6. **Git Integration** — Clone/init repo, commit/push digest files to GitHub via `subprocess.run`
7. **Digest Pipeline** — `run_digest_pipeline()` orchestrates: export → analyze → save → push
8. **Discord Bot** — `!export [hours]` and `!digest [hours]` commands, scheduled 24h task at midnight ET

## Key Env Vars

Required: `DISCORD_TOKEN`, `GUILD_ID`. Optional: `ANTHROPIC_API_KEY` (for digests), `GITHUB_REPO_URL` + `GITHUB_TOKEN` (for push), `DOKPLOY_VOLUME_PATH` (Dokploy volume mount).

## Design Patterns

- Git operations use `subprocess.run` with token-injected HTTPS URLs
- Scheduled task uses `discord.ext.tasks.loop(hours=24)` with timezone-aware wait
- All timestamps normalized to Eastern Time with auto-DST
- Digests include Obsidian frontmatter and `[[wikilink]]` navigation between days

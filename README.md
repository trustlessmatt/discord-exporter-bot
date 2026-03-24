# discord-exporter-bot

A Discord bot that exports channel messages to JSON and generates AI-powered daily digests using Claude, saved as Obsidian-formatted markdown files.

## What it does

- **Exports** all text channel messages from a Discord server to a timestamped JSON file
- **Generates daily digests** using Claude (Haiku) — summarizing individual updates, blockers, decisions, and action items per team member
- **Saves digests** as Obsidian markdown with frontmatter, organized into `YYYY-MM/` subfolders
- **Pushes to GitHub** automatically if a repo is configured
- **Runs on a schedule** — generates a digest every 24 hours at midnight ET

## Commands

| Command | Description |
|---------|-------------|
| `!export [hours]` | Export messages from the last N hours (default: 24, max: 720) |
| `!digest [hours]` | Generate and save an AI digest from the last N hours |

## Setup

### Requirements

- Python 3.11+
- Discord bot token with `message_content` and `members` intents enabled

### Install

```bash
pip install -r requirements.txt
```

### Environment variables

Create a `.env` file:

```env
DISCORD_TOKEN=your_bot_token
GUILD_ID=your_guild_id
ANTHROPIC_API_KEY=your_anthropic_key

# Optional: push digests to a GitHub repo
GITHUB_REPO_URL=https://github.com/org/repo.git
GITHUB_TOKEN=your_github_pat

# Optional: write digests to a Dokploy-mounted volume
DOKPLOY_VOLUME_PATH=/path/to/volume
```

### Run

```bash
python bot.py
```

### Docker

```bash
docker build -t discord-exporter-bot .
docker run --env-file .env discord-exporter-bot
```

## Output

**JSON exports** are saved to `exports/discord_export_YYYYMMDD_HHMMSS_ET.json`.

**Digests** are saved to `Daily Digests/YYYY-MM/YYYY-MM-DD - Team Digest.md` (or the configured Dokploy volume path) with Obsidian frontmatter and prev/next day navigation links.

## Dependencies

- `discord.py` — Discord API client
- `anthropic` — Claude API for digest generation
- `python-dotenv` — environment variable loading

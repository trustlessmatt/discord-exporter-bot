import discord
from discord.ext import commands, tasks
import json
import os
import logging
import re
import asyncio
import subprocess
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import anthropic

# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    """Bot configuration loaded from environment variables."""
    discord_token: str
    guild_id: int
    anthropic_api_key: Optional[str] = None
    dokploy_volume_path: Optional[str] = None
    github_repo_url: Optional[str] = None
    github_token: Optional[str] = None
    eastern_tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("America/New_York"))
    digest_model: str = "claude-haiku-4-5-20251001"
    digest_max_tokens: int = 4096
    default_hours: int = 24
    min_hours: int = 1
    max_hours: int = 720
    exports_dir: str = "exports"
    digests_dir: str = "Daily Digests"
    digest_preview_length: int = 1500
    scheduled_hour: int = 0  # 12am ET

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        load_dotenv()

        token = os.getenv("DISCORD_TOKEN")
        guild_id = os.getenv("GUILD_ID")

        if not token:
            raise ValueError("DISCORD_TOKEN not found in .env file")
        if not guild_id:
            raise ValueError("GUILD_ID not found in .env file")

        return cls(
            discord_token=token,
            guild_id=int(guild_id),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            dokploy_volume_path=os.getenv("DOKPLOY_VOLUME_PATH"),
            github_repo_url=os.getenv("GITHUB_REPO_URL"),
            github_token=os.getenv("GITHUB_TOKEN"),
        )

# =============================================================================
# Set up logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# =============================================================================
# Regex patterns (compiled once for performance)
# =============================================================================

USER_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
CHANNEL_MENTION_PATTERN = re.compile(r"<#(\d+)>")
ROLE_MENTION_PATTERN = re.compile(r"<@&(\d+)>")

# =============================================================================
# Utility Functions
# =============================================================================

def convert_to_eastern(utc_time: datetime, tz: ZoneInfo) -> datetime:
    """Convert UTC datetime to target timezone (handles DST automatically)."""
    if utc_time.tzinfo is None:
        utc_time = utc_time.replace(tzinfo=timezone.utc)
    return utc_time.astimezone(tz)


def replace_mention(pattern: re.Pattern, content: str, guild, resolver) -> str:
    """Generic function to replace Discord mentions with readable text."""
    if not content:
        return content

    def replace(match):
        entity_id = int(match.group(1))
        entity = resolver(entity_id)
        if entity:
            prefix = "@" if pattern in (USER_MENTION_PATTERN, ROLE_MENTION_PATTERN) else "#"
            return f"{prefix}{entity.name}"
        return match.group(0)

    return pattern.sub(replace, content)


def clean_content(content: str, guild) -> str:
    """Replace all Discord mentions with readable text."""
    content = replace_mention(USER_MENTION_PATTERN, content, guild, guild.get_member)
    content = replace_mention(CHANNEL_MENTION_PATTERN, content, guild, guild.get_channel)
    content = replace_mention(ROLE_MENTION_PATTERN, content, guild, guild.get_role)
    return content


def serialize_message(message, guild, config: Config) -> dict:
    """Convert a Discord message to a serializable dictionary."""
    eastern_time = convert_to_eastern(message.created_at, config.eastern_tz)
    edited_eastern = convert_to_eastern(message.edited_at, config.eastern_tz) if message.edited_at else None

    return {
        "message_id": str(message.id),
        "author": {
            "name": message.author.name,
            "display_name": message.author.display_name,
            "id": str(message.author.id),
            "bot": message.author.bot
        },
        "content": message.content,
        "content_clean": clean_content(message.content, guild),
        "timestamp": eastern_time.isoformat(),
        "timestamp_utc": message.created_at.isoformat(),
        "edited_timestamp": edited_eastern.isoformat() if edited_eastern else None,
        "attachments": [
            {"filename": att.filename, "url": att.url, "size": att.size}
            for att in message.attachments
        ],
        "embeds": len(message.embeds),
        "reactions": [
            {"emoji": str(reaction.emoji), "count": reaction.count}
            for reaction in message.reactions
        ],
        "mentions": [
            {"id": str(user.id), "name": user.name, "display_name": user.display_name}
            for user in message.mentions
        ],
        "channel_mentions": [
            {"id": str(ch.id), "name": ch.name}
            for ch in message.channel_mentions
        ],
        "thread": message.thread.name if hasattr(message, "thread") and message.thread else None
    }


def calculate_export_stats(export_data: dict) -> dict:
    """Calculate statistics from export data."""
    total_messages = sum(len(ch["messages"]) for ch in export_data["channels"].values())
    active_channels = len(export_data["channels"])

    contributors = {
        msg["author"]["display_name"]
        for channel_data in export_data["channels"].values()
        for msg in channel_data["messages"]
        if not msg["author"]["bot"]
    }

    return {
        "total_messages": total_messages,
        "active_channels": active_channels,
        "contributors": contributors,
        "contributor_count": len(contributors)
    }


def filter_bot_messages(messages: list) -> list:
    """Filter out messages from bots."""
    return [m for m in messages if not m["author"]["bot"]]

# =============================================================================
# Core Export Functions
# =============================================================================

async def export_channel_messages(channel, hours: int, config: Config) -> list:
    """Export messages from a channel within the last X hours."""
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
    messages_data = []
    guild = channel.guild

    try:
        async for message in channel.history(limit=None, after=cutoff_time):
            messages_data.append(serialize_message(message, guild, config))

        logger.info(f"Exported {len(messages_data)} messages from #{channel.name}")
        return messages_data

    except discord.Forbidden:
        logger.warning(f"No permission to read #{channel.name}")
        return []
    except Exception as e:
        logger.error(f"Error exporting #{channel.name}: {e}")
        return []


async def perform_export(bot: commands.Bot, config: Config, hours: int) -> Optional[dict]:
    """Perform the export operation across all channels concurrently."""
    guild = bot.get_guild(config.guild_id)
    if not guild:
        logger.error(f"Guild with ID {config.guild_id} not found")
        return None

    logger.info(f"Starting export for last {hours} hours from {guild.name}")

    export_time_utc = datetime.now(timezone.utc)
    export_time_eastern = convert_to_eastern(export_time_utc, config.eastern_tz)

    export_data = {
        "guild_name": guild.name,
        "export_time_eastern": export_time_eastern.isoformat(),
        "export_time_utc": export_time_utc.isoformat(),
        "timezone": "America/New_York (Eastern Time - auto DST)",
        "time_range_hours": hours,
        "channels": {}
    }

    # Export all channels concurrently for better performance
    tasks_list = [export_channel_messages(ch, hours, config) for ch in guild.text_channels]
    results = await asyncio.gather(*tasks_list)

    for channel, messages in zip(guild.text_channels, results):
        if messages:
            export_data["channels"][channel.name] = {
                "channel_id": str(channel.id),
                "messages": messages
            }

    # Save to JSON
    filename = f"{config.exports_dir}/discord_export_{export_time_eastern.strftime('%Y%m%d_%H%M%S')}_ET.json"
    os.makedirs(config.exports_dir, exist_ok=True)

    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)

        stats = calculate_export_stats(export_data)
        logger.info(f"Export complete: {stats['total_messages']} messages from {stats['active_channels']} channels")

        return {
            "filename": filename,
            "total_messages": stats["total_messages"],
            "channel_count": stats["active_channels"],
            "export_data": export_data,
            "stats": stats
        }
    except Exception as e:
        logger.error(f"Error saving export: {e}")
        return None


# =============================================================================
# Claude Anthropic Integration
# =============================================================================

def get_anthropic_client(api_key: str) -> anthropic.Anthropic:
    """Get or create Anthropic client (reusable)."""
    return anthropic.Anthropic(api_key=api_key)


def prepare_transcript(export_data: dict) -> str:
    """Prepare transcript for Claude analysis from export data."""
    channel_summaries = []

    for channel_name, channel_data in export_data["channels"].items():
        non_bot_messages = filter_bot_messages(channel_data["messages"])
        if not non_bot_messages:
            continue

        messages_text = "\n".join([
            f"[{msg['timestamp'][:19]}] {msg['author']['display_name']}: {msg['content_clean']}"
            for msg in non_bot_messages
        ])

        channel_summaries.append(f"## Channel: #{channel_name}\n{messages_text}\n")

    return "\n".join(channel_summaries) if channel_summaries else "No messages to analyze."


async def generate_daily_digest(export_data: dict, config: Config) -> Optional[str]:
    """Use Claude to generate structured daily digest."""
    if not config.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return None

    transcript = prepare_transcript(export_data)

    try:
        client = get_anthropic_client(config.anthropic_api_key)

        prompt = f"""Analyze this Discord transcript from the last 24 hours and create a structured daily digest for a team manager.

Focus on extracting:

1. **Individual Updates** - What each team member worked on, completed, or made progress on
2. **Upcoming Work** - What team members mentioned they're planning to work on next
3. **Blockers & Challenges** - Any obstacles, issues, or requests for help
4. **Key Decisions & Ideas** - Important discussions, decisions made, or ideas that shouldn't be lost
5. **Action Items** - Specific TODOs or follow-ups mentioned

Be concise but don't lose important technical details. Organize by person where possible.
If there's very little activity, just note that briefly.

Transcript:
{transcript}
"""

        message = client.messages.create(
            model=config.digest_model,
            max_tokens=config.digest_max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )

        return message.content[0].text

    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        return None


# =============================================================================
# Obsidian/Markdown Output
# =============================================================================

def get_output_path(config: Config) -> str:
    """Determine the output path for digests."""
    if config.dokploy_volume_path and os.path.exists(config.dokploy_volume_path):
        logger.info(f"Saving to dokploy volume: {config.dokploy_volume_path}/{config.digests_dir}")
        return f"{config.dokploy_volume_path}/{config.digests_dir}"
    logger.info(f"Dokploy volume not found, saving to local: {config.digests_dir}")
    return config.digests_dir


def format_obsidian_document(date_str: str, digest_content: str, stats: dict, config: Config) -> str:
    """Create Obsidian-formatted document."""
    prev_day = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    return f"""---
date: {date_str}
type: daily-digest
tags: [team, standup, daily]
contributors: {stats['contributor_count']}
messages: {stats['total_messages']}
channels: {stats['active_channels']}
---

# Team Digest - {date_str}

**📊 Activity Summary**
- {stats['total_messages']} messages across {stats['active_channels']} channels
- {stats['contributor_count']} active team members
- Time range: Last 24 hours

---

{digest_content}

---

**🔗 Links**
- [[{prev_day} - Team Digest|← Previous Day]]
- [[{next_day} - Team Digest|Next Day →]]

---
*Auto-generated at {datetime.now(config.eastern_tz).strftime('%I:%M %p ET')} from Discord*
"""


async def save_digest(digest_content: str, date_str: str, stats: dict, config: Config) -> str:
    """Save digest to local directory and push to GitHub."""
    output_path = get_output_path(config)

    # Initialize git repo if configured
    if config.github_repo_url:
        init_git_repo(output_path, config)

    os.makedirs(output_path, exist_ok=True)

    filename = f"{output_path}/{date_str} - Team Digest.md"
    obsidian_content = format_obsidian_document(date_str, digest_content, stats, config)

    with open(filename, "w", encoding="utf-8") as f:
        f.write(obsidian_content)

    logger.info(f"Saved digest to {filename}")

    # Commit and push to GitHub
    if config.github_repo_url:
        git_commit_and_push(filename, date_str, config)

    return filename


def init_git_repo(output_path: str, config: Config) -> bool:
    """Initialize or clone git repository."""
    git_dir = os.path.join(output_path, ".git")

    # If .git exists, just pull latest
    if os.path.exists(git_dir):
        try:
            subprocess.run(
                ["git", "-C", output_path, "pull"],
                check=True,
                capture_output=True,
                text=True
            )
            logger.info("Git repo updated")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Git pull failed: {e.stderr}")
            return False

    # Clone the repo
    if not config.github_repo_url or not config.github_token:
        logger.error("GitHub repo URL or token not configured")
        return False

    # Format: https://TOKEN@github.com/username/repo.git
    auth_url = config.github_repo_url.replace(
        "https://",
        f"https://{config.github_token}@"
    )

    try:
        # Clone to temp directory first
        temp_dir = output_path + "_temp"
        subprocess.run(
            ["git", "clone", auth_url, temp_dir],
            check=True,
            capture_output=True,
            text=True
        )

        # Move .git directory
        shutil.move(os.path.join(temp_dir, ".git"), output_path)
        shutil.rmtree(temp_dir)

        logger.info("Git repo cloned successfully")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Git clone failed: {e.stderr}")
        return False


def git_commit_and_push(file_path: str, date_str: str, config: Config) -> bool:
    """Commit and push the digest file to GitHub."""
    if not config.github_repo_url or not config.github_token:
        logger.warning("GitHub not configured, skipping push")
        return False

    output_path = os.path.dirname(file_path)

    try:
        # Configure git user (required for commits)
        subprocess.run(
            ["git", "-C", output_path, "config", "user.name", "Discord Bot"],
            check=True,
            capture_output=True
        )
        subprocess.run(
            ["git", "-C", output_path, "config", "user.email", "bot@discord.local"],
            check=True,
            capture_output=True
        )

        # Add the file
        subprocess.run(
            ["git", "-C", output_path, "add", os.path.basename(file_path)],
            check=True,
            capture_output=True
        )

        # Commit
        commit_message = f"Daily digest for {date_str}"
        subprocess.run(
            ["git", "-C", output_path, "commit", "-m", commit_message],
            check=True,
            capture_output=True,
            text=True
        )

        # Push with authentication
        auth_url = config.github_repo_url.replace(
            "https://",
            f"https://{config.github_token}@"
        )

        subprocess.run(
            ["git", "-C", output_path, "push", auth_url, "main"],
            check=True,
            capture_output=True,
            text=True
        )

        logger.info(f"Successfully pushed {file_path} to GitHub")
        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"Git operation failed: {e.stderr if hasattr(e, 'stderr') else str(e)}")
        return False


# =============================================================================
# Digest Pipeline (reusable)
# =============================================================================

async def run_digest_pipeline(bot: commands.Bot, config: Config, hours: int) -> Optional[dict]:
    """Run the complete digest pipeline: export, analyze, save."""
    result = await perform_export(bot, config, hours)
    if not result:
        return {"success": False, "error": "Export failed"}

    digest = await generate_daily_digest(result["export_data"], config)
    if not digest:
        return {"success": False, "error": "Failed to generate digest"}

    date_str = datetime.now(config.eastern_tz).strftime("%Y-%m-%d")
    digest_path = await save_digest(digest, date_str, result["stats"], config)

    return {
        "success": True,
        "digest": digest,
        "digest_path": digest_path,
        "stats": result["stats"],
        "export_filename": result["filename"]
    }


# =============================================================================
# Discord Bot Setup
# =============================================================================

config = Config.from_env()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    logger.info(f"{bot.user} has connected to Discord!")
    logger.info(f"Bot is in {len(bot.guilds)} guild(s)")

    guild = bot.get_guild(config.guild_id)
    if guild:
        logger.info(f"Connected to: {guild.name}")
        logger.info(f"Text channels: {len(guild.text_channels)}")
        for channel in guild.text_channels:
            logger.info(f"  - {channel.name}")

    if not daily_digest_task.is_running():
        daily_digest_task.start()
        logger.info("Daily digest task started")


@bot.command(name="export")
async def export_now(ctx, hours: int = None):
    """Manually trigger export of last X hours."""
    hours = hours or config.default_hours
    if hours < config.min_hours or hours > config.max_hours:
        await ctx.send(f"Hours must be between {config.min_hours} and {config.max_hours} (30 days)")
        return

    await ctx.send(f"Starting export of last {hours} hours...")

    result = await perform_export(bot, config, hours)

    if result:
        await ctx.send(
            f"✅ Export complete!\n"
            f"• {result['total_messages']} messages\n"
            f"• {result['channel_count']} channels\n"
            f"• Saved to `{result['filename']}`"
        )
    else:
        await ctx.send("❌ Export failed. Check the logs for details.")


@bot.command(name="digest")
async def generate_digest_command(ctx, hours: int = None):
    """Generate AI-powered daily digest from Discord activity."""
    hours = hours or config.default_hours
    if hours < config.min_hours or hours > config.max_hours:
        await ctx.send(f"Hours must be between {config.min_hours} and {config.max_hours} (30 days)")
        return

    await ctx.send(f"🤖 Generating digest for last {hours} hours...")

    result = await run_digest_pipeline(bot, config, hours)

    if result["success"]:
        preview = result["digest"]
        if len(preview) > config.digest_preview_length:
            preview = preview[:config.digest_preview_length] + "..."

        await ctx.send(
            f"✅ **Daily Digest Generated**\n\n{preview}\n\n"
            f"📁 Full digest: `{result['digest_path']}`"
        )
    else:
        await ctx.send(f"❌ {result['error']}. Check the logs for details.")


@tasks.loop(hours=24)
async def daily_digest_task():
    """Automatically generate daily digest at scheduled time."""
    logger.info("Running scheduled daily digest")

    result = await run_digest_pipeline(bot, config, config.default_hours)

    if result["success"]:
        logger.info("✅ Daily digest completed successfully")
    else:
        logger.error(f"Daily digest failed: {result.get('error', 'Unknown error')}")


@daily_digest_task.before_loop
async def before_daily_digest():
    """Wait until scheduled time to start the loop."""
    await bot.wait_until_ready()

    now = datetime.now(config.eastern_tz)
    target_time = now.replace(hour=config.scheduled_hour, minute=0, second=0, microsecond=0)

    if now.time() >= target_time.time():
        target_time += timedelta(days=1)

    wait_seconds = (target_time - now).total_seconds()
    logger.info(f"⏰ Waiting {wait_seconds/3600:.1f} hours until first digest at {config.scheduled_hour}:00am ET")
    await asyncio.sleep(wait_seconds)


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    bot.run(config.discord_token)

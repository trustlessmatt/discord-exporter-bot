import discord
from discord.ext import commands, tasks
import json
import os
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = os.getenv('GUILD_ID')

# Timezone configuration
EASTERN_TZ = ZoneInfo("America/New_York")

# Validate environment variables
if not TOKEN:
    raise ValueError("DISCORD_TOKEN not found in .env file")
if not GUILD_ID:
    raise ValueError("GUILD_ID not found in .env file")

GUILD_ID = int(GUILD_ID)

# Set up intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Create bot instance
bot = commands.Bot(command_prefix='!', intents=intents)

def convert_to_eastern(utc_time):
    """Convert UTC datetime to Eastern Time (handles DST automatically)"""
    if utc_time.tzinfo is None:
        utc_time = utc_time.replace(tzinfo=timezone.utc)
    return utc_time.astimezone(EASTERN_TZ)

def replace_user_mentions(content, guild):
    """Replace <@USER_ID> mentions with @username"""
    if not content:
        return content
    
    # Regex to match <@USER_ID> or <@!USER_ID>
    mention_pattern = r'<@!?(\d+)>'
    
    def replace_mention(match):
        user_id = int(match.group(1))
        member = guild.get_member(user_id)
        if member:
            return f"@{member.name}"
        return match.group(0)  # Keep original if user not found
    
    return re.sub(mention_pattern, replace_mention, content)

def replace_channel_mentions(content, guild):
    """Replace <#CHANNEL_ID> mentions with #channel-name"""
    if not content:
        return content
    
    channel_pattern = r'<#(\d+)>'
    
    def replace_channel(match):
        channel_id = int(match.group(1))
        channel = guild.get_channel(channel_id)
        if channel:
            return f"#{channel.name}"
        return match.group(0)
    
    return re.sub(channel_pattern, replace_channel, content)

def replace_role_mentions(content, guild):
    """Replace <@&ROLE_ID> mentions with @role-name"""
    if not content:
        return content
    
    role_pattern = r'<@&(\d+)>'
    
    def replace_role(match):
        role_id = int(match.group(1))
        role = guild.get_role(role_id)
        if role:
            return f"@{role.name}"
        return match.group(0)
    
    return re.sub(role_pattern, replace_role, content)

def clean_content(content, guild):
    """Replace all Discord mentions with readable text"""
    content = replace_user_mentions(content, guild)
    content = replace_channel_mentions(content, guild)
    content = replace_role_mentions(content, guild)
    return content

@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    logger.info(f'Bot is in {len(bot.guilds)} guild(s)')
    
    guild = bot.get_guild(GUILD_ID)
    if guild:
        logger.info(f'Connected to: {guild.name}')
        logger.info(f'Text channels: {len(guild.text_channels)}')
        for channel in guild.text_channels:
            logger.info(f'  - {channel.name}')

async def export_channel_messages(channel, hours=24):
    """Export messages from a channel within the last X hours"""
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
    messages_data = []
    guild = channel.guild
    
    try:
        async for message in channel.history(limit=None, after=cutoff_time):
            # Convert timestamp to Eastern Time
            eastern_time = convert_to_eastern(message.created_at)
            edited_eastern = convert_to_eastern(message.edited_at) if message.edited_at else None
            
            message_data = {
                "message_id": str(message.id),
                "author": {
                    "name": message.author.name,
                    "display_name": message.author.display_name,
                    "id": str(message.author.id),
                    "bot": message.author.bot
                },
                "content": message.content,  # Original with Discord formatting
                "content_clean": clean_content(message.content, guild),  # Readable version
                "timestamp": eastern_time.isoformat(),  # Eastern Time
                "timestamp_utc": message.created_at.isoformat(),  # Keep UTC for reference
                "edited_timestamp": edited_eastern.isoformat() if edited_eastern else None,
                "attachments": [
                    {
                        "filename": att.filename,
                        "url": att.url,
                        "size": att.size
                    } for att in message.attachments
                ],
                "embeds": len(message.embeds),
                "reactions": [
                    {
                        "emoji": str(reaction.emoji),
                        "count": reaction.count
                    } for reaction in message.reactions
                ],
                "mentions": [
                    {
                        "id": str(user.id),
                        "name": user.name,
                        "display_name": user.display_name
                    } for user in message.mentions
                ],
                "channel_mentions": [
                    {
                        "id": str(ch.id),
                        "name": ch.name
                    } for ch in message.channel_mentions
                ],
                "thread": message.thread.name if hasattr(message, 'thread') and message.thread else None
            }
            messages_data.append(message_data)
        
        logger.info(f"Exported {len(messages_data)} messages from #{channel.name}")
        return messages_data
    
    except discord.Forbidden:
        logger.warning(f"No permission to read #{channel.name}")
        return []
    except Exception as e:
        logger.error(f"Error exporting #{channel.name}: {e}")
        return []

async def perform_export(hours=24, output_dir="exports"):
    """Perform the export operation - can be called by commands or scheduled tasks"""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        logger.error(f"Guild with ID {GUILD_ID} not found")
        return None
    
    logger.info(f"Starting export for last {hours} hours from {guild.name}")
    
    export_time_utc = datetime.now(timezone.utc)
    export_time_eastern = convert_to_eastern(export_time_utc)
    
    export_data = {
        "guild_name": guild.name,
        "export_time_eastern": export_time_eastern.isoformat(),
        "export_time_utc": export_time_utc.isoformat(),
        "timezone": "America/New_York (Eastern Time - auto DST)",
        "time_range_hours": hours,
        "channels": {}
    }
    
    for channel in guild.text_channels:
        messages = await export_channel_messages(channel, hours)
        if messages:
            export_data["channels"][channel.name] = {
                "channel_id": str(channel.id),
                "messages": messages
            }
    
    # Save to JSON with Eastern timestamp in filename
    filename = f"{output_dir}/discord_export_{export_time_eastern.strftime('%Y%m%d_%H%M%S')}_ET.json"
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        
        total_messages = sum(len(ch["messages"]) for ch in export_data["channels"].values())
        logger.info(f"Export complete: {total_messages} messages from {len(export_data['channels'])} channels")
        
        return {
            "filename": filename,
            "total_messages": total_messages,
            "channel_count": len(export_data["channels"]),
            "export_data": export_data
        }
    except Exception as e:
        logger.error(f"Error saving export: {e}")
        return None

@bot.command(name='export')
async def export_now(ctx, hours: int = 24):
    """Manually trigger export of last X hours"""
    if hours < 1 or hours > 720:  # Max 30 days
        await ctx.send("Hours must be between 1 and 720 (30 days)")
        return
    
    await ctx.send(f'Starting export of last {hours} hours...')
    
    result = await perform_export(hours)
    
    if result:
        await ctx.send(
            f'✅ Export complete!\n'
            f'• {result["total_messages"]} messages\n'
            f'• {result["channel_count"]} channels\n'
            f'• Saved to `{result["filename"]}`'
        )
    else:
        await ctx.send('❌ Export failed. Check the logs for details.')

# Run the bot
if __name__ == "__main__":
    bot.run(TOKEN)
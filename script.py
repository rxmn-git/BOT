import asyncio
import os
import re
import discord
import spotipy
import logging
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from discord.ext import commands
from discord import app_commands

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load .env
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
SPOTIPY_REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI')
SPOTIFY_USERNAME = os.getenv('SPOTIFY_USERNAME')
SPOTIFY_PLAYLIST_NAME = os.getenv('SPOTIFY_PLAYLIST_NAME')
CHANNEL_ID = int(os.getenv('CHANNEL_ID'))

# Spotify setup
scope = 'playlist-modify-public playlist-modify-private'
sp_oauth = SpotifyOAuth(
    client_id=SPOTIPY_CLIENT_ID,
    client_secret=SPOTIPY_CLIENT_SECRET,
    redirect_uri=SPOTIPY_REDIRECT_URI,
    scope=scope,
    username=SPOTIFY_USERNAME,
    cache_path=".spotify_token_cache"
)
sp = spotipy.Spotify(auth_manager=sp_oauth)
logger.info(f"[SPOTIFY] Logged in as: {sp.current_user()['display_name']}")

# Playlist handling
def get_or_create_playlist():
    playlists = sp.current_user_playlists(limit=50)
    for playlist in playlists['items']:
        if playlist['name'].lower() == SPOTIFY_PLAYLIST_NAME.lower():
            logger.info(f"[PLAYLIST] Found existing: {playlist['name']}")
            return playlist['id'], playlist['external_urls']['spotify']
    new = sp.user_playlist_create(SPOTIFY_USERNAME, SPOTIFY_PLAYLIST_NAME)
    logger.info(f"[PLAYLIST] Created new: {SPOTIFY_PLAYLIST_NAME}")
    return new['id'], new['external_urls']['spotify']

playlist_id, playlist_url = get_or_create_playlist()

def get_existing_track_ids(playlist_id):
    track_ids = set()
    results = sp.playlist_items(playlist_id)
    while results:
        track_ids.update([item['track']['id'] for item in results['items'] if item.get('track')])
        if results['next']:
            results = sp.next(results)
        else:
            break
    return track_ids

SPOTIFY_LINK_REGEX = r'https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(?P<type>track|album|playlist)/(?P<id>[a-zA-Z0-9]+)'

def extract_track_ids_from_text(text, existing_ids):
    matches = re.findall(SPOTIFY_LINK_REGEX, text)
    new_ids = []

    for link_type, spotify_id in matches:
        try:
            if link_type == 'track':
                if spotify_id not in existing_ids:
                    new_ids.append(spotify_id)
            elif link_type == 'album':
                album_tracks = sp.album_tracks(spotify_id)['items']
                for track in album_tracks:
                    tid = track['id']
                    if tid and tid not in existing_ids:
                        new_ids.append(tid)
            elif link_type == 'playlist':
                results = sp.playlist_items(spotify_id)
                while results:
                    for item in results['items']:
                        track = item.get('track')
                        if track:
                            tid = track['id']
                            if tid and tid not in existing_ids:
                                new_ids.append(tid)
                    if results['next']:
                        results = sp.next(results)
                    else:
                        break
        except Exception as e:
            logger.warning(f"[SPOTIFY ERROR] {link_type}:{spotify_id} - {e}")
    
    return new_ids

# Discord bot setup with commands
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    await tree.sync()
    logger.info(f"[DISCORD] Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.channel.id != CHANNEL_ID or message.author == bot.user:
        return

    existing_ids = get_existing_track_ids(playlist_id)
    new_ids = extract_track_ids_from_text(message.content, existing_ids)

    if new_ids:
        try:
            sp.playlist_add_items(playlist_id, new_ids, position=0)
            logger.info(f"[SPOTIFY] Added from message: {new_ids}")
            await message.channel.send(f"Added {len(new_ids)} track(s).", delete_after=5)
        except Exception as e:
            logger.error(f"[SPOTIFY ERROR] Adding from message: {e}")
            await message.channel.send("Failed to add track(s).", delete_after=5)

# Slash command: /playlist
@tree.command(name="playlist", description="Post the link to the Spotify playlist.")
async def playlist_command(interaction: discord.Interaction):
    await interaction.response.send_message(f"🎧 {playlist_url}")

# Slash command: /sync
@tree.command(name="sync", description="Read past messages and adds the Spotify songs")
async def sync_command(interaction: discord.Interaction):
    if interaction.channel.id != CHANNEL_ID:
        return await interaction.response.send_message("This command can only be used in #music-recs.", ephemeral=True)

    await interaction.response.send_message("Reading previous messages...", delete_after=500)
    existing_ids = get_existing_track_ids(playlist_id)
    total_new_ids = []

    async for message in interaction.channel.history(limit=500):
        new_ids = extract_track_ids_from_text(message.content, existing_ids)
        total_new_ids.extend(new_ids)
        existing_ids.update(new_ids)

    if total_new_ids:
        try:
            sp.playlist_add_items(playlist_id, list(reversed(new_ids)), position=0)
            logger.info(f"[SPOTIFY] Added from history: {total_new_ids}")
            await interaction.followup.send(f"Added {len(total_new_ids)} new track(s) from message history.")
            
        except Exception as e:
            logger.error(f"[SPOTIFY ERROR] Adding from history: {e}")
            await interaction.followup.send("Failed to add tracks from history.")
    else:
        await interaction.followup.send("No new tracks found in previous messages.")

bot.run(DISCORD_TOKEN)

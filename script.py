import asyncio
import os
import re
import discord
import spotipy
import logging
import json
import asyncpg
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

# PostgreSQL
DATABASE_URL = os.getenv("postgresql://postgres:RlLgrpOOFVCRULMgQqiMhHZtSsMMRVST@shortline.proxy.rlwy.net:25650/railway")
db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scoreboard (
                user_id TEXT PRIMARY KEY,
                count INTEGER NOT NULL
            );
        """)

async def increment_score(user_id: str, amount: int = 1):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO scoreboard (user_id, count)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE
            SET count = scoreboard.count + $2;
        """, user_id, amount)

async def get_scoreboard(limit=10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, count FROM scoreboard
            ORDER BY count DESC
            LIMIT $1
        """, limit)
        return rows



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

# Función para mantener la playlist en tamaño máximo 64
def trim_playlist(playlist_id, max_size=64):
    tracks = []
    results = sp.playlist_items(playlist_id, fields="items.track.uri,next,total", limit=100)
    tracks.extend(results['items'])

    while results['next']:
        results = sp.next(results)
        tracks.extend(results['items'])

    total_tracks = len(tracks)

    if total_tracks > max_size:
        to_remove_count = total_tracks - max_size
        # Eliminar las canciones del final de la lista (las más antiguas)
        tracks_to_remove = tracks[-to_remove_count:]
        uris_to_remove = [item['track']['uri'] for item in tracks_to_remove if item.get('track')]

        sp.playlist_remove_all_occurrences_of_items(playlist_id, uris_to_remove)
        logger.info(f"[SPOTIFY] Removed {to_remove_count} oldest tracks (from end) to trim playlist.")


# Discord bot setup with commands
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

@bot.event
async def on_ready():

    await init_db()
     # Lista de servidores donde quieres forzar el sync
    guild_ids = [597970988587810816]

    for gid in guild_ids:
        guild = discord.Object(id=gid)
        await tree.sync(guild=guild)
        logger.info(f"[DISCORD] Slash commands synced to guild: {gid}")

    logger.info(f"[DISCORD] Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.channel.id != CHANNEL_ID or message.author == bot.user:
        return
    
    user_id = str(message.author.id)
    existing_ids = get_existing_track_ids(playlist_id)
    new_ids = extract_track_ids_from_text(message.content, existing_ids)

    if new_ids:
        try:
            # Check current playlist size
            results = sp.playlist_items(playlist_id, fields="total", limit=1)
            current_total = results['total']
            total_after_add = current_total + len(new_ids)

            if total_after_add > 64:
                # Eliminar las más antiguas para dejar espacio
                trim_playlist(playlist_id, max_size=64 - len(new_ids))

            sp.playlist_add_items(playlist_id, new_ids, position=0)
            logger.info(f"[SPOTIFY] Added from message: {new_ids}")
            await increment_score(user_id, len(new_ids))
            await message.channel.send(f"Added {len(new_ids)} track(s).", delete_after=5)
        except Exception as e:
            logger.error(f"[SPOTIFY ERROR] Adding from message: {e}")
            await message.channel.send("Failed to add track(s).", delete_after=5)

    # Procesar otros eventos de on_message
    await bot.process_commands(message)

# Slash command: /playlist
@tree.command(name="playlist", description="Post the link to the Spotify playlist.")
async def playlist_command(interaction: discord.Interaction):
    await interaction.response.send_message(f"🎧 {playlist_url}")

# Slash command: /sync
@tree.command(name="sync", description="Read past messages and adds the Spotify songs")
async def sync_command(interaction: discord.Interaction):
    if interaction.channel.id != CHANNEL_ID:
        return await interaction.response.send_message("This command can only be used in #music-recs.", ephemeral=True)

    await interaction.response.send_message("Reading previous messages...")
    existing_ids = get_existing_track_ids(playlist_id)
    total_new_ids = []

    async for message in interaction.channel.history(limit=250):
        user_id = str(message.author.id)
        new_ids = extract_track_ids_from_text(message.content, existing_ids)
        if new_ids:
            await increment_score(user_id, len(new_ids))
        total_new_ids.extend(new_ids)
        existing_ids.update(new_ids)

    if total_new_ids:
        try:
            results = sp.playlist_items(playlist_id, fields="total", limit=1)
            current_total = results['total']
            total_after_add = current_total + len(total_new_ids)

            if total_after_add > 64:
                trim_playlist(playlist_id, max_size=64 - len(total_new_ids))

            for track_id in reversed(total_new_ids):
                sp.playlist_add_items(playlist_id, [track_id], position=0)

            logger.info(f"[SPOTIFY] Added from history: {total_new_ids}")
            await interaction.followup.send(f"Added {len(total_new_ids)} new track(s) from message history.")
        except Exception as e:
            logger.error(f"[SPOTIFY ERROR] Adding from history: {e}")
            await interaction.followup.send("Failed to add tracks from history.")
    else:
        await interaction.followup.send("No new tracks found in previous messages.")

# Slash command: /scoreboard
@tree.command(name="scoreboard", description="Show the user contribution scoreboard.")
async def scoreboard_command(interaction: discord.Interaction):
    rows = await get_scoreboard()
    if not rows:
        return await interaction.response.send_message("No scores yet.")

    lines = []
    for rank, row in enumerate(rows, 1):
        user = await bot.fetch_user(int(row["user_id"]))
        lines.append(f"#{rank} - {user.mention}: {row['count']} song(s)")

    await interaction.response.send_message("🐈 Top Contributors:\n" + "\n".join(lines))

bot.run(DISCORD_TOKEN)
import asyncio
import os
import re
import discord
import spotipy
import logging
import yt_dlp
from yt_dlp import YoutubeDL
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from discord.ext import commands
from discord import app_commands

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load .env
load_dotenv()

# Recrea cookies.txt desde env var para yt_dlp
if os.getenv("YOUTUBE_COOKIES"):
    with open("cookies.txt", "w", encoding="utf-8") as f:
        f.write(os.getenv("YOUTUBE_COOKIES"))

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

# Helpers
def get_existing_track_ids(playlist_id):
    track_ids = []
    results = sp.playlist_items(playlist_id)
    while results:
        for item in results['items']:
            if item.get('track'):
                track_ids.append(item['track']['id'])
        if results['next']:
            results = sp.next(results)
        else:
            break
    return track_ids

SPOTIFY_LINK_REGEX = r'https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(?P<type>track|album|playlist)/(?P<id>[a-zA-Z0-9]+)'
YOUTUBE_LINK_REGEX = r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w\-]+)'

def extract_track_ids_from_text(text, existing_ids):
    matches = re.findall(SPOTIFY_LINK_REGEX, text)
    new_ids = []

    # Manejo de enlaces Spotify
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
                    results = sp.next(results) if results['next'] else None
        except Exception as e:
            logger.warning(f"[SPOTIFY ERROR] {link_type}:{spotify_id} - {e}")

    # Manejo de enlaces YouTube
    youtube_links = re.findall(YOUTUBE_LINK_REGEX, text)
    for yt_url in youtube_links:
        title = get_youtube_title(yt_url)
        if title:
            try:
                results = sp.search(q=title, type='track', limit=1)
                items = results.get('tracks', {}).get('items', [])
                if items:
                    track_id = items[0]['id']
                    if track_id and track_id not in existing_ids:
                        new_ids.append(track_id)
                        logger.info(f"[YOUTUBE->SPOTIFY] Matched '{title}' to {track_id}")
            except Exception as e:
                logger.warning(f"[SPOTIFY SEARCH ERROR] {title} - {e}")

    return new_ids


def get_youtube_title(url: str):
    try:
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'extract_flat': 'in_playlist',
        }
        if os.path.exists("cookies.txt"):
            ydl_opts['cookies'] = 'cookies.txt'

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get('title')
    except Exception as e:
        logger.warning(f"[YOUTUBE ERROR] {url} - {e}")
        return None

def search_spotify_track_by_title(title, existing_ids):
    try:
        results = sp.search(q=title, type='track', limit=1)
        items = results['tracks']['items']
        if items:
            track = items[0]
            if track['id'] not in existing_ids:
                return track['id']
    except Exception as e:
        logger.warning(f"[SPOTIFY SEARCH ERROR] {title} - {e}")
    return None




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
    new_ids = extract_track_ids_from_text(message.content, set(existing_ids))

    # Spotify links
    if new_ids:
        try:
            for tid in reversed(new_ids):
                sp.playlist_add_items(playlist_id, [tid], position=0)
            logger.info(f"[SPOTIFY] Added from message: {new_ids}")
        except Exception as e:
            logger.error(f"[SPOTIFY ERROR] Adding from message: {e}")
            await message.channel.send("Failed to add track(s).", delete_after=5)

    # YouTube links
    yt_matches = re.findall(YOUTUBE_LINK_REGEX, message.content)
    yt_links = [f"https://www.youtube.com/watch?v={vid}" for vid in yt_matches]

    for yt_url in yt_links:
        title = get_youtube_title(yt_url)
        if title:
            track_id = search_spotify_track_by_title(title, set(existing_ids))
            if track_id:
                try:
                    sp.playlist_add_items(playlist_id, [track_id], position=0)
                    logger.info(f"[YOUTUBE->SPOTIFY] Added '{title}' as {track_id}")
                    await message.channel.send(f"Added track from YouTube: {title}", delete_after=5)
                except Exception as e:
                    logger.error(f"[YOUTUBE ADD ERROR] {title} - {e}")

    # Enforce playlist limit
    all_ids = get_existing_track_ids(playlist_id)
    if len(all_ids) > 64:
        to_remove = all_ids[64:]
        if to_remove:
            sp.playlist_remove_all_occurrences_of_items(playlist_id, to_remove)
            logger.info(f"[SPOTIFY] Removed old tracks: {to_remove}")

# Slash command: /playlist
@tree.command(name="playlist", description="Post the link to the Spotify playlist.")
async def playlist_command(interaction: discord.Interaction):
    await interaction.response.send_message(f"{playlist_url}")

# Slash command: /sync
@tree.command(name="sync", description="Read past messages and adds the Spotify songs")
async def sync_command(interaction: discord.Interaction):
    if interaction.channel.id != CHANNEL_ID:
        return await interaction.response.send_message("This command can only be used in #music-recs.", ephemeral=True)

    await interaction.response.send_message("Reading previous messages...")
    existing_ids = set(get_existing_track_ids(playlist_id))
    total_new_ids = []

    async for message in interaction.channel.history(limit=250):
        new_ids = extract_track_ids_from_text(message.content, existing_ids)
        total_new_ids.extend(new_ids)
        existing_ids.update(new_ids)

        yt_matches = re.findall(YOUTUBE_LINK_REGEX, message.content)
        yt_links = [f"https://www.youtube.com/watch?v={vid}" for vid in yt_matches]

        for yt_url in yt_links:
            title = get_youtube_title(yt_url)
            if title:
                track_id = search_spotify_track_by_title(title, existing_ids)
                if track_id:
                    total_new_ids.append(track_id)
                    existing_ids.add(track_id)

    if total_new_ids:
        try:
            for tid in reversed(total_new_ids):
                sp.playlist_add_items(playlist_id, [tid], position=0)
            logger.info(f"[SPOTIFY] Added from history: {total_new_ids}")
        except Exception as e:
            logger.error(f"[SPOTIFY ERROR] Adding from history: {e}")
            await interaction.followup.send("Failed to add tracks from history.")

        # Enforce limit
        all_ids = get_existing_track_ids(playlist_id)
        if len(all_ids) > 64:
            to_remove = all_ids[64:]
            if to_remove:
                sp.playlist_remove_all_occurrences_of_items(playlist_id, to_remove)
                logger.info(f"[SPOTIFY] Removed old tracks: {to_remove}")

        await interaction.followup.send(f"Added {len(total_new_ids)} new track(s) from message history.")
    else:
        await interaction.followup.send("No new tracks found in previous messages.")

bot.run(DISCORD_TOKEN)

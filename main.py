import asyncio
from twitchio.ext import commands
import re
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import os
from dotenv import load_dotenv
import aiohttp

load_dotenv()

class Bot(commands.Bot):
    def __init__(self):
        try:
            with open('.bot', 'r') as f:
                lines = f.read().strip().split('\n')
                bot_name = lines[0].strip()
                bot_token = lines[1].strip()
            with open('.broadcaster', 'r') as f:
                lines = f.read().strip().split('\n')
                client_name = lines[0].strip()
                client_id = lines[3].strip()
        except FileNotFoundError:
            print('❌ bot file not found!')
            exit(1)
        except IndexError:
            print('❌ bot file is empty or malformed!')
            exit(1)
        
        super().__init__(
            token=f'oauth:{bot_token}',
            client_id=f'{client_id}',
            nick=f'{bot_name}',
            prefix='!',
            initial_channels=[client_name],
        )
        self.current_problem = None
        
        # Initialize Spotify
        self.spotify = None
        self.init_spotify()
    
    def init_spotify(self):
        try:
            scope = "user-read-currently-playing user-read-playback-state"
            self.spotify = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=os.getenv('SPOTIFY_CLIENT_ID'),
                client_secret=os.getenv('SPOTIFY_CLIENT_SECRET'),
                redirect_uri=os.getenv('SPOTIFY_REDIRECT_URI'),
                scope=scope,
                cache_path=".spotify_cache"
            ))
            print('✅ Spotify API initialized')
        except Exception as e:
            print(f'❌ Failed to initialize Spotify: {e}')
            self.spotify = None

    async def event_ready(self):
        print(f'✅ Bot ready | {self.nick}')

    async def event_message(self, message):
        if message.echo:
            return
        print(f'💬 {message.author.name}: {message.content}')
        await self.handle_commands(message)

    @commands.command(name='daily')
    async def daily_leetcode(self, ctx):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://leetcode-api-pied.vercel.app/daily') as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # Extract information from the response
                        title = data['question']['title']
                        difficulty = data['question']['difficulty']
                        link_suffix = data['link']
                        full_link = f"https://leetcode.com{link_suffix}"
                        
                        # Send the daily challenge info
                        await ctx.send(f"📅 Today's LeetCode Daily: {title} ({difficulty}) | {full_link}")
                    else:
                        await ctx.send("❌ Failed to fetch today's LeetCode daily challenge")
        except Exception as e:
            print(f"Error fetching daily LeetCode: {e}")
            await ctx.send("❌ Error retrieving today's LeetCode daily challenge")

    @commands.command(name='song')
    async def current_song(self, ctx):
        if not self.spotify:
            await ctx.send("❌ Spotify integration not available!")
            return
        
        try:
            current_track = self.spotify.current_playback()
            
            if not current_track or not current_track['is_playing']:
                await ctx.send("🎵 No song currently playing on Spotify")
                return
            
            track = current_track['item']
            if track:
                song_name = track['name']
                artists = ', '.join([artist['name'] for artist in track['artists']])
                album = track['album']['name']
                
                # Get the Spotify URL
                spotify_url = track['external_urls']['spotify']
                
                await ctx.send(f"🎵 Now playing: {song_name} by {artists} | {spotify_url}")
            else:
                await ctx.send("🎵 No track information available")
                
        except Exception as e:
            print(f"Error getting current song: {e}")
            await ctx.send("❌ Failed to get current song from Spotify")

    @commands.command(name='lt')
    async def leetcode_timer(self, ctx, url: str = None, minutes: int = 30):
        if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
            return
        if not url:
            await ctx.send("❌ Please provide a LeetCode problem URL! Usage: !lt <leetcode-url> [minutes]")
            return
        
        # Validate minutes (optional bounds check)
        if minutes <= 0:
            await ctx.send("❌ Timer duration must be greater than 0 minutes!")
            return
        if minutes > 180:  # Optional: cap at 3 hours
            await ctx.send("❌ Timer duration cannot exceed 180 minutes!")
            return
        
        self.current_problem = url
        
        def extract_problem_name(url):
            pattern = r'leetcode\.com/problems/([^/]+)'
            match = re.search(pattern, url)
            if match:
                problem_name = match.group(1).replace('-', ' ').title()
                return problem_name
            return "LeetCode Problem"
        
        problem_name = extract_problem_name(url)
        print(f"🔒 Timer started by: {ctx.author.name} (Role: {'Broadcaster' if ctx.author.is_broadcaster else 'Mod' if ctx.author.is_mod else 'VIP'})")
        
        await ctx.send(f"⏰ Starting {minutes}-minute timer for '{problem_name}'")
        
        # Calculate halfway point
        halfway_seconds = (minutes * 60) // 2
        remaining_seconds = (minutes * 60) - halfway_seconds
        
        await asyncio.sleep(halfway_seconds)
        await ctx.send(f"⏰ Halfway through '{problem_name}' ({minutes//2 if minutes % 2 == 0 else f'{minutes/2:.1f}'} minutes remaining)")
        
        await asyncio.sleep(remaining_seconds)
        await ctx.send(f"⏰ Timer's up for '{problem_name}'!")

    @commands.command(name='problem')
    async def get_problem(self, ctx, problem_id: str = None):
        # If no problem_id is provided, return the current problem being worked on
        if problem_id is None:
            if self.current_problem is not None:
                await ctx.send(self.current_problem)
            else:
                await ctx.send("Howlingfantods_ currently isn't working on a problem!")
            return
        
        # If problem_id is provided, fetch the specific problem from the API
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f'https://leetcode-api-pied.vercel.app/problem/{problem_id}') as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # Extract information from the response
                        title = data['title']
                        difficulty = data['difficulty']
                        url = data['url']
                        
                        # Send the problem info
                        await ctx.send(f"🧩 Problem #{problem_id}: {title} ({difficulty}) | {url}")
                    elif response.status == 404:
                        await ctx.send(f"❌ Problem #{problem_id} not found!")
                    else:
                        await ctx.send(f"❌ Failed to fetch problem #{problem_id}")
        except ValueError:
            # Handle case where problem_id is not a valid number
            await ctx.send("❌ Please provide a valid problem number!")
        except Exception as e:
            print(f"Error fetching LeetCode problem {problem_id}: {e}")
            await ctx.send(f"❌ Error retrieving problem #{problem_id}")

    @commands.Cog.event()
    async def event_channel_commercial(self, channel, length):
        """Automatically triggered when Twitch starts an ad break"""
        await channel.send(f"📺 Ad break starting! Back in {length} seconds - don't go anywhere! ☕")
        
        # Schedule a "back from ads" message
        asyncio.create_task(self.ad_break_over(channel, length))

    async def ad_break_over(self, channel, duration):
        """Send a message when the ad break should be over"""
        await asyncio.sleep(duration)
        await channel.send("🎉 We're back! Thanks for waiting through the ads! 👋")


    @commands.command(name='spotify')
    async def get_spotify(self, ctx):
        await ctx.send('https://open.spotify.com/user/31s6zl5xs5kqjw7qbrqgslamrcfa')

    @commands.command(name='goodreads')
    async def get_goodreads(self, ctx):
        await ctx.send('https://www.goodreads.com/howlingfantods_')

    @commands.command(name='discord')
    async def get_discord(self, ctx):
        await ctx.send('https://discord.gg/tHjeDK8Cd7')

if __name__ == "__main__":
    bot = Bot()
    bot.run()

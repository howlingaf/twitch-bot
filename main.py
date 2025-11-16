import asyncio
import json
import os
import re

import aiohttp
import spotipy
import websockets
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from twitchio.ext import commands

load_dotenv()

# ---------------------------------------------------------
# ---------------- OVERLAY WEBSOCKET SERVER ----------------
# ---------------------------------------------------------

overlay_clients = set()


async def overlay_handler(websocket):
    overlay_clients.add(websocket)
    print("📺 Overlay connected")
    try:
        async for _ in websocket:
            pass
    except Exception as e:
        print(f"[ERROR] overlay ws: {e}")
    finally:
        overlay_clients.discard(websocket)
        print("📺 Overlay disconnected")


async def overlay_broadcast(data: dict):
    if not overlay_clients:
        print("[INFO] No overlay clients connected for broadcast.")
        return

    message = json.dumps(data)
    dead = []

    for ws in overlay_clients:
        try:
            await ws.send(message)
        except Exception as e:
            print(f"[ERROR] sending to overlay client: {e}")
            dead.append(ws)

    for ws in dead:
        overlay_clients.discard(ws)


# ---------------------------------------------------------
# ------------------------------ BOT -----------------------
# ---------------------------------------------------------

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            token=os.getenv('BOT_OAUTH_TOKEN'),
            client_id=os.getenv('CLIENT_ID'),
            nick="hairyrug_",
            prefix='!',
            initial_channels=["howlingfantods_"],
        )

        self.current_problem = None
        self.spotify = None
        self.is_live = False
        self.ad_task = None
        self.lt_task = None
        self.ltlock_task = None

        self.init_spotify()

    # ---------------- SPOTIFY INIT ----------------
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
            print(f"[ERROR] Spotify init: {e}")
            self.spotify = None

    # -----------------------------------------------------
    # ---------------- STREAM METADATA LOGGER -------------
    # -----------------------------------------------------
    async def log_stream_metadata(self):
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                "Client-Id": os.getenv("CLIENT_ID")
            }
            url = f"https://api.twitch.tv/helix/streams?user_id={os.getenv('BROADCASTER_ID')}"

            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                print("\n====== STREAM METADATA ======")
                print(json.dumps(data, indent=2))
                print("=============================\n")

    # -----------------------------------------------------
    # ---------------- STREAM LIVE CHECK ------------------
    # -----------------------------------------------------
    async def is_stream_live(self):
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                "Client-Id": os.getenv("CLIENT_ID")
            }
            url = f"https://api.twitch.tv/helix/streams?user_id={os.getenv('BROADCASTER_ID')}"

            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    print(f"[ERROR] stream status check failed: {resp.status}")
                    return False

                data = await resp.json()
                return len(data.get("data", [])) > 0

    # -----------------------------------------------------
    # ---------------- GET CURRENT CATEGORY ---------------
    # -----------------------------------------------------
    async def get_current_category(self):
        """
        Twitch doesn't populate category immediately when stream first goes LIVE.
        Retry 5 times over ~10 seconds.
        """
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                "Client-Id": os.getenv("CLIENT_ID")
            }
            url = f"https://api.twitch.tv/helix/streams?user_id={os.getenv('BROADCASTER_ID')}"

            for attempt in range(5):
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        print(f"[ERROR] category fetch failed: {resp.status}")
                        return None

                    data = await resp.json()

                    if data.get("data"):
                        game_name = data["data"][0].get("game_name")
                        print(f"[Category attempt {attempt}] game_name = {repr(game_name)}")

                        if game_name:  # populated!
                            return game_name

                await asyncio.sleep(2)

        print("⚠️ Category never populated, returning None")
        return None

    # -----------------------------------------------------
    # ---------------- DELETE LATEST VOD -------------------
    # -----------------------------------------------------
    async def delete_latest_vod(self):
        category = await self.get_current_category()
        print(f"🔍 VOD deletion check — category={repr(category)}")

        if category != "Fitness & Health":
            print("Skipping VOD deletion — not Fitness & Health.")
            return

        print("💪 Category is Fitness & Health — deleting VOD…")

        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                "Client-Id": os.getenv("CLIENT_ID")
            }

            # Get latest archive VOD
            url = (
                f"https://api.twitch.tv/helix/videos?"
                f"user_id={os.getenv('BROADCASTER_ID')}&first=1&type=archive"
            )

            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                if not data["data"]:
                    print("No VOD found to delete.")
                    return

                vod_id = data["data"][0]["id"]

            delete_url = f"https://api.twitch.tv/helix/videos?id={vod_id}"
            async with session.delete(delete_url, headers=headers) as delete_resp:
                print(f"🗑 Deleted VOD {vod_id} (status={delete_resp.status})")

    # -----------------------------------------------------
    # ---------------- LIVE STATUS MONITOR ----------------
    # -----------------------------------------------------
    async def monitor_live_status(self):
        while True:
            live = await self.is_stream_live()

            if live and not self.is_live:
                print("🎉 Stream just went LIVE!")
                self.is_live = True

                # Log all metadata
                await self.log_stream_metadata()

                # OPTIONAL: Run ads instantly when going live
                # asyncio.create_task(self.start_ad_immediately())

                self.ad_task = asyncio.create_task(self._run_ad_loop())

            elif not live and self.is_live:
                print("🔻 Stream went OFFLINE")
                self.is_live = False

                await self.delete_latest_vod()

                if self.ad_task:
                    self.ad_task.cancel()
                    self.ad_task = None

            await asyncio.sleep(20)

    # -----------------------------------------------------
    # ---------------- BOT READY EVENT --------------------
    # -----------------------------------------------------
    async def event_ready(self):
        print(f'✅ Bot ready | {self.nick}')
        asyncio.create_task(self.monitor_live_status())

    # -----------------------------------------------------
    # ---------------- CHAT MESSAGE HANDLER ---------------
    # -----------------------------------------------------
    async def event_message(self, message):
        if message.echo:
            return
        await self.handle_commands(message)

    # -----------------------------------------------------
    # ---------------- AD LOOP ----------------------------
    # -----------------------------------------------------
    async def _run_ad_loop(self):
        print("▶️ Ad loop started.")
        channel = self.connected_channels[0]

        while self.is_live:
            try:
                await asyncio.sleep(59 * 60)

                if not self.is_live:
                    break

                await channel.send("📢 Ad in 1 minute!")
                await asyncio.sleep(60)

                if not self.is_live:
                    break

                async with aiohttp.ClientSession() as session:
                    headers = {
                        "Authorization": f"Bearer {os.getenv('ACCESS_TOKEN')}",
                        "Client-Id": os.getenv("CLIENT_ID"),
                    }
                    payload = {
                        "broadcaster_id": os.getenv("BROADCASTER_ID"),
                        "length": 180
                    }

                    async with session.post(
                        "https://api.twitch.tv/helix/channels/commercial",
                        headers=headers,
                        json=payload
                    ) as resp:
                        if resp.status != 200:
                            print(f"❌ Failed to start ad: {await resp.text()}")
                            break

                        await channel.send("📺 Ad starting (3 minutes).")

                await asyncio.sleep(180)

                if not self.is_live:
                    break

                await channel.send("✅ Ad break over!")

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"❌ Fatal error in ad loop: {e}")
                break

        print("⛔ Ad loop stopped (stream offline).")

    # -----------------------------------------------------
    # ---------------- UTILITY HELPERS --------------------
    # -----------------------------------------------------
    def _extract_problem_name(self, url: str) -> str:
        m = re.search(r'leetcode\.com/problems/([^/]+)', url)
        return m.group(1).replace('-', ' ').title() if m else "LeetCode Problem"

    # -----------------------------------------------------
    # ---------------- LT TIMER COMMANDS ------------------
    # -----------------------------------------------------
    @commands.command(name='lt')
    async def leetcode_timer(self, ctx, url: str = None, minutes: int = 30):
        try:
            if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
                return

            if url and url.lower() == "clear":
                if self.lt_task and not self.lt_task.done():
                    self.lt_task.cancel()
                self.current_problem = None
                return

            if not url or minutes <= 0 or minutes > 180:
                return

            self.current_problem = url
            problem_name = self._extract_problem_name(url)
            await ctx.send(f"⏰ {minutes}-minute timer started for '{problem_name}'")

            self.lt_task = asyncio.create_task(self._run_lt_timer(ctx, problem_name, minutes))

        except Exception as e:
            print(f"[ERROR] lt command: {e}")

    async def _run_lt_timer(self, ctx, problem_name, minutes):
        try:
            halfway = (minutes * 60) // 2
            final = minutes * 60 - halfway

            await asyncio.sleep(halfway)
            await ctx.send(f"⏰ Halfway done with '{problem_name}'")

            await asyncio.sleep(final)
            await ctx.send(f"⏰ Time's up for '{problem_name}'")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[ERROR] LT timer loop: {e}")

    # -----------------------------------------------------
    # ---------------- LOCK-IN + OVERLAY TIMER ------------
    # -----------------------------------------------------
    @commands.command(name='ltlockin')
    async def leetcode_lockin(self, ctx, url: str = None, minutes: int = 30):
        try:
            if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
                return

            if url and url.lower() == "clear":
                if self.ltlock_task and not self.ltlock_task.done():
                    self.ltlock_task.cancel()
                await overlay_broadcast({"command": "stop"})
                print("🛑 LOCK-IN cancelled.")
                return

            if not url or minutes <= 0 or minutes > 180:
                return

            self.current_problem = url
            problem_name = self._extract_problem_name(url)

            await ctx.send(f"🔒 LOCKED IN — {minutes} minutes for '{problem_name}'")

            await overlay_broadcast({
                "command": "start",
                "duration": minutes * 60,
                "label": "LOCKED IN"
            })

            self.ltlock_task = asyncio.create_task(
                self._run_ltlock_timer(ctx, problem_name, minutes)
            )

        except Exception as e:
            print(f"[ERROR] ltlockin command: {e}")

    async def _run_ltlock_timer(self, ctx, problem_name, minutes):
        try:
            await asyncio.sleep(minutes * 60)
            await overlay_broadcast({"command": "stop"})
            await ctx.send(f"⏰ Time's up for '{problem_name}' — LOCK-IN over!")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[ERROR] LTLOCK timer loop: {e}")

    # -----------------------------------------------------
    # ---------------- OTHER COMMANDS ---------------------
    # -----------------------------------------------------
    @commands.command(name='daily')
    async def daily_leetcode(self, ctx):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://leetcode-api-pied.vercel.app/daily') as resp:
                    if resp.status != 200:
                        return

                    data = await resp.json()
                    title = data['question']['title']
                    diff = data['question']['difficulty']
                    link = f"https://leetcode.com{data['link']}"

                    await ctx.send(f"📅 Daily: {title} ({diff}) | {link}")

        except Exception as e:
            print(f"[ERROR] daily command: {e}")

    @commands.command(name='song')
    async def current_song(self, ctx):
        try:
            if not self.spotify:
                return

            data = self.spotify.current_playback()
            if not data or not data.get("is_playing"):
                return

            item = data["item"]
            song = item["name"]
            artists = ", ".join(a["name"] for a in item["artists"])
            url = item["external_urls"]["spotify"]

            await ctx.send(f"🎵 {song} — {artists} | {url}")

        except Exception as e:
            print(f"[ERROR] song command: {e}")

    @commands.command(name='problem')
    async def get_problem(self, ctx, problem_id: str = None):
        try:
            if problem_id is None:
                return

            async with aiohttp.ClientSession() as session:
                async with session.get(f'https://leetcode-api-pied.vercel.app/problem/{problem_id}') as resp:
                    if resp.status != 200:
                        return

                    data = await resp.json()
                    await ctx.send(
                        f"🧩 #{problem_id}: {data['title']} ({data['difficulty']}) | {data['url']}"
                    )

        except Exception as e:
            print(f"[ERROR] problem command: {e}")

    @commands.command(name='spotify')
    async def get_spotify(self, ctx):
        await ctx.send('https://open.spotify.com/user/31s6zl5xs5kqjw7qbrqgslamrcfa')

    @commands.command(name='goodreads')
    async def get_goodreads(self, ctx):
        await ctx.send('https://www.goodreads.com/howlingfantods_')

    @commands.command(name='discord')
    async def get_discord(self, ctx):
        await ctx.send('https://discord.gg/tHjeDK8Cd7')


# ---------------------------------------------------------
# ------------------------------ MAIN ----------------------
# ---------------------------------------------------------

async def main():
    bot = Bot()

    overlay_host = "0.0.0.0"
    overlay_port = int(os.getenv("OVERLAY_PORT", "8765"))

    server = await websockets.serve(overlay_handler, overlay_host, overlay_port)
    print(f"🔌 Overlay WebSocket server listening on ws://{overlay_host}:{overlay_port}")

    try:
        await bot.start()
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())

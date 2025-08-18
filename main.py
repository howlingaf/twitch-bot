import asyncio
from twitchio.ext import commands
import re

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

    async def event_ready(self):
        print(f'✅ Bot ready | {self.nick}')

    async def event_message(self, message):
        if message.echo:
            return
        print(f'💬 {message.author.name}: {message.content}')
        await self.handle_commands(message)


    @commands.command(name='lt')
    async def leetcode_timer(self, ctx, url: str = None):

        if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
            return
        if not url:
            await ctx.send("❌ Please provide a LeetCode problem URL! Usage: !lt <leetcode-url>")
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
        await ctx.send(f"⏰ Starting 45-minute timer for '{problem_name}'")
        await asyncio.sleep(1350)
        await ctx.send(f"⏰ Halfway through '{problem_name}'")
        await asyncio.sleep(1350)
        await ctx.send(f"⏰ Timer's up for '{problem_name}'!")
        self.current_problem = None

    @commands.command(name='problem')
    async def get_problem(self, ctx):
        if self.current_problem is not None:
            await ctx.send(self.current_problem)
        else:
            await ctx.send("There isn't a problem currently being worked on!")

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

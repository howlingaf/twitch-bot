import asyncio
from twitchio.ext import commands
import re

class Bot(commands.Bot):
    def __init__(self):
        # Read bot token from dotfile
        try:
            with open('.hairyrug_', 'r') as f:
                lines = f.read().strip().split('\n')
                bot_token = lines[0].strip()  # First line should be access token
        except FileNotFoundError:
            print("❌ .hairyrug_ file not found!")
            exit(1)
        except IndexError:
            print("❌ .hairyrug_ file is empty or malformed!")
            exit(1)
        
        super().__init__(
            token=f'oauth:{bot_token}',
            client_id='38sbiq35p4d7p3ybpn0n545orwa6hl',
            nick='hairyrug_',
            prefix='!',
            initial_channels=['howlingfantods_'],
        )
        self.current_problem = None

    async def event_ready(self):
        print(f'✅ Bot ready | {self.nick}')

    async def event_message(self, message):
        if message.echo:
            return
        print(f'💬 {message.author.name}: {message.content}')
        await self.handle_commands(message)

    def extract_problem_name(self, url):
        pattern = r'leetcode\.com/problems/([^/]+)'
        match = re.search(pattern, url)
        if match:
            problem_name = match.group(1).replace('-', ' ').title()
            return problem_name
        return "LeetCode Problem"

    @commands.command(name='lt')
    async def leetcode_timer(self, ctx, url: str = None):
        # Check if user has required privileges
        if not (ctx.author.is_mod or ctx.author.is_broadcaster or ctx.author.is_vip):
            return
        if not url:
            await ctx.send("❌ Please provide a LeetCode problem URL! Usage: !lt <leetcode-url>")
            return
        self.current_problem = url
        problem_name = self.extract_problem_name(url)
        print(f"🔒 Timer started by: {ctx.author.name} (Role: {'Broadcaster' if ctx.author.is_broadcaster else 'Mod' if ctx.author.is_mod else 'VIP'})")
        await ctx.send(f"⏰ Starting 45-minute timer for '{problem_name}'")
        await asyncio.sleep(1350)
        await ctx.send(f"⏰ Halfway through '{problem_name}'")
        await asyncio.sleep(1350)
        await ctx.send(f"⏰ Timer's up for '{problem_name}'!")
        self.current_problem = None

if __name__ == "__main__":
    bot = Bot()
    bot.run()

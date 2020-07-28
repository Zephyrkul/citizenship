import json
from pathlib import Path

import sans
from redbot.core.errors import CogLoadError

from .citizenship import Citizenship

with open(Path(__file__).parent / "info.json") as fp:
    __red_end_user_data_statement__ = json.load(fp)["end_user_data_statement"]


async def setup(bot):
    expected = "0.0.1b7"
    if sans.version_info != type(sans.version_info)(expected):
        raise CogLoadError(f"This cog requires sans version {expected}.")
    if bot.user.id not in (488781401567526915, 256505473807679488):
        raise CogLoadError("I don't know how you found this cog, but it isn't meant for your bot.")
    cog = Citizenship(bot)
    await cog.initialize()
    bot.add_cog(cog)

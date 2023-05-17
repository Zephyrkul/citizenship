import json
from pathlib import Path
from packaging import version

import sans
from redbot.core.errors import CogLoadError

from .citizenship import Citizenship

with open(Path(__file__).parent / "info.json") as fp:
    __red_end_user_data_statement__ = json.load(fp)["end_user_data_statement"]


async def setup(bot):
    expected = "1.1.2"
    if version.parse(sans.__version__) < version.parse(expected):
        raise CogLoadError(f"This cog requires sans version {expected} or later.")
    cog = Citizenship(bot)
    await cog.initialize()
    await bot.add_cog(cog)

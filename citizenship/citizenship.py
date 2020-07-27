import asyncio
import contextlib
import json
import logging
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from itertools import repeat, starmap
from typing import Dict, MutableMapping, Set, Tuple, Union

import aiohttp
import discord
import sans
from backoff import expo, on_exception
from bidict import bidict
from multidict import MultiDict
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from redbot.core.commands import Context
from redbot.core.utils.chat_formatting import box, pagify, humanize_list, humanize_timedelta
from sans.api import Api

PERIOD = 43200
NVALID = r"\-\w"
LAST_MAGICALITY_SNOWFLAKE = 735393592775802900
LOG = logging.getLogger("red.fluffy.citizenship")


def nid(arg):
    ret = re.sub("[^{}]+".format(NVALID), "", arg.lower().replace(" ", "_"))
    if ret:
        return ret
    raise TypeError("Empty nation string")


def rnid(arg):
    return arg.replace("_", " ").title()


def tnid(*args):
    if not args:
        return (None, None)
    if len(args) == 1:
        return (None, args[0])
    return (nid(args[-1]), args[0])


class SheetsError(Exception):
    pass


class Default(dict):
    def __missing__(self, key):
        return key


class Cached(MutableMapping):
    """ this is a terrbile idea, don't ever do it """

    def __init__(self, config: Config, *, inv_from=None):
        self.data = None if inv_from is None else inv_from.data.inv
        self.config = config
        self._inv = inv_from is not None and not inv_from._inv
        self._inv_data = inv_from.data if inv_from else None
        self._cm = None

    async def initialize(self):
        if self.data is not None:
            raise RuntimeError
        self.data = bidict({k: v["nation"] for k, v in (await self.config.all_users()).items()})

    def __getitem__(self, item):
        return self.data[item]

    async def __aenter__(self):
        if self._inv:
            raise RuntimeError
        self._cm = set()
        return self

    async def __aexit__(self, *args):
        cm, self._cm, data = self._cm, None, self.data.copy()
        for key in cm:
            if key not in data:
                await self.config.user_from_id(key).nation.clear()
            else:
                await self.config.user_from_id(key).nation.set(self.data[key])

    def __setitem__(self, item, value):
        if self._cm is None:
            if self._inv:
                asyncio.ensure_future(self.config.user_from_id(value).nation.set(item))
            else:
                asyncio.ensure_future(self.config.user_from_id(item).nation.set(value))
        else:
            self._cm.add(value if self._inv else item)
        self.data[item] = value

    def __delitem__(self, item):
        value = self.data.pop(item)
        if self._cm is None:
            if self._inv:
                asyncio.ensure_future(self.config.user_from_id(value).nation.clear())
            else:
                asyncio.ensure_future(self.config.user_from_id(item).nation.clear())
        else:
            self._cm_del.add(value if self._inv else item)

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)

    def __missing__(self, item):
        return None

    @property
    def inv(self):
        if self._inv_data is None:
            self._inv_data = Cached(self.config, inv_from=self)
        return self._inv_data


class Citizenship(commands.Cog):
    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=2113674295, force_registration=True)
        self.config.register_guild(on=False, roles={})
        self.config.register_user(nation=None)
        self.nations = Cached(self.config)
        self.cache: Dict[str, Set[str]] = {}
        self.cooldowns: Dict[discord.User, datetime] = {}
        self.enabled_guilds: Set[discord.Guild] = set()
        self.recheck = re.compile(
            rf".*\b(?:https?:\/\/)?(?:www\.)?nationstates\.net\/(?:(\w+)=)?([{NVALID}]+)\b.*",
            re.I | re.S,
        )
        self.usernid = re.compile(
            rf'"?(?:(?:https?:\/\/)?(?:www\.)?nationstates\.net\/(?:(\w+)=)?)?([{NVALID}\s]+)"?',
            re.I,
        )
        self.task = bot.loop.create_task(self._task())
        self.task.add_done_callback(self._callback)
        self.waiting_for = None

    def _callback(self, fut: asyncio.Future):
        try:
            exc = fut.exception()
        except asyncio.CancelledError:
            LOG.debug("Future was cancelled.")
            return
        except asyncio.InvalidStateError:
            LOG.debug("The future said it was done but actually wasn't?")
            return
        if not exc:
            LOG.debug("Future exited with no exception.")
            return
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        asyncio.ensure_future(
            self.bot.send_to_owners(
                box(
                    f"Exception {exc.__class__.__name__} occurred in {self.__class__.__name__} task.\n\n{tb}",
                    lang="py",
                )
            )
        )
        LOG.debug("Sending traceback to owners.")

    def _waiting(self):
        return self.waiting_for and not self.waiting_for.done()

    async def initialize(self):
        await self.nations.initialize()
        all_guilds = await self.config.all_guilds()
        self.enabled_guilds = {self.bot.get_guild(g) for g, d in all_guilds.items() if d["on"]}
        self.enabled_guilds.discard(None)

    async def maybe_fetch(self, user_id: int):
        return self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)

    @commands.group(invoke_without_command=True, autohelp=False)
    async def identify(self, ctx: Context, *, nation=None):
        """Configure or view the nations associated with yourself or others."""
        if ctx.invoked_subcommand is None and nation is None:
            return await ctx.send_help()
        await ctx.invoke(self._identify_nation, nation=nation)

    @identify.command(name="import")
    @checks.is_owner()
    async def _identify_import(self, ctx: Context, *, path: str):
        """
        Import data from V2 and help catch up from V2's downtime.

        <path> should be a direct path to citizenships data.json.
        This is most likely <V2 installation path>/data/citizenship/data.json.
        """
        try:
            with open(path) as file:
                servers_data = json.load(file)
        except Exception as e:
            return await ctx.author.send(
                f"I couldn't load your data due to an error:\n`{e.__class__.__name__}:{' '.join(e.args)}``"
            )
        nations_data = servers_data.pop("nations")
        settings_data = servers_data.pop("settings")
        await ctx.bot.set_shared_api_tokens("google_sheets", api_key=settings_data["KEY"])
        for server_id, settings in servers_data.items():
            await self.config.guild_from_id(int(server_id)).on.set(settings["on"])
        async with self.nations:
            for nation, user_id in nations_data.items():
                self.nations.setdefault(int(user_id), nation)

        user_guesses: Dict[discord.Member, Set[Tuple[discord.Message, str]]] = {}
        prefix = "!nation "
        async for message in ctx.bot.get_channel(641847054477295629).history(
            after=discord.Object(id=LAST_MAGICALITY_SNOWFLAKE), limit=None
        ):
            author = message.guild.get_member(message.author)
            if not author or author.bot:
                continue
            if author.joined_at < discord.utils.snowflake_time(LAST_MAGICALITY_SNOWFLAKE):
                return
            if len(author.roles) == 1:
                continue
            if author.id in self.nations:
                continue
            user_guesses.setdefault(author, set())
            if message.content.startswith(prefix):
                guess = nid(message.content[len(prefix) :])
                if guess:
                    user_guesses[author].add((message, guess))
        user_guesses_message = []
        for k, v in user_guesses.items():
            if v:
                l = [f"[{rnid(n)}]({m.jump_url})" for m, n in v]
                s = humanize_list(l)
            else:
                s = "*No nations found.*"
            user_guesses_message.append(f"{k.mention}: {s}")
        embed = discord.Embed(
            color=await ctx.embed_color(), description="\n".join(user_guesses_message),
        )
        await ctx.send(
            "Data imported.\n"
            "The following users joined and were given roles after Magicality went down; "
            "the following are my best guesses as to what nation they were registered with.\n"
            "Multiple nations means they posted more than one nation.",
            embed=embed,
        )

    @identify.group(name="task", invoke_without_subcommand=True, autohelp=False)
    @checks.is_owner()
    async def _identify_task(self, ctx: Context, *, run: bool = False):
        """View the status of the autorole task."""
        if self.task.done():
            try:
                self.task.result()
            except Exception as e:
                tb = "".join(traceback.format_exception(type(e), e, e.__traceback__.tb_next))
                message = f"Exception {e.__class__.__name__} occurred in {self.__class__.__name__} task.\n\n{tb}"
            else:
                # whuh? how'd we get to this line? :thonk:
                return await ctx.send(
                    "Somehow an infinite loop finished. This should never ever happen."
                )
        else:
            if run:
                if self._waiting():
                    self.waiting_for.cancel()
                    await asyncio.sleep(0.1)  # yield to show success
            stack = self.task.get_stack()
            if self.waiting_for:
                wakeupat = stack[-1].f_locals.get("wakeupat")
            else:
                wakeupat = None
            message = "Task is {}\n{}".format(
                "running"
                if not self._waiting()
                else "suspended until resumed"
                if wakeupat is None
                else "suspended for duration: {}".format(
                    humanize_timedelta(timedelta=wakeupat - datetime.now(timezone.utc))
                ),
                "\n\n".join("\n".join(frame) for frame in map(traceback.format_stack, stack)),
            )
        await ctx.send_interactive(pagify(message, shorten_by=10), box_lang="py")

    @_identify_task.command(name="restart")
    @checks.is_owner()
    async def _restart_task(self, ctx: Context):
        with contextlib.suppress(Exception):
            self.task.cancel()
        self.task = ctx.bot.loop.create_task(self._task())
        self.task.add_done_callback(self._callback)
        self.waiting_for = None
        await ctx.tick()

    @identify.command(name="nation", pass_context=True)
    async def _identify_nation(self, ctx: Context, *, nation):
        """Associate your nation with your account.

        Note that you may only add one nation every hour,
        and that only one nation may be associated with your account."""
        await self.set_nation(nation, ctx.author, ctx, False)

    async def set_nation(
        self, nation: str, member: discord.User, ctx: Context, third_party: bool,
    ):
        if not third_party:
            try:
                cooldown = self.cooldowns[member] + timedelta(hours=1) - datetime.now(timezone.utc)
                if cooldown.total_seconds() > 0:
                    return await ctx.send(
                        "You may only claim nations every hour. You may claim another nation in {:.0f} minutes.".format(
                            cooldown.total_seconds() // 60
                        ),
                    )
                del self.cooldowns[member]
            except KeyError:
                pass
        # nation = nid(self.usernid.match(nation).groups()[-1])
        nation = self.usernid.match(nation)
        if not nation or (nation.group(1) and nation.group(1).casefold() != "nation"):
            return await ctx.send("That doesn't look like a nation name to me.")
        nation = nid(nation.group(2))
        if nation in self.nations.inv:
            if third_party and not member:
                del self.nations.inv[nation]
                return await ctx.send("Nation removed.")
            if self.nations.inv[nation] == member.id:
                if third_party:
                    return await ctx.send("{} has already claimed that nation.".format(member))
                return await ctx.send("You already claimed that nation.")
            if not third_party:
                return await ctx.send(
                    "That nation was already claimed by {}.\n".format(
                        await self.maybe_fetch(self.nations.inv[nation])
                    ),
                )
        if not member:
            return await ctx.send("No user has claimed that nation.")
        if not third_party and member.id in self.nations:
            answer = None

            def check(m):
                nonlocal answer
                if m.author != member or m.channel != ctx.channel:
                    return False
                lowered = m.content.lower()
                if lowered in ("yes", "y"):
                    answer = True
                elif lowered in ("no", "n"):
                    answer = False
                else:
                    return False
                return True

            await ctx.send(
                "You may only claim one nation at a time. Are you sure you want to replace {} with {}?".format(
                    rnid(self.nations[member.id]), rnid(nation)
                ),
            )
            try:
                await self.bot.wait_for("message", timeout=60, check=check)
            except asyncio.TimeoutError:
                answer = False
            if not answer:
                return await ctx.send("Okay, I haven't changed your nation.")
        async with ctx.typing():
            try:
                data = await Api("region wa", nation=nation)
            except ValueError:
                return await self.bot.send_message(ctx, "I can't find that nation. :shrug:")
            self.nations[member.id] = nation
            self.cooldowns[member] = datetime.now(timezone.utc)
            tnp = nid(data["REGION"].text) == "the_north_pacific"
            self.cache.setdefault(nation, set()).add("residents" if tnp else "visitors")
            self.cache[nation].discard("visitors" if tnp else "residents")
            if True in self.cache[nation] and data["UNSTATUS"].text.lower() != "non-member":
                self.cache[nation].add("wa residents")
            try:
                await self._add_roles(member)
            except discord.Forbidden:
                if third_party:
                    await ctx.send(
                        f"I couldn't modify {member}'s roles. Please check my permissions.",
                    )
                else:
                    await ctx.send(
                        "I couldn't modify your roles. Please ask an administrator to check my permissions.",
                    )
        await ctx.send("Nation set.")

    @identify.command(name="remove")
    async def _identify_remove(self, ctx):
        """Remove the nation associated with your account."""
        author = ctx.message.author
        try:
            del self.nations[author.id]
        except KeyError:
            return await ctx.send("You have no nation associated with your account.")
        try:
            await self._add_roles(author)
        except discord.Forbidden:
            return await ctx.send(
                "I couldn't modify your roles. Please ask an administrator to check my permissions."
            )
        await ctx.send("Nation removed.")

    @identify.command(name="show", pass_context=True)
    async def _identify_show(self, ctx, *, member: Union[discord.Member, str] = None):
        """List all nations associated with yourself or the specified member."""
        if not member:
            member = ctx.message.author
        elif isinstance(member, str):
            member = nid(self.usernid.match(member).group(2))
        nation = None
        if isinstance(member, str) and member in self.nations.inv:
            member, nation = (
                await self.maybe_fetch(self.nations.inv[member]),
                rnid(member),
            )
        elif isinstance(member, discord.Member) and member.id in self.nations:
            member, nation = member, rnid(self.nations[member.id])
        if nation:
            await ctx.send("{} 👉 {}".format(nation, member))
        else:
            await ctx.send("{} is not in my data.".format(member))

    @identify.group(name="set", invoke_without_command=True)
    @checks.admin_or_permissions(manage_roles=True)
    async def _identify_set(self, ctx, nation, *, member: discord.Member = None):
        """Set various options.

        This command itself can set a nation for another user,
        or remove that nation from an account if no member is specified."""
        await self.set_nation(nation, member, ctx, ctx.message.author != member)

    @_identify_set.command(name="toggle", no_pm=True)
    @checks.admin_or_permissions(manage_guild=True)
    async def _set_toggle(self, ctx, *, true_false: bool = None):
        """Toggle autoroles on this server."""
        server = ctx.guild
        if true_false is not None:
            await self.config.guild(ctx.guild).on.set(true_false)
        else:
            true_false = await self.config.guild(ctx.guild).on()
        if not true_false:
            self.enabled_guilds.discard(ctx.guild)
            return await ctx.send("Autoroles for this server are **off**.")
        self.enabled_guilds.add(ctx.guild)
        await ctx.send("Autoroles for this server are **on**.")

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Role members on join."""
        sid = member.guild.id
        if member.bot or member.guild not in self.enabled_guilds:
            LOG.debug("%s is a bot or in a disabled guild.", member)
            return
        if member.id in self.nations:
            LOG.debug("%s already has a nation.", member)
            return await self._add_roles(member, as_user=False)
        servers = (147373390104231936, 492050318775943170)
        if sid not in servers:
            return
        channel = self.bot.get_channel(
            (147373859736256512, 736372226831679499)[servers.index(sid)]
        )
        if not channel or not channel.permissions_for(member).send_messages:
            return
        await asyncio.sleep(1)
        self.cooldowns.pop(member, None)
        question = await channel.send(
            f"{member.mention}: Greetings! Do you have a nation on nationstates.net? "
            "If so, could you post a direct link to that nation, "
            "so I can give you the proper roles?"
        )
        nation = None

        def check(answer):
            nonlocal nation
            if answer.author != member:
                return
            if (
                answer.channel != question.channel
                and answer.channel.type != discord.ChannelType.private
            ):
                return False
            if (
                answer.created_at - question.created_at
            ).total_seconds() > 600 and member.guild.me not in answer.mentions:
                return False
            match = self.recheck.match(answer.content)
            if not match or match.group(1).casefold() != "nation":
                return False
            nation = match.group(2)
            return bool(nation)

        answer = await self.bot.wait_for("message", check=check)
        await self.set_nation(nation, member, answer.channel, False)

    def __unload(self):
        with contextlib.suppress(Exception):
            self.task.cancel()

    cog_unload = __unload
    __del__ = __unload

    @identify.before_invoke
    async def _before_invoke(self, ctx):
        if ctx.cog is not self:
            return
        xra = Api.xra
        if xra:
            raise commands.CommandOnCooldown(None, time.time() - xra)

    async def _add_roles(self, user: discord.abc.User, *, as_user=True):
        if user.bot or user.id not in self.nations:
            return
        if as_user or not isinstance(user, discord.Member):
            members = map(discord.Guild.get_member, self.bot.guilds, repeat(user.id))
        else:
            members = (user,)
        nation = rnid(self.nations[user.id])
        all_guilds = await self.config.all_guilds()
        for member in members:
            if member and all_guilds.get(member.guild.id, {}).get("on"):
                to_add, to_remove = self._role_set(member)
                if to_remove:
                    await member.remove_roles(*to_remove, reason=f"Set nation to {nation}")
                if to_add:
                    await member.add_roles(*to_add, reason=f"Set nation to {nation}")

    async def _task(self):
        while self is self.bot.get_cog(self.__class__.__name__):
            localcache = {"ALL": {"ex-nation"}}
            key = (await self.bot.get_shared_api_tokens("google_sheets")).get("api_key", None)
            if not key:
                self.waiting_for = self.bot.loop.create_future()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.waiting_for
                self.waiting_for = None
                continue

            # get new cache by abusing dict mutability
            async with aiohttp.ClientSession() as session:
                await asyncio.gather(
                    *(
                        on_exception(expo, (aiohttp.ClientError, SheetsError), max_tries=8,)(
                            getattr(self, attr)
                        )(session, localcache, key)
                        for attr in dir(self)
                        if attr.startswith("_task_")
                    )
                )  # do it all at once
                await asyncio.gather(
                    *(
                        on_exception(expo, (aiohttp.ClientError, SheetsError), max_tries=8,)(
                            getattr(self, attr)
                        )(session, localcache, key)
                        for attr in dir(self)
                        if attr.startswith("_sub_task_")
                    )
                )
            localcache.pop(None, None)

            # atomically update self.cache
            self.cache = dict(map(lambda t: (t[0], set(map(str.lower, t[1]))), localcache.items()))
            del localcache

            # update roles
            await on_exception(expo, (RuntimeError,), max_tries=8)(self._role_task)()

            # sleep until the next 12-hour mark
            t = datetime.now(timezone.utc)
            timetil = PERIOD - (t.timestamp() % PERIOD)
            if timetil < PERIOD / 4:
                timetil += PERIOD
            wakeupat = t + timedelta(seconds=timetil)
            self.waiting_for = asyncio.ensure_future(asyncio.sleep(timetil))
            with contextlib.suppress(asyncio.CancelledError):
                await self.waiting_for
            del wakeupat

    async def _task_region(self, session, localcache, _):
        powers = {
            "X": "Executive Officer",
            "W": "World Assembly Officer",
            "A": "Appearance Officer",
            "B": "Border Control Officer",
            "C": "Communications Officer",
            "E": "Embassies Officer",
            "P": "Polls Officer",
        }
        root = await Api(
            "nations officers delegate delegateauth founder founderauth",
            region="the_north_pacific",
        )
        localcache["ALL"].update(powers.values())
        title = "residents"
        localcache["ALL"].add(title)
        for x in re.finditer(r"[{}]+".format(NVALID), root["NATIONS"].text):
            localcache.setdefault(x.group(0), set()).add(title)
        delegate, founder = root["DELEGATE"].text, root["FOUNDER"].text
        if delegate != "0":
            localcache.setdefault(delegate, set()).update(
                map(powers.__getitem__, root.find("DELEGATEAUTH").text)
            )
        if founder != "0":
            localcache.setdefault(founder, set()).update(
                map(powers.__getitem__, root.find("FOUNDERAUTH").text)
            )
        officers = root["OFFICERS"].findall("OFFICER")
        for officer in officers:
            localcache.setdefault(officer.find("NATION").text, set()).update(
                map(powers.__getitem__, officer.find("AUTHORITY").text)
            )

    async def _task_citizenship(self, session, localcache, key):
        async with session.get(
            "https://sheets.googleapis.com/v4/spreadsheets/"
            "1aQ9EplmCzZLz7AmWQwpSXiCPo60AdyGG97PR1lD2tWM/values/Citizens!D3:D",
            params={"majorDimension": "columns", "key": key},
        ) as response:
            json = await response.json()
        if "error" in json:
            raise SheetsError(json["error"]["message"])
        title = json["range"].split("!")[0].strip("'").replace("''", "'")
        localcache["ALL"].add(title)
        for nation in map(nid, json["values"][0]):
            localcache.setdefault(nation, set()).add(title)

    async def _task_army(self, session, localcache, key):
        async with session.get(
            "https://sheets.googleapis.com/v4/spreadsheets/"
            "12l7zoYXrV7L_5uXM5HeVoe93ZBU70ypYf3jS1I0TZuE/values/Roster!B4:B",
            params={"majorDimension": "columns", "key": key},
        ) as response:
            json = await response.json()
        if "error" in json:
            raise SheetsError(json["error"]["message"])
        title = "NPA Soldiers"
        localcache["ALL"].add(title)
        for nation in map(nid, json["values"][0]):
            localcache.setdefault(nation, set()).add(title)

    async def _task_government(self, session, localcache, key):
        async with session.get(
            "https://sheets.googleapis.com/v4/spreadsheets/"
            "1hBUA7i7n5-0RXNbItLDHA1lb_D9rKQp4JJ1hc5InD8k/",
            params={"key": key},
        ) as response:
            json = await response.json()
        if "error" in json:
            raise SheetsError(json["error"]["message"])
        query = MultiDict(majorDimension="rows", key=key)
        for sheet in filter(lambda s: not s["properties"].get("hidden", False), json["sheets"]):
            query.add("ranges", "{}!A2:C".format(sheet["properties"]["title"]))
        async with session.get(
            "https://sheets.googleapis.com/v4/spreadsheets/"
            "1hBUA7i7n5-0RXNbItLDHA1lb_D9rKQp4JJ1hc5InD8k/values:batchGet/",
            params=query,
        ) as response:
            json = await response.json()
        if "error" in json:
            raise SheetsError(json["error"]["message"])
        for ranges in json["valueRanges"]:
            executive = False
            sheettitle = ranges["range"].split("!")[0].strip("'").replace("''", "'")
            localcache["ALL"].add(sheettitle)
            for nation, title in starmap(tnid, ranges["values"]):
                if executive:
                    title = "Minister of " + title
                localcache.setdefault(nation, set()).update((sheettitle, title))
                localcache["ALL"].add(title)
                if title.lower() == "delegate":
                    executive = True

    async def _sub_task_world(self, session, localcache, _):
        root = await Api("nations")
        title = "visitors"
        localcache["ALL"].add(title)
        for x in re.finditer(r"[{}]+".format(NVALID), root["NATIONS"].text):
            n = x.group(0)
            if n not in localcache:
                localcache[n] = set((title,))

    async def _sub_task_wa(self, session, localcache, _):
        root = await Api("members", wa="1")
        title = "wa residents"
        localcache["ALL"].add(title)
        for x in re.finditer(r"[{}]+".format(NVALID), root["MEMBERS"].text):
            n = x.group(0)
            if n in localcache and "residents" in map(str.lower, localcache[n]):
                localcache[n].add(title)

    async def _role_task(self):
        all_guilds = await self.config.all_guilds()
        for server in filter(
            bool, map(self.bot.get_guild, filter(lambda k: all_guilds[k]["on"], all_guilds),),
        ):
            for i, member in enumerate(
                filter(lambda m: not m.bot and m.id in self.nations, server.members), 1
            ):
                to_add, to_remove = self._role_set(member)
                if to_remove:
                    await member.remove_roles(
                        *to_remove, reason=f"{self.__class__.__name__} autorole task"
                    )
                if to_add:
                    await member.add_roles(
                        *to_add, reason=f"{self.__class__.__name__} autorole task"
                    )
                if not i % 5:
                    await asyncio.sleep(0.1)  # yield to other tasks

    def _role_set(self, member) -> Tuple[Set[discord.Role], Set[discord.Role]]:
        def torole(n):
            return next(filter(lambda r: r.name.lower() == n, member.guild.roles), None)

        alltitles = self.cache["ALL"]
        base = filter(lambda role: role.name.lower() not in alltitles, member.roles)
        roles = set(map(torole, self.cache.get(self.nations[member.id], ("ex-nation",)))).union(
            base
        )
        roles.discard(None)
        member_roles = set(member.roles)
        to_add = roles - member_roles
        to_remove = member_roles - roles
        if to_add or to_remove:
            LOG.info(
                "\n\n%s\nTo add: %s\nTo remove: %s",
                member,
                humanize_list([r.name for r in to_add]) if to_add else None,
                humanize_list([r.name for r in to_remove]) if to_remove else None,
            )
        return [], []
        # return to_add, to_remove

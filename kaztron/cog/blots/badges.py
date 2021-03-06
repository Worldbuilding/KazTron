import logging
from typing import Optional

import discord
from discord.ext import commands

from kaztron import KazCog
from kaztron.cog.blots import model
from kaztron.cog.blots.controller import BlotsBadgeController, BlotsConfig
from kaztron.driver import database
from kaztron.driver.pagination import Pagination
from kaztron.kazcog import ready_only
from kaztron.theme import solarized
from kaztron.utils.checks import mod_channels, mod_only
from kaztron.utils.converter import MemberConverter2
from kaztron.utils.datetime import format_datetime
from kaztron.utils.discord import check_mod, user_mention, Limits, get_command_prefix
from kaztron.utils.embeds import EmbedSplitter
from kaztron.utils.logging import message_log_str
from kaztron.utils.strings import split_chunks_on

logger = logging.getLogger(__name__)


class BadgeManager(KazCog):
    """!kazhelp
    category: Commands
    brief: Give users badges for community contributions. Part of Inkblood Writing Guild BLOTS.
    description: |
        BadgeManager lets users give each other badges for contribution to each others' projects
        and to the community.

        Badges can be given in the {{badge_channel}} channel. {{name}} will detect all messages in
        this channel and let you know when it detects that you've given a badge. **Check the
        {{name}} response to make sure your badge registered properly.**

        The badge format is:

        ```
        To: @user
        For: :badgeEmoji: Description of badge
        ```

        in *one single message*, where

        * `@user` is a **mention** of the user you want to give the badge to.
        * `:badgeEmoji:` is one of the badge emoji, currently consisting of the Guild, Writing,
          Worldbuilding, Idea, Critique, Art, Resource or Community emoji.
        * `Description of badge` is a textual description of why you're giving the badge.

        You may also use bold, italic or underline formatting around the `To:` and `For:` fields.

        You can also edit or delete your old message, and {{name}} will detect the change and update
        its badge list accordingly.
    contents:
        - badges:
            - report
            - load
    """
    cog_config: BlotsConfig

    ITEMS_PER_PAGE = 8
    EMBED_COLOUR = solarized.green

    badge_channel_id = KazCog.config.blots.badge_channel

    def __init__(self, bot):
        super().__init__(bot, 'blots', BlotsConfig)
        self.channel = None  # type: discord.Channel
        self.c = None  # type: BlotsBadgeController

    async def on_ready(self):
        await super().on_ready()
        channel_id = self.config.get('blots', 'badge_channel')
        self.channel = self.get_channel(channel_id)
        self.c = BlotsBadgeController(self.server, self.config)

    def export_kazhelp_vars(self):
        return {'badge_channel': '#' + self.channel.name}

    async def add_badge(self, message: discord.Message, suppress_errors=False) \
            -> Optional[model.Badge]:

        # Check if this seems like a command (usually badge commands for the badge channel)
        class FakeContext:
            def __init__(self, bot, msg):
                self.bot = bot
                self.message = msg
        # noinspection PyTypeChecker
        prefix = get_command_prefix(FakeContext(self.bot, message))
        msg_init = message.content.strip()
        if msg_init.startswith(prefix) and\
                (len(msg_init) == len(prefix) or not msg_init[len(prefix)].isspace()):
            logger.warning("Skipping badge parsing: message appears to be a command.")
            return

        # Parsing
        badges = [b for b in model.BadgeType if b.pattern.search(message.content)]
        reason = self._parse_badge_reason(message.content)
        dummy_context = type('', (object,), {'message': message})()

        # Validation: member
        if len(message.mentions) != 1:
            logger.warning("Badge must mention exactly 1 user: {}"
                .format(', '.join([m.nick or m.name for m in message.mentions])))
            if not suppress_errors:
                await self.bot.send_message(self.channel,
                    "**Error**: Badges must mention exactly 1 user.")
            return
        elif message.mentions[0] == message.author:
            logger.warning("Cannot give badge to self")
            if not suppress_errors:
                await self.bot.send_message(self.channel,
                "**Error**: You can't give yourself a badge!")
            return

        # Validation: badge type
        if len(badges) != 1:
            logger.warning("Only 1 badge can be given at a time: {!s}".format(badges))
            if not suppress_errors:
                await self.bot.send_message(self.channel,
                    "{} **Error**: You can only give 1 badge at a time (found {:d}).".format(
                        message.author.mention, len(badges)))
            return
        elif model.BadgeType.Guild in badges and not check_mod(dummy_context):
            logger.warning("Guild badge by non-mod: {}"
                .format(message.author.nick or message.author.name))
            if not suppress_errors:
                await self.bot.send_message(self.channel,
                    "{} **Error**: Only Overseers can give the Guild badge."
                        .format(message.author.mention))
            return

        # Validation: reason
        if reason is None:
            logger.warning("Cannot find badge reason")
            if not suppress_errors:
                await self.bot.send_message(self.channel,
                    ("{} **Error**: Can't find the badge reason. Make sure the badge reason "
                     "has the text `**For:**` at the beginning of the line.")
                        .format(message.author.mention))
            return

        badge_row = self.c.save_badge(
            message_id=message.id,
            member=message.mentions[0],
            from_member=message.author,
            badge=badges[0],
            reason=reason,
            timestamp=message.timestamp
        )
        return badge_row

    @ready_only
    async def on_message(self, message: discord.Message):
        """
        Message handler. Monitor messages for badges in the badge channel.
        """
        if message.channel != self.channel or message.author.id == self.bot.user.id:
            return

        logger.info("Detected message in badge channel (#{})".format(self.channel.name))
        logger.debug("Badge Manager: {}".format(message_log_str(message)))
        badge_row = await self.add_badge(message)
        if badge_row:
            await self.bot.send_message(self.channel,
                "Congratulations, {1.mention}! {0.mention} just gave you the {2} badge!".format(
                    message.author,
                    self.server.get_member(badge_row.user.discord_id),
                    self._get_badge(badge_row.badge))
            )

    @ready_only
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """
        Edited message handler. Monitor messages for changed badges in the badge channel.
        """
        if before.channel != self.channel or before.author.id == self.bot.user.id:
            return

        logger.info("Detected edited message in badge channel (#{})".format(self.channel.name))
        logger.debug("Badge Manager: {}".format(message_log_str(after)))
        badge_row = await self.add_badge(after)
        if badge_row:
            await self.bot.send_message(self.channel,
                "{0.mention} Your {2} badge to {1.mention} has been updated.".format(
                    after.author,
                    self.server.get_member(badge_row.user.discord_id),
                    self._get_badge(badge_row.badge))
            )

    @ready_only
    async def on_message_delete(self, message: discord.Message):
        """
        Delete the badge associated to this message.
        """
        if message.channel != self.channel or message.author.id == self.bot.user.id:
            return

        logger.info("Detected deleted message in badge channel (#{})".format(self.channel.name))
        logger.debug("Badge Manager: {}".format(message_log_str(message)))
        badge_row = self.c.delete_badge(message.id)
        if badge_row:
            await self.bot.send_message(self.channel,
                "{0.mention} Your {2} badge to {1.mention} has been deleted.".format(
                    message.author,
                    self.server.get_member(badge_row.user.discord_id),
                    self._get_badge(badge_row.badge))
            )

    @staticmethod
    def _parse_badge_reason(text: str):
        reason = None
        for line in text.split('\n'):
            try:
                field, value = line.split(':', maxsplit=1)
                if field.strip(' \n\r\t*_').lower() == 'for':
                    reason = value.lstrip('*_').strip()  # type: str
                    for badge in model.BadgeType:
                        reason = badge.pattern.sub('', reason)
                    break
            except ValueError:  # can't unpack
                continue
        return reason

    def _get_badge(self, badge: model.BadgeType):
        """ Get a displayable badge. """
        return str(discord.utils.get(self.server.emojis, name=badge.name))

    @commands.group(pass_context=True, ignore_extra=False, invoke_without_command=True)
    async def badges(self, ctx: commands.Context, user: MemberConverter2, page: int=None):
        """!kazhelp
        brief: Check a user's badges.
        description: |
            Check a user's badges.

            TIP: If you want to give a badge, leave a properly formatted message in
            {{badge_channel}}. See the top of the {{%BadgeManager}} page (web manual) or
            `.help BadgeManager` (in-bot help) for more information.
        parameters:
            - name: user
              type: "@mention"
              description: The user to check (as an @mention or a Discord ID).
            - name: page
              type: number
              optional: true
              description: The page number to access, if a user has more than 1 page of badges.
              default: last page (most recent)
        examples:
            - command: .badges @JaneDoe
              description: List all of JaneDoe's badges (most recent, if there are multiple pages).
            - command: .badges @JaneDoe 4
              description: List the 4th page of JaneDoe's badges.
        """
        user = user  # type: discord.Member  # for IDE type checking
        try:
            db_records = self.c.query_badges(member=user)
            paginator = Pagination(db_records, self.ITEMS_PER_PAGE, align_end=True)
            if page is not None:
                paginator.page = max(0, min(paginator.total_pages - 1, page-1))
            await self.show_badges(ctx.message.channel, paginator, user)
        except database.NoResultFound:
            await self.bot.say("{} hasn't gotten any badges yet!".format(user.mention))

    async def show_badges(self, dest: discord.Channel, badges: Pagination, member: discord.Member):
        es = EmbedSplitter(
            auto_truncate=True,
            title="Badges",
            description="{} - {:d} badges".format(member.mention, len(badges)),
            colour=self.EMBED_COLOUR
        )
        es.set_footer(text="Page {:d}/{:d} (total {:d} badges)"
            .format(badges.page + 1, badges.total_pages, len(badges)))
        for b in badges:  # type: model.Badge
            es.add_field_no_break(name=format_datetime(b.timestamp), value=self._get_badge(b.badge))
            es.add_field_no_break(name="From", value=user_mention(b.from_user.discord_id))
            es.add_field(name="For", value=b.reason + '\n' + r'\_'*16, inline=False)

        for em in es.finalize():
            await self.bot.send_message(dest, embed=em)

    @badges.command(pass_context=True, ignore_extra=True)
    @mod_only()
    @mod_channels()
    async def report(self, ctx: commands.Context, min_badges: int=1):
        """!kazhelp
        description: "Show a report of member badge counts."
        parameters:
            - name: min_badges
              optional: true
              type: number
              description: |
                Minimum number of badges a user needs to have to be included in the report.
        """
        if min_badges < 1:
            raise commands.BadArgument("`min` must be at least 1.")

        report_lines = ["**Badge report (minimum {:d} badges)**".format(min_badges)]
        for u, n in self.c.query_badge_report(min_badges):
            report_lines.append("{} - {:d} badges".format(user_mention(u.discord_id), n))

        for msg in split_chunks_on('\n'.join(report_lines), maxlen=Limits.MESSAGE):
            await self.bot.say(msg)

    @badges.command(pass_context=True, ignore_extra=True)
    @mod_only()
    async def load(self, ctx: commands.Context, messages: int=100):
        """!kazhelp
        description:
            Read the badge channel history and add any missing badges. Mostly useful for
            transitioning to bot-managed badges, or loading missed badges from bot downtime.
        parameters:
            - name: messages
              optional: true
              default: 100
              type: number
              description: Number of messages to read in history.
        """
        if messages <= 0:
            raise commands.BadArgument("`messages` must be positive")

        await self.bot.say(("Loading the last {:d} badge channel messages. "
                            "This might take a while...").format(messages))

        total_badges = 0
        async for message in self.bot.logs_from(self.channel, messages):
            if message.author.id == self.bot.user.id:
                continue
            logger.info("badges load: Attempting to add/update badge: " + message_log_str(message))
            badge_row = await self.add_badge(message, suppress_errors=True)
            if badge_row:
                total_badges += 1

        await self.bot.say("Added or updated {:d} badges.".format(total_badges))

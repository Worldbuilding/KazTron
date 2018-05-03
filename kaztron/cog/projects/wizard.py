import logging
from datetime import datetime
from typing import Dict, Tuple

import discord
from discord.ext import commands

from kaztron.utils.logging import message_log_str
from kaztron.utils.strings import count_words
from kaztron.utils.wizard import make_wizard, len_validator
from . import model as m, query as q

logger = logging.getLogger(__name__)


keys = ['title', 'genre', 'subgenre', 'type', 'pitch']
keys_optional = ['subgenre']

start_msg = "**New Project Wizard**\n\n" \
            "Let the server know what projects you're working on! Other members on the " \
            "server can look up any of your registered projects. To cancel this wizard, " \
            "type `.project cancel`."
start_edit_msg_fmt = "**Edit Project Wizard**\n\n" \
                     "You are editing your current project, {0.title}. If you don't want to " \
                     "change a previous value, type `None` for that question. To cancel this " \
                     "wizard, type `.project cancel`."
end_msg = "Your project is set up! Other members can now look it up and find out what" \
              "you're up to!\n\nIf you'd like to re-run this wizard to make changes to your " \
              "project, use `.project wizard`. You can also edit fields or add additional " \
              "info using the `.project set` series of commands. Do `.help projects set` " \
              "for more information."


questions = {
    'title': "What is your project's title?",
    'genre': lambda: ("What genre is your project? Available genres: {}. "
                      "(You can specify a more specific sub-genre later.)"
                      ).format(', '.join(o.name for o in q.query_genres())),
    'subgenre': "What specific sub-genre is your project? Type `none` if you don't want to add "
                "a sub-genre.",
    'type': lambda: "What kind of project? Available types: {}."
                    .format(', '.join(o.name for o in q.query_project_types())),
    'pitch': "Give an elevator pitch (about 50 words) for your project!"
}


def pitch_validator(s: str):
    wc = count_words(s)
    if wc > m.Project.MAX_PITCH_WORDS:
        raise ValueError("Elevator pitch too long ({:d} words, max {:d})"
            .format(wc, m.Project.MAX_PITCH_WORDS))
    return s


validators = {
    'title': len_validator(m.Project.MAX_TITLE),
    'genre': lambda x: q.get_genre(x),
    'subgenre': len_validator(m.Project.MAX_SHORT),
    'type': lambda x: q.get_project_type(x),
    'pitch': pitch_validator,
    'description': len_validator(m.Project.MAX_FIELD)
}

serializers = {
    'genre': lambda x: x.name,
    'type': lambda x: x.name
}


ProjectWizard = make_wizard(keys, questions, validators, serializers, keys_optional)

UserWizardMap = Dict[str, ProjectWizard]


class WizardManager:
    @classmethod
    def from_dict(cls, bot: commands.Bot, data: dict):
        obj = cls(bot)
        for uid, d in data.get('new', {}).items():
            try:
                obj.wizards['new'][uid] = ProjectWizard.from_dict(d)
            except ValueError:
                pass

        obj.edit_wizards = {}
        for uid, d in data.get('edit', {}).items():
            try:
                obj.wizards['edit'][uid] = ProjectWizard.from_dict(d)
                obj.wizards['edit'][uid].opts = ProjectWizard.question_keys
            except ValueError:
                pass
        return obj

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.wizards = {
            'new': {},
            'edit': {},
        }

    def has_open_wizard(self, member: discord.Member):
        return any(member.id in w for w in self.wizards.values())

    def get_wizard_for(self, member: discord.Member) -> Tuple[str, ProjectWizard]:
        for name, uw_map in self.wizards.items():
            try:
                if uw_map[member.id] is None:
                    raise KeyError
                return name, uw_map[member.id]
            except KeyError:
                continue
        else:
            raise KeyError("Member {} does not have an active wizard".format(member))

    async def send_question(self, member: discord.Member):
        await self.bot.send_message(member, self.get_wizard_for(member)[1].question)

    async def create_new_wizard(self, member: discord.Member, timestamp: datetime):
        if self.has_open_wizard(member):
            raise commands.CommandError("You already have an ongoing wizard!")

        logger.info("Starting 'new' wizard for for {}".format(member))
        self.wizards['new'][member.id] = ProjectWizard(member.id, timestamp)

        try:
            await self.bot.send_message(member, start_msg)
            await self.send_question(member)
        except Exception:
            self.cancel_wizards(member)
            raise

    async def create_edit_wizard(self,
                                 member: discord.Member, timestamp: datetime, proj: m.Project):
        if self.has_open_wizard(member):
            raise commands.CommandError("You already have an ongoing wizard!")

        logger.info("Starting 'edit' wizard for for {}".format(member))
        w = ProjectWizard(member.id, timestamp)
        w.opts = ProjectWizard.question_keys
        self.wizards['edit'][member.id] = w

        try:
            await self.bot.send_message(member, start_edit_msg_fmt.format(proj))
            await self.send_question(member)
        except Exception:
            self.cancel_wizards(member)
            raise

    def process_answer(self, message: discord.Message):
        wiz_name, wizard = self.get_wizard_for(message.author)

        logger.info("Processing '{}' wizard answer for {}".format(wiz_name, message.author))
        logger.debug(message_log_str(message))

        wizard.answer(message.content)

    async def close_wizard(self, member: discord.Member) -> Tuple[str, ProjectWizard]:
        wiz_name, wizard = self.get_wizard_for(member)
        if wizard.is_done:
            logger.info("Closing '{}' wizard for {}".format(wiz_name, member))
            del self.wizards[wiz_name][member.id]
            await self.bot.send_message(member, end_msg)
            return wiz_name, wizard
        else:
            raise KeyError("Wizard for user {} not completed yet".format(member))

    async def cancel_wizards(self, member: discord.Member):
        for name, user_wizard_map in self.wizards.items():
            try:
                del user_wizard_map[member.id]
            except KeyError:
                pass  # no open wizard of this kind
            else:
                logger.info("Cancelled {} project wizard for user {}".format(name, member))
                if name == 'new':
                    await self.bot.send_message(member, "New project has been cancelled.")
                elif name == 'edit':
                    await self.bot.send_message(member, "Editing your project has been cancelled.")
                else:
                    await self.bot.send_message(member, "Wizard has been cancelled (generic msg).")

    def to_dict(self) -> dict:
        ret = {}
        for name, wiz_map in self.wizards.items():
            ret[name] = {uid: wiz.to_dict() for uid, wiz in wiz_map.items()}
        return ret

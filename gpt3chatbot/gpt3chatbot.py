import logging
import os
import re
import time
from collections import deque
from typing import Union

import discord
import openai
from redbot.core import Config
from redbot.core import commands

from gpt3chatbot.personalities import personalities_dict

log = logging.getLogger("red.tytocogsv3.gpt3chatbot")
log.setLevel(os.getenv("TYTOCOGS_LOG_LEVEL", "INFO"))

CUSTOM_EMOJI = re.compile("<(?P<animated>a?):(?P<name>[a-zA-Z0-9_]{2,32}):(?P<id>[0-9]{18,22})>")  # from brainshop cog


class GPT3ChatBot(commands.Cog):
    """AI chatbot Using GPT3

    An artificial intelligence chatbot using OpenAI's GPT3 (https://openai.org)."""

    def __init__(self, bot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot
        self.config = Config.get_conf(self, identifier=259390542)  # randomly generated identifier
        default_global = {
            "reply": True,
            "memory": 20,
            "personalities": personalities_dict,
        }
        self.config.register_global(**default_global)
        default_guild = {  # default per-guild settings
            "reply": True,
            "channels": [],
            "allowlist": [],
            "blacklist": [],
            "personality": "Aurora",
        }
        self.config.register_guild(**default_guild)
        default_member = {"personality": "Aurora", "chat_log": []}
        self.config.register_member(**default_member)
        default_user = {"personality": "Aurora", "chat_log": []}
        self.config.register_user(**default_user)

    @staticmethod
    async def _filter_custom_emoji(message: str) -> str:
        return CUSTOM_EMOJI.sub("", message).strip()

    async def _filter_message(self, message):
        """Filter the message down to just the content, cleaning custom emoji and the bot mention
        :param message:
        :return:
        """

        # Remove bot mention
        filtered = re.sub(f"<@!?{self.bot.user.id}>", "", message.content)
        # clean custom emoji
        filtered = await self._filter_custom_emoji(filtered)
        if not filtered:
            return None
        return filtered

    @commands.Cog.listener("on_message_without_command")
    async def _message_listener(self, message: discord.Message):
        """This does stuff!"""
        if not await self._should_respond(message=message):
            return
        # Get OpenAI API Key
        openai_api = await self.bot.get_shared_api_tokens("openai")
        if not (key := openai_api.get("key")):
            log.error("No API key found!")
            return

        # if filtered message is blank, we can't respond
        if not await self._filter_message(message):
            return

        # Get response from OpenAI
        async with message.channel.typing():
            response = await self._get_response(key=key, message=message)
            log.debug(f"{response=}")
            if not response:  # sometimes blank?
                return

        # update the chat log with the new interaction
        await self._update_chat_log(
            author=message.author,
            question=await self._filter_message(message),
            answer=response,
        )

        if hasattr(message, "reply"):
            return await message.reply(response, mention_author=False)
        return await message.channel.send(response)

    async def _should_respond(self, message: discord.Message) -> bool:
        """1. Check if we should response to an incoming message.
        :param message: the incoming message to tests (discord.Message)
        :return: True if we should respond, False otherwise (bool)"""
        # ignore bots
        if message.author.bot:
            log.debug(f"Ignoring message, author is a bot: {message.author.bot=} | {message.clean_content=}")
            return False

        global_reply = await self.config.reply()
        starts_with_mention = message.content.startswith((f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"))
        is_reply = (message.reference is not None and message.reference.resolved is not None) and (
                message.reference.resolved.author.id == self.bot.user.id
        )
        log.debug(f"{is_reply=}: {message.clean_content=}")

        # command is in DMs
        if not message.guild:
            if not (starts_with_mention or is_reply) or not global_reply:
                log.debug("Ignoring DM, bot does not respond unless asked if global auto-reply is off.")
                return False
        # command is in a server
        else:
            log.info("Checking message from server.")
            # cog is disabled or bot cannot send messages in channel
            if (
                    await self.bot.cog_disabled_in_guild(self, message.guild)
                    or not message.channel.permissions_for(message.guild.me).send_messages
            ):
                log.debug("Cog is disabled or bot cannot send messages in channel")
                return False
            # noinspection PyTypeChecker
            guild_settings = await self.config.guild(message.guild).all()
            # Not in auto-channel
            if message.channel.id not in guild_settings["channels"]:
                if not (starts_with_mention or is_reply) or not (  # Does not start with mention/isn't a reply
                        guild_settings["reply"] or global_reply
                ):  # Both guild & global auto are toggled off
                    log.debug("Not in auto-channel, does not start with mention or auto-replies are turned off.")
                    return False
        # passed the checks
        log.info("Message OK.")
        return True

    async def _get_response(self, key: str, message: discord.Message) -> str:
        """Get the AIs response to the message.

        :param key: openai api key
        :param message:
        :return:
        """

        prompt_text = await self._build_prompt_from_chat_log(new_msg=message)

        response = openai.Completion.create(
            api_key=key,
            engine="ada",  # ada: $0.0008/1K tokens, babbage $0.0012/1K, curie$0.0060/1K, davinci $0.0600/1K
            prompt=prompt_text,
            temperature=0.8,
            max_tokens=100,
            top_p=1,
            best_of=1,
            frequency_penalty=0.8,
            presence_penalty=0.1,
            stop=[f"{message.author.display_name}:", "\n", "###", "\n###"],
        )
        reply: str = response["choices"][0]["text"].strip()
        return reply

    async def _build_prompt_from_chat_log(self, new_msg: discord.Message) -> str:
        """Serialize the chat_log into a prompt for the AI request.
        :param new_msg: The new message
        :return: prompt_text
        """
        available_personas = await self.config.personalities()
        group = await self._get_user_or_member_config_from_message(new_msg)
        persona_name = await group.personality()

        prompt_text, initial_chat_log = available_personas[persona_name].values()
        log.debug(f"{available_personas=}\n\n{persona_name=}\n\n{prompt_text}")
        async with group.chat_log() as chat_log:
            # include initial_chat_log and chat_log in prompt_text
            for entry in initial_chat_log + chat_log:
                prompt_text += (
                    f"{new_msg.author.display_name}: {entry['input']}\n" f"{persona_name}: {entry['reply']}\n###\n"
                )
        # add new request to prompt_text
        prompt_text += f"{new_msg.author.display_name}: {await self._filter_message(new_msg)}\n" f"{persona_name}:"
        log.debug(f"{prompt_text=}")
        return str(prompt_text)

    async def _get_user_or_member_config_from_message(self, message: discord.Message):
        return self.config.member(message.author) if message.guild else self.config.user(message.author)

    async def _get_user_or_member_config_from_author(self, author: Union[discord.User, discord.Member]):
        try:
            config = self.config.member(author)
        except AttributeError as e:
            log.debug(e)
            config = self.config.user(author)

        return config

    async def _update_chat_log(self, author: Union[discord.User, discord.Member], question: str, answer: str):
        """Update chat log with new response, so the bot can remember conversations."""
        new_response = {"timestamp": time.time(), "input": question, "reply": answer}
        log.info(f"Adding new response to the chat log: {author.id=}, {new_response['timestamp']=}")

        # create queue from chat chat_log
        group = await self._get_user_or_member_config_from_author(author)
        chat_log = await group.chat_log()
        deq_chat_log = deque(chat_log)
        log.info(f"Current chat log length: {len(deq_chat_log)}")
        # memory purge
        if not len(deq_chat_log) <= (mem := await self.config.memory()):
            log.debug(f"length at {mem=}, popping oldest log:")
            log.debug(deq_chat_log.popleft())
        # memory add
        deq_chat_log.append(new_response)
        # back to list for saving
        await group.chat_log.set(list(deq_chat_log))

    @commands.command(name="clearmylogs")
    async def clear_personal_history(self, ctx):
        """Clear chat log."""
        group = await self._get_user_or_member_config_from_message(ctx)
        await group.chat_log.set([])
        return await ctx.tick()

    @commands.command(name="listpersonas", aliases=["plist"])
    async def list_personas(self, ctx: commands.Context):
        """Lists available personas."""
        personas_mbed = discord.Embed(
            title="My personas", description="A list of configured personas by name, with description."
        )
        for persona in (persona_dict := await self.config.personalities()).keys():
            personas_mbed.add_field(name=persona, value=persona_dict[persona]["description"], inline=True)

        return await ctx.send(embed=personas_mbed)

    @commands.command(name="getpersona", aliases=["pget"])
    async def get_personas(self, ctx: commands.Context):
        """Get current persona."""
        persona_mbed = discord.Embed(
            title="My persona", description="Your currently configured persona's name, with description."
        )
        persona_dict = await self.config.personalities()
        group = await self._get_user_or_member_config_from_message(ctx)
        persona = await group.personality()
        persona_mbed.add_field(name=persona, value=persona_dict[persona]["description"], inline=True)

        return await ctx.send(embed=persona_mbed)

    @commands.command(name="setpersona", aliases=["pset"])
    async def change_member_personality(self, ctx: commands.Context, persona: str):
        """Change persona in replies to you."""
        group = await self._get_user_or_member_config_from_message(ctx)
        # get persona global dict
        persona_dict = await self.config.personalities()
        if persona.capitalize() not in persona_dict.keys():
            return await ctx.send(
                content="Not a valid persona. Use [p]list_personas.\n"
                        f"Your current persona is `{await group.personality()}`"
            )
        # set new persona
        await group.personality.set(persona.capitalize())
        # clear chat log
        async with group.chat_log() as chat_log:
            chat_log: list
            chat_log.clear()
        return await ctx.tick()

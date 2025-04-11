# -*- coding: utf-8 -*-
import asyncio
import contextlib
import datetime
import itertools
import os.path
import pickle
import pprint
import re
import sys
import traceback
import zlib
from base64 import b64decode
from contextlib import suppress
from copy import deepcopy
from io import BytesIO
from random import shuffle
from typing import Union, Optional
from urllib.parse import urlparse, parse_qs, quote

import aiofiles
import aiohttp
import disnake
from async_timeout import timeout
from disnake.ext import commands
from yt_dlp import YoutubeDL

import wavelink
from utils.client import BotCore
from utils.db import DBModel
from utils.music.audio_sources.deezer import deezer_regex
from utils.music.audio_sources.spotify import spotify_regex_w_user
from utils.music.checks import check_voice, has_player, has_source, is_requester, is_dj, \
    can_send_message_check, check_requester_channel, can_send_message, can_connect, check_deafen, check_pool_bots, \
    check_channel_limit, check_stage_topic, check_queue_loading, check_player_perm, check_yt_cooldown
from utils.music.converters import time_format, fix_characters, string_to_seconds, URL_REG, \
    YOUTUBE_VIDEO_REG, google_search, percentage, music_source_image
from utils.music.errors import GenericError, MissingVoicePerms, NoVoice, PoolException, parse_error, \
    EmptyFavIntegration, DiffVoiceChannel, NoPlayer
from utils.music.interactions import VolumeInteraction, QueueInteraction, SelectInteraction, FavMenuView, ViewMode, \
    SetStageTitle, SelectBotVoice, youtube_regex, ButtonInteraction
from utils.music.models import LavalinkPlayer, LavalinkTrack, LavalinkPlaylist, PartialTrack, PartialPlaylist, \
    native_sources, CustomYTDL
from utils.others import check_cmd, send_idle_embed, CustomContext, PlayerControls, queue_track_index, \
    pool_command, string_to_file, CommandArgparse, music_source_emoji_url, song_request_buttons, \
    select_bot_pool, ProgressBar, update_inter, get_source_emoji_cfg, music_source_emoji

sc_recommended = re.compile(r"https://soundcloud\.com/.*/recommended$")
sc_profile_regex = re.compile(r"<?https://soundcloud\.com/[a-zA-Z0-9_-]+>?$")

class Music(commands.Cog):

    emoji = "🎶"
    name = "Música"
    desc_prefix = f"[{emoji} {name}] | "

    playlist_opts = [
        disnake.OptionChoice("Misturar Playlist", "shuffle"),
        disnake.OptionChoice("Inverter Playlist", "reversed"),
    ]

    audio_formats = ("audio/mpeg", "audio/ogg", "audio/mp4", "audio/aac")

    providers_info = {
        "youtube": "ytsearch",
        "soundcloud": "scsearch",
        "spotify": "spsearch",
        "tidal": "tdsearch",
        "bandcamp": "bcsearch",
        "applemusic": "amsearch",
        "deezer": "dzsearch",
        "jiosaavn": "jssearch",
    }

    def __init__(self, bot: BotCore):

        self.bot = bot

        self.extra_hints = bot.config["EXTRA_HINTS"].split("||")

        self.song_request_concurrency = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

        self.player_interaction_concurrency = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

        self.song_request_cooldown = commands.CooldownMapping.from_cooldown(rate=1, per=300,
                                                                            type=commands.BucketType.member)

        self.music_settings_cooldown = commands.CooldownMapping.from_cooldown(rate=3, per=15,
                                                                              type=commands.BucketType.guild)

        if self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"]:
            self.error_report_queue = asyncio.Queue()
            self.error_report_task = bot.loop.create_task(self.error_report_loop())
        else:
            self.error_report_queue = None

    stage_cd = commands.CooldownMapping.from_cooldown(2, 45, commands.BucketType.guild)
    stage_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @commands.has_guild_permissions(manage_guild=True)
    @pool_command(
        only_voiced=True, name="setvoicestatus", aliases=["stagevc", "togglestageannounce", "announce", "vcannounce", "setstatus",
                                                         "voicestatus", "setvcstatus", "statusvc", "vcstatus", "stageannounce"],
        description="Ativar o sistema de anuncio/status automático do canal com o nome da música.",
        cooldown=stage_cd, max_concurrency=stage_mc, extras={"exclusive_cooldown": True},
        usage="{prefix}{cmd} <placeholders>\nEx: {track.author} - {track.title}"
    )
    async def setvoicestatus_legacy(self, ctx: CustomContext, *, template = ""):
        await self.set_voice_status.callback(self=self, inter=ctx, template=template)

    @commands.slash_command(
        description=f"{desc_prefix}Ativar/editar o sistema de anúncio/status automático do canal com o nome da música.",
        extras={"only_voiced": True, "exclusive_cooldown": True}, cooldown=stage_cd, max_concurrency=stage_mc,
        default_member_permissions=disnake.Permissions(manage_guild=True)
    )
    @commands.contexts(guild=True)
    async def set_voice_status(
            self, inter: disnake.AppCmdInter,
            template: str = commands.Param(
                name="modelo", default="",
                description="Especifique manualmente um modelo de status (inclua placeholders)."
            )
    ):

        if isinstance(template, commands.ParamInfo):
            template = ""

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            author = guild.get_member(inter.author.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            author = inter.author

        if not author.guild_permissions.manage_guild and not (await bot.is_owner(author)):
            raise GenericError("**Você não possui permissão de gerenciar servidor para ativar/desativar esse sistema.**")

        if not template:
            await inter.response.defer(ephemeral=True, with_message=True)
            global_data = await self.bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)
            view = SetStageTitle(ctx=inter, bot=bot, data=global_data, guild=guild)
            view.message = await inter.send(view=view, embeds=view.build_embeds(), ephemeral=True)
            await view.wait()
        else:
            if not any(p in template for p in SetStageTitle.placeholders):
                raise GenericError(f"**Você deve usar pelo menos um placeholder válido:** {SetStageTitle.placeholder_text}")

            try:
                player = bot.music.players[inter.guild_id]
            except KeyError:
                raise NoPlayer()

            if not author.voice:
                raise NoVoice()

            if author.id not in guild.me.voice.channel.voice_states:
                raise DiffVoiceChannel()

            await inter.response.defer()

            player.stage_title_event = True
            player.stage_title_template = template
            player.start_time = disnake.utils.utcnow()

            await player.update_stage_topic()

            await player.process_save_queue()

            player.set_command_log(text="ativou o status automático", emoji="📢")

            player.update = True

            if isinstance(inter, CustomContext):
                await inter.send("**O status automático foi definido com sucesso!**")
            else:
                await inter.edit_original_message("**O status automático foi definido com sucesso!**")


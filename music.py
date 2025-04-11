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


    @set_voice_status.autocomplete("modelo")
    async def default_models(self, inter: disnake.Interaction, query: str):
        return [
            "{track.title} - By: {track.author} | {track.timestamp}",
            "{track.emoji} | {track.title}",
            "{track.title} ( {track.playlist} )",
            "{track.title}  Solicitado por: {requester.name}",
        ]

    play_cd = commands.CooldownMapping.from_cooldown(3, 12, commands.BucketType.member)
    play_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @check_voice()
    @can_send_message_check()
    @commands.message_command(name="add to queue", extras={"check_player": False},
                              cooldown=play_cd, max_concurrency=play_mc)
    async def message_play(self, inter: disnake.MessageCommandInteraction):

        if not inter.target.content:
            emb = disnake.Embed(description=f"Não há texto na [mensagem]({inter.target.jump_url}) selecionada...",
                                color=disnake.Colour.red())
            await inter.send(embed=emb, ephemeral=True)
            return

        await self.play.callback(
            self=self,
            inter=inter,
            query=inter.target.content,
            position=0,
            options="",
            manual_selection=False,
            force_play="no",
        )

    @check_voice()
    @can_send_message_check()
    @commands.slash_command(name="search", extras={"check_player": False}, cooldown=play_cd, max_concurrency=play_mc,
                            description=f"{desc_prefix}Buscar música e escolher uma entre os resultados para tocar.")
    @commands.contexts(guild=True)
    async def search(
            self,
            inter: disnake.AppCmdInter,
            query: str = commands.Param(name="busca", desc="Nome ou link da música."),
            *,
            position: int = commands.Param(name="posição", description="Colocar a música em uma posição específica",
                                           default=0),
            force_play: str = commands.Param(
                name="tocar_agora",
                description="Tocar a música imediatamente (ao invés de adicionar na fila).",
                default="no",
                choices=[
                    disnake.OptionChoice(disnake.Localized("Yes", data={disnake.Locale.pt_BR: "Sim"}), "yes"),
                ]
            ),
            options: str = commands.Param(name="opções", description="Opções para processar playlist",
                                          choices=playlist_opts, default=False),
            server: str = commands.Param(name="server", desc="Usar um servidor de música específico na busca.",
                                         default=None),
            manual_bot_choice: str = commands.Param(
                name="selecionar_bot",
                description="Selecionar um bot disponível manualmente.",
                default="no",
                choices=[
                    disnake.OptionChoice(disnake.Localized("Yes", data={disnake.Locale.pt_BR: "Sim"}), "yes"),
                ]
            ),
    ):

        await self.play.callback(
            self=self,
            inter=inter,
            query=query,
            position=position,
            force_play=force_play,
            options=options,
            manual_selection=True,
            server=server,
            manual_bot_choice=manual_bot_choice
        )

    @search.autocomplete("busca")
    async def search_autocomplete(self, inter: disnake.Interaction, current: str):

        if not current:
            return []

        if not self.bot.bot_ready or not self.bot.is_ready() or URL_REG.match(current):
            return [current] if len(current) < 100 else []

        try:
            bot, guild = await check_pool_bots(inter, only_voiced=True)
        except GenericError:
            return [current[:99]]
        except:
            bot = inter.bot

        try:
            if not inter.author.voice:
                return []
        except AttributeError:
            return [current[:99]]

        return await google_search(bot, current)

    @is_dj()
    @has_player()
    @can_send_message_check()
    @commands.max_concurrency(1, commands.BucketType.guild)
    @commands.slash_command(
        extras={"only_voiced": True},
        description=f"{desc_prefix}Me conectar em um canal de voz (ou me mover para um)."
    )
    @commands.contexts(guild=True)
    async def connect(
            self,
            inter: disnake.AppCmdInter,
            channel: Union[disnake.VoiceChannel, disnake.StageChannel] = commands.Param(
                name="canal",
                description="Canal para me conectar"
            )
    ):
        try:
            channel = inter.music_bot.get_channel(channel.id)
        except AttributeError:
            pass

        await self.do_connect(inter, channel)

    async def do_connect(
            self,
            ctx: Union[disnake.AppCmdInter, commands.Context, disnake.Message],
            channel: Union[disnake.VoiceChannel, disnake.StageChannel] = None,
            check_other_bots_in_vc: bool = False,
            bot: BotCore = None,
            me: disnake.Member = None,
    ):

        if not channel:
            try:
                channel = ctx.music_bot.get_channel(ctx.author.voice.channel.id) or ctx.author.voice.channel
            except AttributeError:
                channel = ctx.author.voice.channel

        if not bot:
            try:
                bot = ctx.music_bot
            except AttributeError:
                try:
                    bot = ctx.bot
                except:
                    bot = self.bot

        if not me:
            try:
                me = ctx.music_guild.me
            except AttributeError:
                me = ctx.guild.me

        try:
            guild_id = ctx.guild_id
        except AttributeError:
            guild_id = ctx.guild.id

        try:
            text_channel = ctx.music_bot.get_channel(ctx.channel.id)
        except AttributeError:
            text_channel = ctx.channel

        try:
            player = bot.music.players[guild_id]
        except KeyError:
            print(f"Player debug test 20: {bot.user} | {self.bot.user}")
            raise GenericError(
                f"**O player do bot {bot.user.mention} foi finalizado antes de conectar no canal de voz "
                f"(ou o player não foi inicializado)...\nPor via das dúvidas tente novamente.**"
            )

        can_connect(channel, me.guild, check_other_bots_in_vc=check_other_bots_in_vc, bot=bot)

        deafen_check = True

        if isinstance(ctx, disnake.AppCmdInter) and ctx.application_command.name == self.connect.name:

            perms = channel.permissions_for(me)

            if not perms.connect or not perms.speak:
                raise MissingVoicePerms(channel)

            await player.connect(channel.id, self_deaf=True)

            if channel != me.voice and me.voice.channel:
                txt = [
                    f"me moveu para o canal <#{channel.id}>",
                    f"**Movido com sucesso para o canal** <#{channel.id}>"
                ]

                deafen_check = False


            else:
                txt = [
                    f"me conectou no canal <#{channel.id}>",
                    f"**Conectei no canal** <#{channel.id}>"
                ]

            await self.interaction_message(ctx, txt, emoji="🔈", rpc_update=True)

        else:
            await player.connect(channel.id, self_deaf=True)

        try:
            player.members_timeout_task.cancel()
        except:
            pass

        if deafen_check and bot.config["GUILD_DEAFEN_WARN"]:

            retries = 0

            while retries < 5:

                if me.voice:
                    break

                await asyncio.sleep(1)
                retries += 0

            if not await check_deafen(me):
                await text_channel.send(
                    embed=disnake.Embed(
                        title="Aviso:",
                        description="Para manter sua privacidade e me ajudar a economizar "
                                    "recursos, recomendo desativar meu áudio do canal clicando "
                                    "com botão direito sobre mim e em seguida marcar: desativar "
                                    "áudio no servidor.",
                        color=self.bot.get_color(me),
                    ).set_image(
                        url="https://cdn.discordapp.com/attachments/554468640942981147/1012533546386210956/unknown.png"
                    ), delete_after=20
                )

        if isinstance(channel, disnake.StageChannel):

            stage_perms = channel.permissions_for(me)

            if stage_perms.mute_members:

                retries = 5

                while retries > 0:
                    await asyncio.sleep(1)
                    if not me.voice:
                        retries -= 1
                        continue
                    break
                await asyncio.sleep(1.5)
                await me.edit(suppress=False)
            else:
                embed = disnake.Embed(color=self.bot.get_color(me))

                embed.description = f"**Preciso que algum staff me convide para falar no palco: " \
                                    f"[{channel.name}]({channel.jump_url}).**"

                embed.set_footer(
                    text="💡 Dica: para me permitir falar no palco automaticamente será necessário me conceder "
                         "permissão de silenciar membros (no servidor ou apenas no canal de palco escolhido).")

                await text_channel.send(ctx.author.mention, embed=embed, delete_after=45)

    @can_send_message_check()
    @check_voice()
    @commands.bot_has_guild_permissions(send_messages=True)
    @commands.max_concurrency(1, commands.BucketType.member)
    @pool_command(name="addposition", description="Adicionar música em uma posição especifica da fila.",
                  aliases=["adp", "addpos"], check_player=False, cooldown=play_cd, max_concurrency=play_mc,
                  usage="{prefix}{cmd} [posição(Nº)] [nome|link]\nEx: {prefix}{cmd} 2 sekai - burn me down")
    async def addpos_legacy(self, ctx: CustomContext, position: int, *, query: str):

        if position < 1:
            raise GenericError("**Número da posição da fila tem que ser 1 ou superior.**")

        await self.play.callback(self=self, inter=ctx, query=query, position=position, options=False,
                                 force_play="no", manual_selection=False, server=None)

    stage_flags = CommandArgparse()
    stage_flags.add_argument('query', nargs='*', help="nome ou link da música")
    stage_flags.add_argument('-position', '-pos', '-p', type=int, default=0, help='Colocar a música em uma posição específica da fila (será ignorado caso use -next etc).\nEx: -p 10')
    stage_flags.add_argument('-next', '-proximo', action='store_true', help='Adicionar a música/playlist no topo da fila (equivalente ao: -pos 1)')
    stage_flags.add_argument('-reverse', '-r', action='store_true', help='Inverter a ordem das músicas adicionadas (efetivo apenas ao adicionar playlist).')
    stage_flags.add_argument('-shuffle', '-sl', action='store_true', help='Misturar as músicas adicionadas (efetivo apenas ao adicionar playlist).')
    stage_flags.add_argument('-select', '-s', action='store_true', help='Escolher a música entre os resultados encontrados.')
    stage_flags.add_argument('-mix', '-rec', '-recommended', action="store_true", help="Adicionar/tocar músicas recomendadas com o nome do artsta - música informado.")
    stage_flags.add_argument('-force', '-now', '-n', '-f', action='store_true', help='Tocar a música adicionada imediatamente (efetivo apenas se houver uma música tocando atualmente.)')
    stage_flags.add_argument('-server', '-sv', type=str, default=None, help='Usar um servidor de música específico.')
    stage_flags.add_argument('-selectbot', '-sb', action="store_true", help="Selecionar um bot disponível manualmente.")

    @can_send_message_check()
    @commands.bot_has_guild_permissions(send_messages=True)
    @check_voice()
    @commands.max_concurrency(1, commands.BucketType.member)
    @pool_command(name="play", description="Tocar música em um canal de voz.", aliases=["p"], check_player=False,
                  cooldown=play_cd, max_concurrency=play_mc, extras={"flags": stage_flags},
                  usage="{prefix}{cmd} [nome|link]\nEx: {prefix}{cmd} sekai - burn me down")
    async def play_legacy(self, ctx: CustomContext, *, flags: str = ""):

        args, unknown = ctx.command.extras['flags'].parse_known_args(flags.split())

        await self.play.callback(
            self = self,
            inter = ctx,
            query = " ".join(args.query + unknown),
            position= 1 if args.next else args.position if args.position > 0 else 0,
            options = "shuffle" if args.shuffle else "reversed" if args.reverse else None,
            force_play = "yes" if args.force else "no",
            manual_selection = args.select,
            server = args.server,
            manual_bot_choice = "yes" if args.selectbot else "no",
            mix = args.mix,
        )

    
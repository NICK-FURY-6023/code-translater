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

    @can_send_message_check()
    @commands.bot_has_guild_permissions(send_messages=True)
    @check_voice()
    @pool_command(name="search", description="Pesquisar por músicas e escolher uma entre os resultados para tocar.",
                  aliases=["sc"], check_player=False, cooldown=play_cd, max_concurrency=play_mc,
                  usage="{prefix}{cmd} [nome]\nEx: {prefix}{cmd} sekai - burn me down")
    async def search_legacy(self, ctx: CustomContext, *, query):

        await self.play.callback(self=self, inter=ctx, query=query, position=0, options=False, force_play="no",
                                 manual_selection=True, server=None)

    @can_send_message_check()
    @check_voice()
    @commands.slash_command(
        name="play_music_file",
        description=f"{desc_prefix}Tocar arquivo de música em um canal de voz.",
        extras={"check_player": False}, cooldown=play_cd, max_concurrency=play_mc
    )
    @commands.contexts(guild=True)
    async def play_file(
            self,
            inter: Union[disnake.AppCmdInter, CustomContext],
            file: disnake.Attachment = commands.Param(
                name="arquivo", description="arquivo de audio para tocar ou adicionar na fila"
            ),
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

        class DummyMessage:
            attachments = [file]

        try:
            thread = inter.message.thread
        except:
            thread = None
        inter.message = DummyMessage()
        inter.message.thread = thread

        await self.play.callback(self=self, inter=inter, query="", position=position, options=False, force_play=force_play,
                                 manual_selection=False, server=server,
                                 manual_bot_choice=manual_bot_choice)

    async def check_player_queue(self, user: disnake.User, bot: BotCore, guild_id: int, tracks: Union[list, LavalinkPlaylist] = None):

        count = self.bot.config["QUEUE_MAX_ENTRIES"]

        try:
            player: LavalinkPlayer = bot.music.players[guild_id]
        except KeyError:
            if count < 1:
                return tracks
            count += 1
        else:
            if count < 1:
                return tracks
            if len(player.queue) >= count and not (await bot.is_owner(user)):
                raise GenericError(f"**A fila está cheia ({self.bot.config['QUEUE_MAX_ENTRIES']} músicas).**")

        if tracks:

            if isinstance(tracks, list):
                if not await bot.is_owner(user):
                    tracks = tracks[:count]
            else:
                if not await bot.is_owner(user):
                    tracks.tracks = tracks.tracks[:count]

        return tracks

    @can_send_message_check()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Tocar música em um canal de voz.",
        extras={"check_player": False}, cooldown=play_cd, max_concurrency=play_mc
    )
    @commands.contexts(guild=True)
    async def play(
            self,
            inter: Union[disnake.AppCmdInter, CustomContext],
            query: str = commands.Param(name="busca", desc="Nome ou link da música."), *,
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
            mix: str = commands.Param(
                name="recomendadas",
                description="Tocar músicas recomendadas com base no nome do artista - música informado",
                default=False,
                choices=[
                    disnake.OptionChoice(disnake.Localized("Yes", data={disnake.Locale.pt_BR: "Sim"}), "yes"),
                ]
            ),
            manual_selection: bool = commands.Param(name="selecionar_manualmente",
                                                    description="Escolher uma música manualmente entre os resultados encontrados",
                                                    default=False),
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

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            author = guild.get_member(inter.author.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            author = inter.author

        original_bot = bot

        mix = mix == "yes" or mix is True

        msg = None
        guild_data = await bot.get_data(inter.author.id, db_name=DBModel.guilds)
        ephemeral = None

        if not inter.response.is_done():
            try:
                async with timeout(1.5):
                    ephemeral = await self.is_request_channel(inter, data=guild_data, ignore_thread=True)
            except asyncio.TimeoutError:
                ephemeral = True
            await inter.response.defer(ephemeral=ephemeral, with_message=True)

        """if not inter.author.voice:
            raise NoVoice()

            if not (c for c in guild.channels if c.permissions_for(inter.author).connect):
                raise GenericError(f"**Você não está conectado a um canal de voz, e não há canais de voz/palcos "
                                   "disponíveis no servidor que concedam a permissão para você se conectar.**")

            color = self.bot.get_color(guild.me)

            if isinstance(inter, CustomContext):
                func = inter.send
            else:
                func = inter.edit_original_message

            msg = await func(
                embed=disnake.Embed(
                    description=f"**{inter.author.mention} entre em um canal de voz para tocar sua música.**\n"
                                f"**Caso não conecte em um canal em até 25 segundos essa operação será cancelada.**",
                    color=color
                )
            )

            if msg:
                inter.store_message = msg

            try:
                await bot.wait_for("voice_state_update", timeout=25, check=lambda m, b, a: m.id == inter.author.id and m.voice)
            except asyncio.TimeoutError:
                try:
                    func = msg.edit
                except:
                    func = inter.edit_original_message
                await func(
                    embed=disnake.Embed(
                        description=f"**{inter.author.mention} operação cancelada.**\n"
                                    f"**Você demorou para conectar em um canal de voz/palco.**", color=color
                    )
                )
                return

            await asyncio.sleep(1)

        else:
            channel = bot.get_channel(inter.channel.id)
            if not channel:
                raise GenericError(f"**O canal <#{inter.channel.id}> não foi encontrado (ou foi excluido).**")
            await check_pool_bots(inter, check_player=False, bypass_prefix=True)"""

        if bot.user.id not in author.voice.channel.voice_states:

            if str(inter.channel.id) == guild_data['player_controller']['channel']:

                try:
                    if inter.author.id not in bot.music.players[guild.id].last_channel.voice_states:
                        raise DiffVoiceChannel()
                except (KeyError, AttributeError):
                    pass

            else:

                free_bots = await self.check_available_bot(inter=inter, guild=guild, bot=bot, message=msg)

                if len(free_bots) > 1 and manual_bot_choice == "yes":

                    v = SelectBotVoice(inter, guild, free_bots)

                    try:
                        func = msg.edit
                    except AttributeError:
                        try:
                            func = inter.edit_original_message
                        except AttributeError:
                            func = inter.send

                    newmsg = await func(
                        embed=disnake.Embed(
                            description=f"**Escolha qual bot você deseja usar no canal {author.voice.channel.mention}**",
                            color=self.bot.get_color(guild.me)), view=v
                    )
                    await v.wait()

                    if newmsg:
                        msg = newmsg

                    if v.status is None:
                        try:
                            func = msg.edit
                        except AttributeError:
                            func = inter.edit_original_message
                        try:
                            await func(embed=disnake.Embed(description="### Tempo esgotado...", color=self.bot.get_color(guild.me)), view=None)
                        except:
                            traceback.print_exc()
                        return

                    if v.status is False:
                        try:
                            func = msg.edit
                        except AttributeError:
                            func = inter.edit_original_message
                        await func(embed=disnake.Embed(description="### Operação cancelada.",
                                                       color=self.bot.get_color(guild.me)), view=None)
                        return

                    if not author.voice:
                        try:
                            func = msg.edit
                        except AttributeError:
                            func = inter.edit_original_message
                        await func(embed=disnake.Embed(description="### Você não está conectado em um canal de voz...",
                                                       color=self.bot.get_color(guild.me)), view=None)
                        return

                    update_inter(inter, v.inter)

                    current_bot = v.bot
                    inter = v.inter
                    guild = v.guild

                    await inter.response.defer()

                else:
                    try:
                        current_bot = free_bots.pop(0)
                    except:
                        return

                if bot != current_bot:
                    guild_data = await current_bot.get_data(guild.id, db_name=DBModel.guilds)

                bot = current_bot

        channel = bot.get_channel(inter.channel.id)

        can_send_message(channel, bot.user)

        await check_player_perm(inter=inter, bot=bot, channel=channel, guild_data=guild_data)

        if not guild.voice_client and not check_channel_limit(guild.me, author.voice.channel):
            raise GenericError(f"**O canal {author.voice.channel.mention} está lotado!**")

        await self.check_player_queue(inter.author, bot, guild.id)

        query = query.replace("\n", " ").strip()
        warn_message = None
        queue_loaded = False
        reg_query = None
        image_file = None

        try:
            if isinstance(inter.message, disnake.Message):
                message_inter = inter.message
            else:
                message_inter = None
        except AttributeError:
            message_inter = None

        try:
            modal_message_id = int(inter.data.custom_id[15:])
        except:
            modal_message_id = None

        attachment: Optional[disnake.Attachment] = None

        try:
            voice_channel: disnake.VoiceChannel = bot.get_channel(author.voice.channel.id)
        except AttributeError:
            raise NoVoice()

        try:
            player = bot.music.players[guild.id]

            if not server:
                node = player.node
            else:
                node = bot.music.get_node(server) or player.node

            guild_data = {}

        except KeyError:

            node = bot.music.get_node(server)

            if not node:
                node = await self.get_best_node(bot)

            guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

            if not guild.me.voice:
                can_connect(voice_channel, guild, guild_data["check_other_bots_in_vc"], bot=bot)

            static_player = guild_data['player_controller']

            if not inter.response.is_done():
                ephemeral = await self.is_request_channel(inter, data=guild_data, ignore_thread=True)
                await inter.response.defer(ephemeral=ephemeral)

            if static_player['channel']:
                channel, warn_message, message = await self.check_channel(guild_data, inter, channel, guild, bot)

        if ephemeral is None:
            ephemeral = await self.is_request_channel(inter, data=guild_data, ignore_thread=True)

        is_pin = None

        original_query = query or ""

        if not query:

            if self.bot.config["ENABLE_DISCORD_URLS_PLAYBACK"]:

                try:
                    attachment = inter.message.attachments[0]

                    if attachment.size > 18000000:
                        raise GenericError("**O arquivo que você enviou deve ter o tamanho igual ou inferior a 18mb.**")

                    if attachment.content_type not in self.audio_formats:
                        raise GenericError("**O arquivo que você enviou não é um arquivo de música válido...**")

                    query = attachment.url

                except IndexError:
                    pass

        user_data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        try:
            fav_slashcmd = f"</fav_manager:" + str(self.bot.get_global_command_named("fav_manager",
                                                                                     cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
        except AttributeError:
            fav_slashcmd = "/fav_manager"

        try:
            savequeue_slashcmd = f"</save_queue:" + str(self.bot.get_global_command_named("save_queue",
                                                                                          cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
        except AttributeError:
            savequeue_slashcmd = "/save_queue"

        if not query:

            opts = []

            txt = "### `[⭐] Favoritos [⭐]`\n"

            if user_data["fav_links"]:
                opts.append(disnake.SelectOption(label="Usar favorito", value=">> [⭐ Favoritos ⭐] <<", emoji="⭐"))
                txt += f"`Tocar música ou playlist que você curtiu ou que você tenha adicionado nos seus favoritos.`\n"

            else:
                txt += f"`Você não possui favoritos...`\n"

            txt += f"-# Você pode gerenciar seus favoritos usando o comando {fav_slashcmd}.\n" \
                   f"### `[💠] Integrações [💠]`\n"

            if user_data["integration_links"]:
                opts.append(disnake.SelectOption(label="Usar integração", value=">> [💠 Integrações 💠] <<", emoji="💠"))
                txt += f"`Tocar playlist pública de um canal do youtube (ou de um perfil de usuário de alguma plataforma de música) da sua lista de integrações.`\n"

            else:
                txt += f"`Você não possui integração adicionada... " \
                        f"Use as integrações para adicionar links de canais do youtube (ou link de perfil de algum usuário de alguma plataforma de música) para ter acesso facilita a todas a playlists públicas que o mesmo possui.`\n"

            txt += f"-# Para gerenciar suas integrações use o comando {fav_slashcmd} e em seguida selecione a opção \"integrações\".\n" \
                    f"### `[💾] Fila Salva [💾]`\n"

            if os.path.isfile(f"./local_database/saved_queues_v1/users/{inter.author.id}.pkl"):
                txt += f"`Usar fila de música que você salvou via comando` {savequeue_slashcmd}.\n"
                opts.append(disnake.SelectOption(label="Usar fila salva", value=">> [💾 Fila Salva 💾] <<", emoji="💾"))

            else:
                txt += "`Você não possui uma fila de música salva`\n" \
                        f"-# Pra ter uma fila salva você pode usar o comando {savequeue_slashcmd} quando houver no mínimo 3 músicas adicionadas no player."

            if user_data["last_tracks"]:
                txt += "### `[📑] Músicas recentes [📑]`\n" \
                    "`Tocar uma música que você tenha ouvido/adicionado recentemente.`\n"
                opts.append(disnake.SelectOption(label="Adicionar música recente", value=">> [📑 Músicas recentes 📑] <<", emoji="📑"))
                
            if isinstance(inter, disnake.MessageInteraction) and not inter.response.is_done():
                await inter.response.defer(ephemeral=ephemeral)

            if not guild_data:
                guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

            if guild_data["player_controller"]["fav_links"]:
                txt += "### `[📌] Favoritos do servidor [📌]`\n" \
                        "`Usar favorito do servidor (adicionados por staffs do servidor).`\n"
                opts.append(disnake.SelectOption(label="Usar favorito do servidor", value=">> [📌 Favoritos do servidor 📌] <<", emoji="📌"))

            if not opts:
                raise EmptyFavIntegration()

            embed = disnake.Embed(
                color=self.bot.get_color(guild.me),
                description=f"{txt}## Selecione uma opção abaixo:"
                            f"\n-# Nota: Essa solicitação será cancelada automaticamente <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=180)).timestamp())}:R> caso não seja selecionado uma opção abaixo."
            )

            kwargs = {
                "content": "",
                "embed": embed
            }

            try:
                if inter.message.author.bot:
                    kwargs["content"] = inter.author.mention
            except AttributeError:
                pass

            view = SelectInteraction(user=inter.author, timeout=180, opts=opts)

            try:
                await msg.edit(view=view, **kwargs)
            except AttributeError:
                try:
                    await inter.edit_original_message(view=view, **kwargs)
                except AttributeError:
                    msg = await inter.send(view=view, **kwargs)

            await view.wait()

            select_interaction = view.inter

            try:
                func = inter.edit_original_message
            except AttributeError:
                func = msg.edit

            if not select_interaction or view.selected is False:

                embed.set_footer(text="⚠️ " + ("Tempo de seleção esgotado!" if view.selected is not False else "Cancelado pelo usuário."))

                try:
                    await func(embed=embed, components=song_request_buttons)
                except AttributeError:
                    traceback.print_exc()
                    pass
                return

            
            raise GenericError("**Você não pode usar esse comando com um canal de song-request configurado.**")

        if player.has_thread:
            raise GenericError("**Já há uma thread/conversa ativa no player.**")

        if not isinstance(player.text_channel, disnake.TextChannel):
            raise GenericError("**O player-controller está ativo em um canal incompatível com "
                               "criação de thread/conversa.**")

        if not player.controller_mode:
            raise GenericError("**A skin/aparência atual não é compatível com o sistem de song-request "
                               "via thread/conversa\n\n"
                               "Nota:** `Esse sistema requer uma skin que use botões.`")

        if not player.text_channel.permissions_for(guild.me).send_messages:
            raise GenericError(f"**{bot.user.mention} não possui permissão enviar mensagens no canal {player.text_channel.mention}.**")

        if not player.text_channel.permissions_for(guild.me).create_public_threads:
            raise GenericError(f"**{bot.user.mention} não possui permissão de criar tópicos públicos.**")

        if not [m for m in player.guild.me.voice.channel.members if not m.bot and
                player.text_channel.permissions_for(m).send_messages_in_threads]:
            raise GenericError(f"**Não há membros no canal <#{player.channel_id}> com permissão de enviar mensagens "
                               f"em tópicos no canal {player.text_channel.mention}")

        await inter.response.defer(ephemeral=True)

        thread = await player.message.create_thread(name=f"{bot.user.name} temp. song-request", auto_archive_duration=10080)

        txt = [
            "Ativou o sistema de thread/conversa temporária para pedido de música.",
            f"💬 **⠂{inter.author.mention} criou uma [thread/conversa]({thread.jump_url}) temporária para pedido de música.**"
        ]

        await self.interaction_message(inter, txt, emoji="💬", defered=True, force=True)

    nightcore_cd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.guild)
    nightcore_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(name="nightcore", aliases=["nc"], only_voiced=True, cooldown=nightcore_cd, max_concurrency=nightcore_mc,
                  description="Ativar/Desativar o efeito nightcore (Música acelerada com tom mais agudo).")
    async def nightcore_legacy(self, ctx: CustomContext):

        await self.nightcore.callback(self=self, inter=ctx)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Ativar/Desativar o efeito nightcore (Música acelerada com tom mais agudo).",
        extras={"only_voiced": True}, cooldown=nightcore_cd, max_concurrency=nightcore_mc,
    )
    @commands.contexts(guild=True)
    async def nightcore(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.nightcore = not player.nightcore

        if player.nightcore:
            await player.set_timescale(pitch=1.2, speed=1.1)
            txt = "ativou"
        else:
            await player.set_timescale(enabled=False)
            await player.update_filters()
            txt = "desativou"

        txt = [f"{txt} o efeito nightcore.", f"🇳 **⠂{inter.author.mention} {txt} o efeito nightcore.**"]

        await self.interaction_message(inter, txt, emoji="🇳")


    np_cd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.member)

    @commands.command(name="nowplaying", aliases=["np", "npl", "current", "tocando", "playing"],
                 description="Exibir informações da música que você está ouvindo no momento.", cooldown=np_cd)
    async def now_playing_legacy(self, ctx: CustomContext):
        await self.now_playing.callback(self=self, inter=ctx)

    @commands.slash_command(description=f"{desc_prefix}Exibir info da música que que você está ouvindo (em qualquer servidor).",
                            cooldown=np_cd, extras={"allow_private": True})
    @commands.contexts(guild=True)
    async def now_playing(self, inter: disnake.AppCmdInter):

        player: Optional[LavalinkPlayer] = None

        for bot in self.bot.pool.get_guild_bots(inter.guild_id):

            try:
                p = bot.music.players[inter.guild_id]
            except KeyError:
                continue

            if not p.last_channel:
                continue

            if inter.author.id in p.last_channel.voice_states:
                player = p
                break

        if not player:

            if isinstance(inter, CustomContext) and not (await self.bot.is_owner(inter.author)):

                try:
                    slashcmd = f"</now_playing:" + str(self.bot.get_global_command_named("now_playing",
                                                                                                      cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
                except AttributeError:
                    slashcmd = "/now_playing"

                raise GenericError("**Você deve estar conectado em um canal de voz do servidor atual onde há player ativo...**\n"
                                   f"`Nota: Caso esteja ouvindo em outro servidor você pode usar o comando` {slashcmd}")

            for bot in self.bot.pool.get_guild_bots(inter.guild_id):

                for player_id in bot.music.players:

                    if player_id == inter.guild_id:
                        continue

                    if inter.author.id in (p := bot.music.players[player_id]).last_channel.voice_states:
                        player = p
                        break

        if not player:
            raise GenericError("**Você deve estar conectado em um canal de voz com player ativo...**")

        if not player.current:
            raise GenericError(f"**No momento não estou tocando algo no canal {player.last_channel.mention}**")

        guild_data = await player.bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        ephemeral = (player.guild.id != inter.guild_id and not await player.bot.is_owner(inter.author)) or await self.is_request_channel(inter, data=guild_data)

        url = player.current.uri or player.current.search_uri

        if player.current.info["sourceName"] == "youtube":
            url += f"&t={int(player.position/1000)}s"

        txt = f"### [{player.current.title}](<{url}>)\n"

        footer_kw = {}

        if player.current.is_stream:
            txt += "> 🔴 **⠂Transmissão ao vivo**\n"
        else:
            progress = ProgressBar(
                player.position,
                player.current.duration,
                bar_count=8
            )

            txt += f"```ansi\n[34;1m[{time_format(player.position)}] {('=' * progress.start)}[0m🔴️[36;1m{'-' * progress.end} " \
                   f"[{time_format(player.current.duration)}][0m```\n"

        txt += f"> 👤 **⠂Uploader:** {player.current.authors_md}\n"

        if player.current.album_name:
            txt += f"> 💽 **⠂Álbum:** [`{fix_characters(player.current.album_name, limit=20)}`]({player.current.album_url})\n"

        if not player.current.autoplay:
            txt += f"> ✋ **⠂Solicitado por:** <@{player.current.requester}>\n"
        else:
            try:
                mode = f" [`Recomendação`]({player.current.info['extra']['related']['uri']})"
            except:
                mode = "`Recomendação`"
            txt += f"> 👍 **⠂Adicionado via:** {mode}\n"

        if player.current.playlist_name:
            txt += f"> 📑 **⠂Playlist:** [`{fix_characters(player.current.playlist_name, limit=20)}`]({player.current.playlist_url})\n"

        try:
            txt += f"> *️⃣ **⠂Canal de voz:** {player.guild.me.voice.channel.jump_url}\n"
        except AttributeError:
            pass

        txt += f"> 🔊 **⠂Volume:** `{player.volume}%`\n"

        components = [disnake.ui.Button(custom_id=f"np_{inter.author.id}", label="Atualizar", emoji="🔄")]

        if player.guild_id != inter.guild_id:

            if player.current and not player.paused and (listeners:=len([m for m in player.last_channel.members if not m.bot and (not m.voice.self_deaf or not m.voice.deaf)])) > 1:
                txt += f"> 🎧 **⠂Ouvintes atuais:** `{listeners}`\n"

            txt += f"> ⏱️ **⠂Player ativo:** <t:{player.uptime}:R>\n"

            try:
                footer_kw = {"icon_url": player.guild.icon.with_static_format("png").url}
            except AttributeError:
                pass

            footer_kw["text"] = f"No servidor: {player.guild.name} [ ID: {player.guild.id} ]"

        else:
            try:
                if player.bot.user.id != self.bot.user.id:
                    footer_kw["text"] = f"Bot selecionado: {player.bot.user.display_name}"
                    footer_kw["icon_url"] = player.bot.user.display_avatar.url
            except AttributeError:
                pass

        if player.keep_connected:
            txt += "> ♾️ **⠂Modo 24/7:** `Ativado`\n"

        if player.queue or player.queue_autoplay:

            if player.guild_id == inter.guild_id:

                txt += f"### 🎶 ⠂Próximas músicas ({(qsize := len(player.queue + player.queue_autoplay))}):\n" + (
                            "\n").join(
                    f"> `{n + 1})` [`{fix_characters(t.title, limit=28)}`](<{t.uri}>)\n" \
                    f"> `⏲️ {time_format(t.duration) if not t.is_stream else '🔴 Ao vivo'}`" + (
                        f" - `Repetições: {t.track_loops}`" if t.track_loops else "") + \
                    f" **|** " + (f"`✋` <@{t.requester}>" if not t.autoplay else f"`👍⠂Recomendada`") for n, t in
                    enumerate(itertools.islice(player.queue + player.queue_autoplay, 3))
                )

                if qsize > 3:
                    components.append(
                        disnake.ui.Button(custom_id=PlayerControls.queue, label="Ver lista completa",
                                          emoji="<:music_queue:703761160679194734>"))

            elif player.queue:
                txt += f"> 🎶 **⠂Músicas na fila:** `{len(player.queue)}`\n"

        if player.static and player.guild_id == inter.guild_id:
            if player.message:
                components.append(
                    disnake.ui.Button(url=player.message.jump_url, label="Ir p/ player-controller",
                                      emoji="🔳"))
            elif player.text_channel:
                txt += f"\n\n`Acesse o player-controller no canal:` {player.text_channel.mention}"

        embed = disnake.Embed(description=txt, color=self.bot.get_color(player.guild.me))

        embed.set_author(name=("⠂Tocando agora:" if inter.guild_id == player.guild_id else "Você está ouvindo agora:") if not player.paused else "⠂Música atual:",
                         icon_url=music_source_image(player.current.info["sourceName"]))

        embed.set_thumbnail(url=player.current.thumb)

        if footer_kw:
            embed.set_footer(**footer_kw)

        if isinstance(inter, disnake.MessageInteraction):
            await inter.response.edit_message(inter.author.mention, embed=embed, components=components)
        else:
            await inter.send(inter.author.mention, embed=embed, ephemeral=ephemeral, components=components)

    @commands.Cog.listener("on_button_click")
    async def reload_np(self, inter: disnake.MessageInteraction):

        if not inter.data.custom_id.startswith("np_"):
            return

        if inter.data.custom_id != f"np_{inter.author.id}":
            await inter.send("Você não pode clicar nesse botão...", ephemeral=True)
            return

        try:
            inter.application_command = self.now_playing_legacy
            await check_cmd(self.now_playing_legacy, inter)
            await self.now_playing_legacy(inter)
        except Exception as e:
            self.bot.dispatch('interaction_player_error', inter, e)

    controller_cd = commands.CooldownMapping.from_cooldown(1, 10, commands.BucketType.member)
    controller_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @has_source()
    @check_voice()
    @pool_command(name="controller", aliases=["ctl"], only_voiced=True, cooldown=controller_cd,
                  max_concurrency=controller_mc, description="Enviar player controller para um canal específico/atual.")
    async def controller_legacy(self, ctx: CustomContext):
        await self.controller.callback(self=self, inter=ctx)

    @has_source()
    @check_voice()
    @commands.slash_command(description=f"{desc_prefix}Enviar player controller para um canal específico/atual.",
                            extras={"only_voiced": True}, cooldown=controller_cd, max_concurrency=controller_mc)
    @commands.contexts(guild=True)
    async def controller(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            channel = bot.get_channel(inter.channel.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            channel = inter.channel

        player: LavalinkPlayer = bot.music.players[guild.id]

        if player.static:
            raise GenericError("Esse comando não pode ser usado no modo fixo do player.")

        if player.has_thread:
            raise GenericError("**Esse comando não pode ser usado com uma conversa ativa na "
                               f"[mensagem]({player.message.jump_url}) do player.**")

        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)

        if channel != player.text_channel:

            await is_dj().predicate(inter)

            try:

                player.set_command_log(
                    text=f"{inter.author.mention} moveu o player-controller para o canal {inter.channel.mention}.",
                    emoji="💠"
                )

                embed = disnake.Embed(
                    description=f"💠 **⠂{inter.author.mention} moveu o player-controller para o canal:** {channel.mention}",
                    color=self.bot.get_color(guild.me)
                )

                try:
                    if bot.user.id != self.bot.user.id:
                        embed.set_footer(text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
                except AttributeError:
                    pass

                await player.text_channel.send(embed=embed)

            except:
                pass

        await player.destroy_message()

        player.text_channel = channel

        await player.invoke_np()

        if not isinstance(inter, CustomContext):
            await inter.edit_original_message("**Player reenviado com sucesso!**")

    @is_dj()
    @has_player()
    @check_voice()
    @commands.user_command(name=disnake.Localized("Add DJ", data={disnake.Locale.pt_BR: "Adicionar DJ"}),
                           extras={"only_voiced": True})
    async def adddj_u(self, inter: disnake.UserCommandInteraction):
        await self.add_dj(interaction=inter, user=inter.target)

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="adddj", aliases=["adj"], only_voiced=True,
                  description="Adicionar um membro à lista de DJ's na sessão atual do player.",
                  usage="{prefix}{cmd} [id|nome|@user]\nEx: {prefix}{cmd} @membro")
    async def add_dj_legacy(self, ctx: CustomContext, user: disnake.Member):
        await self.add_dj.callback(self=self, inter=ctx, user=user)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Adicionar um membro à lista de DJ's na sessão atual do player.",
        extras={"only_voiced": True}
    )
    @commands.contexts(guild=True)
    async def add_dj(
            self,
            inter: disnake.AppCmdInter, *,
            user: disnake.User = commands.Param(name="membro", description="Membro a ser adicionado.")
    ):

        error_text = None

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            channel = bot.get_channel(inter.channel.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            channel = inter.channel

        player: LavalinkPlayer = bot.music.players[guild.id]

        user = guild.get_member(user.id)

        if user.bot:
            error_text = "**Você não pode adicionar um bot na lista de DJ's.**"
        elif user == inter.author:
            error_text = "**Você não pode adicionar a si mesmo na lista de DJ's.**"
        elif user.guild_permissions.manage_channels:
            error_text = f"você não pode adicionar o membro {user.mention} na lista de DJ's (ele(a) possui permissão de **gerenciar canais**)."
        elif user.id == player.player_creator:
            error_text = f"**O membro {user.mention} é o criador do player...**"
        elif user.id in player.dj:
            error_text = f"**O membro {user.mention} já está na lista de DJ's**"

        if error_text:
            raise GenericError(error_text)

        player.dj.add(user.id)

        text = [f"adicionou {user.mention} à lista de DJ's.",
                f"🎧 **⠂{inter.author.mention} adicionou {user.mention} na lista de DJ's.**"]

        if (player.static and channel == player.text_channel) or isinstance(inter.application_command,
                                                                            commands.InvokableApplicationCommand):
            await inter.send(f"{user.mention} adicionado à lista de DJ's!{player.controller_link}")

        await self.interaction_message(inter, txt=text, emoji="🎧")

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Remover um membro da lista de DJ's na sessão atual do player.",
        extras={"only_voiced": True}
    )
    @commands.contexts(guild=True)
    async def remove_dj(
            self,
            inter: disnake.AppCmdInter, *,
            user: disnake.User = commands.Param(name="membro", description="Membro a ser adicionado.")
    ):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            channel = bot.get_channel(inter.channel.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            channel = inter.channel

        player: LavalinkPlayer = bot.music.players[guild.id]

        user = guild.get_member(user.id)

        if user.id == player.player_creator:
            if inter.author.guild_permissions.manage_guild:
                player.player_creator = None
            else:
                raise GenericError(f"**O membro {user.mention} é o criador do player.**")

        elif user.id not in player.dj:
            GenericError(f"O membro {user.mention} não está na lista de DJ's")

        else:
            player.dj.remove(user.id)

        text = [f"removeu {user.mention} da lista de DJ's.",
                f"🎧 **⠂{inter.author.mention} removeu {user.mention} da lista de DJ's.**"]

        if (player.static and channel == player.text_channel) or isinstance(inter.application_command,
                                                                            commands.InvokableApplicationCommand):
            await inter.send(f"{user.mention} adicionado à lista de DJ's!{player.controller_link}")

        await self.interaction_message(inter, txt=text, emoji="🎧")

    @has_player()
    @check_voice()
    @pool_command(name="commandlog", aliases=["cmdlog", "clog", "cl"], only_voiced=True,
                  description="Ver o log de uso dos comandos.")
    async def command_log_legacy(self, ctx: CustomContext):
        await self.command_log.callback(self=self, inter=ctx)

    @has_player(check_node=False)
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Ver o log de uso dos comandos.",
        extras={"only_voiced": True}
    )
    @commands.contexts(guild=True)
    async def command_log(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.command_log_list:
            raise GenericError("**O Log de comandos está vazio...**")

        embed = disnake.Embed(
            description="### Log de comandos:\n" + "\n\n".join(f"{i['emoji']} ⠂{i['text']}\n<t:{int(i['timestamp'])}:R>" for i in player.command_log_list),
            color=player.guild.me.color
        )

        if isinstance(inter, CustomContext):
            await inter.reply(embed=embed)
        else:
            await inter.send(embed=embed, ephemeral=True)

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="stop", aliases=["leave", "parar"], only_voiced=True,
                  description="Parar o player e me desconectar do canal de voz.")
    async def stop_legacy(self, ctx: CustomContext):
        await self.stop.callback(self=self, inter=ctx)

    @is_dj()
    @has_player(check_node=False)
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Parar o player e me desconectar do canal de voz.",
        extras={"only_voiced": True}
    )
    @commands.contexts(guild=True)
    async def stop(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            inter_destroy = inter if bot.user.id == self.bot.user.id else None
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            inter_destroy = inter

        player: LavalinkPlayer = bot.music.players[inter.guild_id]
        player.set_command_log(text=f"{inter.author.mention} **parou o player!**", emoji="🛑", controller=True)

        self.bot.pool.song_select_cooldown.get_bucket(inter).update_rate_limit()

        if isinstance(inter, disnake.MessageInteraction):
            await player.destroy(inter=inter_destroy)
        else:

            embed = disnake.Embed(
                color=self.bot.get_color(guild.me),
                description=f"🛑 **⠂{inter.author.mention} parou o player.**"
            )

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(text=f"Bot selecionado: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
            except AttributeError:
                pass

            try:
                ephemeral = player.text_channel.id == inter.channel_id and player.static
            except:
                ephemeral = player.static

            await inter.send(
                embed=embed,
                ephemeral=ephemeral
            )
            await player.destroy()

    @check_queue_loading()
    @has_player()
    @check_voice()
    @pool_command(
        name="savequeue", aliases=["sq", "svq"],
        only_voiced=True, cooldown=queue_manipulation_cd, max_concurrency=remove_mc,
        description="Experimental: Salvar a música e fila atual pra reusá-los a qualquer momento."
    )
    async def savequeue_legacy(self, ctx: CustomContext):
        await self.save_queue.callback(self=self, inter=ctx)

    @check_queue_loading()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Experimental: Salvar a música e fila atual pra reusá-los a qualquer momento.",
        extras={"only_voiced": True}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc
    )
    @commands.contexts(guild=True)
    async def save_queue(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = bot.get_guild(inter.guild_id)

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        tracks = []

        if player.current:
            player.current.info["id"] = player.current.id
            if player.current.playlist:
                player.current.info["playlist"] = {"name": player.current.playlist_name, "url": player.current.playlist_url}
            tracks.append(player.current.info)

        for t in player.queue:
            t.info["id"] = t.id
            if t.playlist:
                t.info["playlist"] = {"name": t.playlist_name, "url": t.playlist_url}
            tracks.append(t.info)

        if len(tracks) < 3:
            raise GenericError(f"**É necessário ter no mínimo 3 músicas pra salvar (atual e/ou na fila)**")

        if not os.path.isdir(f"./local_database/saved_queues_v1/users"):
            os.makedirs(f"./local_database/saved_queues_v1/users")

        async with aiofiles.open(f"./local_database/saved_queues_v1/users/{inter.author.id}.pkl", "wb") as f:
            await f.write(
                zlib.compress(
                    pickle.dumps(
                        {
                            "tracks": tracks, "created_at": disnake.utils.utcnow(), "guild_id": inter.guild_id
                        }
                    )
                )
            )

        await inter.response.defer(ephemeral=True)

        global_data = await self.bot.get_global_data(guild.id, db_name=DBModel.guilds)

        try:
            slashcmd = f"</play:" + str(self.bot.get_global_command_named("play", cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
        except AttributeError:
            slashcmd = "/play"

        embed = disnake.Embed(
            color=bot.get_color(guild.me),
            description=f"### {inter.author.mention}: A fila foi salva com sucesso!!\n"
                        f"**Músicas salvas:** `{len(tracks)}`\n"
                        "### Como usar?\n"
                        f"* Usando o comando {slashcmd} (selecionando no preenchimento automático da busca)\n"
                        "* Clicando no botão/select de tocar favorito/integração do player.\n"
                        f"* Usando o comando {global_data['prefix'] or self.bot.default_prefix}{self.play_legacy.name} "
                        "sem incluir um nome ou link de uma música/vídeo."
        )

        embed.set_footer(text="Nota: Esse é um recurso muito experimental, a fila salva pode sofrer alterações ou ser "
                              "removida em futuros updates")

        if isinstance(inter, CustomContext):
            await inter.reply(embed=embed)
        else:
            await inter.edit_original_response(embed=embed)


    @has_player()
    @check_voice()
    @commands.slash_command(name="queue", extras={"only_voiced": True})
    @commands.contexts(guild=True)
    async def q(self, inter):
        pass

    @is_dj()
   
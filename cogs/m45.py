from __future__ import annotations

import asyncio
import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from .m45_data import BASE_URL, EDITIONS, LATEST

# 大見出し（記事）の別名。キー: 見出しに含まれる文字 → 別名（ゆれ・略称・関連語）
ALIASES: dict[str, str] = {
    "黒点": "黒点 太陽 太陽黒点 sunspot",
    "流星群観測": "流星 流星群 メテオ",
    "東大": "東大 東京大学 本郷 天文学実習 実習",
    "缶サット": "缶サット 缶sat cansat fks-b 缶 ロケット",
    "分光": "分光 分光器 スペクトル fukasis",
    "旅行": "旅行 小旅行 つくば",
    "月食": "月食 皆既月食 皆既",
    "標本": "標本 五無斎 保科 地学標本",
    "諏訪湖": "諏訪湖 御神渡り 結氷 11年周期 太陽活動",
    "とんぼ祭": "とんぼ祭 文化祭 学校祭",
    "新入生歓迎": "新歓 歓迎会 新入生 新入部員",
    "送る会": "送る会 3年生 三年生",
    "立体地形図": "地形図 立体 模型",
    "川から石": "川 石 河原 岩石",
    "鉱石": "鉱石 鉱物 方解石",
    "流星シミュレーター": "シミュレーター シミュ",
    "ホームページ": "ホームページ hp web サイト",
    "キャラクター": "キャラ キャラクター イラスト マスコット",
    "部員名簿": "部員名簿 名簿 おわりに 奥付 最後",
    "最後に": "おわりに あとがき 奥付",
    "はじめに": "はじめに 挨拶 会長",
    "部長挨拶": "挨拶 部長 はじめに",
    "もくじ": "目次 もくじ 索引",
    "彗星": "彗星 紫金山 アトラス",
    "jaxa": "jaxa 対談 清水 宇宙",
    "銀河学校": "銀河学校 木曽 銀河",
    "信州サイエンス": "信州サイエンス サイエンスミーティング SSM",
    "国立科学博物館": "科博 博物館 展示",
    "湯俣": "湯俣 山荘 観望",
    "軌道設計": "軌道 ゲーム 軌道設計ゲーム",
    "一年間": "年間 年間予定 活動",
}

MIN_SCORE = 45

LOGGER = logging.getLogger(__name__)
CACHE_DIR = Path(__file__).parent.parent / "m45_cache"   # PDFとページ画像の保存先
RENDER_DPI = 110                  # ページ画像の解像度（大きいほど鮮明だが重い）
MAX_IMAGE_BYTES = 7_500_000       # Discordの添付上限に収める
_locks: dict[str, asyncio.Lock] = {}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[\W_]+", "", s)


@dataclass
class Entry:
    level: int
    title: str
    page: int
    path: tuple[str, ...]
    names: list[str]       # 照合用（正規化済み）：見出し・別名
    full: str              # 親の見出しを含む正規化済みパス

    @property
    def label(self) -> str:
        return " › ".join(self.path)


def build_index() -> dict[str, list[Entry]]:
    index: dict[str, list[Entry]] = {}
    for key, (_, _, rows) in EDITIONS.items():
        entries: list[Entry] = []
        stack: list[str] = []
        top_alias = ""
        for level, title, page in rows:
            del stack[level - 1:]
            stack.append(title)
            if level == 1:
                top_alias = " ".join(a for k, a in ALIASES.items() if norm(k) in norm(title))
            names = [norm(title)]
            if level == 1:
                names += [norm(a) for a in top_alias.split()]
            entries.append(Entry(level, title, page, tuple(stack), names, norm("".join(stack))))
        index[key] = entries
    return index


INDEX = build_index()


def score(e: Entry, q: str) -> float:
    best = 0.0
    if q == e.full:
        best = 100
    for n in e.names:
        if not n:
            continue
        if q == n:
            best = max(best, 100)
        elif n.startswith(q):
            best = max(best, 90)
        elif q in n:
            best = max(best, 80)
        elif len(n) >= 2 and n in q:
            best = max(best, 75)
        elif len(q) >= 3:
            r = SequenceMatcher(None, q, n).ratio()
            if r >= 0.7:
                best = max(best, 40 + r * 20)
    if best < 50 and len(q) >= 2 and q in e.full:
        best = max(best, 50)   # 親の見出しに含まれる（その記事の中の節）
    if best == 0:
        return 0
    return best - (e.level - 1) * 5


def search(edition: str, keyword: str, limit: int = 5) -> list[tuple[float, Entry]]:
    q = norm(keyword)
    if not q:
        return []
    hits = [(score(e, q), e) for e in INDEX[edition]]
    hits = [h for h in hits if h[0] >= MIN_SCORE]
    hits.sort(key=lambda h: (-h[0], h[1].page))
    out, seen = [], set()
    for s, e in hits:                       # 同じページの重複は1つにまとめる
        if e.page in seen:
            continue
        seen.add(e.page)
        out.append((s, e))
        if len(out) >= limit:
            break
    return out


def pdf_url(edition: str, page: int) -> str:
    return f"{BASE_URL}{EDITIONS[edition][1]}#page={page}"


def link(edition: str, e: Entry, with_path: bool = True) -> str:
    text = e.label if with_path else e.title
    return f"[{text}（p.{e.page}）](<{pdf_url(edition, e.page)}>)"


# ---------- PDFのページを画像にする ----------
async def ensure_pdf(edition: str) -> Path:
    """PDFが無ければダウンロードして保存する（初回のみ。以降は保存したものを使う）。"""
    filename = EDITIONS[edition][1]
    path = CACHE_DIR / filename
    if path.exists():
        return path
    async with _locks.setdefault(filename, asyncio.Lock()):
        if path.exists():
            return path
        CACHE_DIR.mkdir(exist_ok=True)
        tmp = path.with_suffix(".part")
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(BASE_URL + filename) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        f.write(chunk)
        tmp.replace(path)
    return path


def _render(pdf: Path, page: int) -> bytes:
    import pymupdf  # 別スレッドで実行する重い処理

    dpi = RENDER_DPI
    with pymupdf.open(pdf) as doc:
        if not 1 <= page <= doc.page_count:
            raise ValueError(f"page {page} is out of range")
        while True:
            data = doc[page - 1].get_pixmap(dpi=dpi).tobytes("png")
            if len(data) <= MAX_IMAGE_BYTES or dpi <= 50:
                return data
            dpi = int(dpi * 0.75)


async def page_image(edition: str, page: int) -> bytes:
    png = CACHE_DIR / f"{Path(EDITIONS[edition][1]).stem}_p{page}.png"
    if png.exists():
        return png.read_bytes()
    pdf = await ensure_pdf(edition)
    data = await asyncio.to_thread(_render, pdf, page)
    CACHE_DIR.mkdir(exist_ok=True)
    png.write_bytes(data)
    return data


def _page_count(pdf: Path) -> int:
    import pymupdf

    with pymupdf.open(pdf) as doc:
        return doc.page_count


async def page_count(edition: str) -> int:
    return await asyncio.to_thread(_page_count, await ensure_pdf(edition))


class M45PageView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        edition: str,
        name: str,
        description: str | None,
        page: int,
        total_pages: int,
    ):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.edition = edition
        self.name = name
        self.description = description
        self.page = page
        self.total_pages = total_pages
        self._update_buttons()

    def _update_buttons(self):
        self.previous.disabled = self.page <= 1
        self.next.disabled = self.page >= self.total_pages

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"{self.name}（p.{self.page}）"[:256],
            url=pdf_url(self.edition, self.page),
            description=self.description,
            color=discord.Color.blue(),
        )
        embed.set_image(url=f"attachment://m45_{self.edition}_p{self.page}.png")
        embed.set_footer(text="タイトルをクリックするとPDFが開きます｜前後のボタンでページを移動できます")
        return embed

    async def _change_page(self, interaction: discord.Interaction, delta: int):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "このページ操作はコマンドを実行した人だけが使えます。",
                ephemeral=True,
            )
            return
        new_page = self.page + delta
        if not 1 <= new_page <= self.total_pages:
            await interaction.response.send_message("これ以上ページを移動できません。", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            data = await page_image(self.edition, new_page)
        except Exception:
            LOGGER.exception("M45のページ画像を作れませんでした: %s p.%s", self.edition, new_page)
            await interaction.followup.send("ページ画像を作成できませんでした。", ephemeral=True)
            return
        self.page = new_page
        self._update_buttons()
        filename = f"m45_{self.edition}_p{self.page}.png"
        await interaction.edit_original_response(
            embed=self.embed(),
            attachments=[discord.File(io.BytesIO(data), filename=filename)],
            view=self,
        )

    @discord.ui.button(label="前のページ", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, -1)

    @discord.ui.button(label="次のページ", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, 1)


class M45(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="m45", description="部誌M45の該当ページのPDFリンクを送る")
    @app_commands.describe(
        keyword="探したい言葉（例: 黒点 / 流星群 / 缶サット / 分光器）",
        option="どの号か（省略すると最新の 2026-2）",
        preview="ページの画像も表示する（既定: 表示する）",
    )
    @app_commands.choices(
        option=[
            app_commands.Choice(name="2025", value="2025"),
            app_commands.Choice(name="2026", value="2026"),
            app_commands.Choice(name="2026-2（最新）", value="2026-2"),
        ]
    )
    async def m45(
        self,
        interaction: discord.Interaction,
        keyword: app_commands.Range[str, 1, 100],
        option: app_commands.Choice[str] | None = None,
        preview: bool = True,
    ):
        await interaction.response.defer()
        edition = option.value if option else LATEST
        name = EDITIONS[edition][0]
        hits = search(edition, keyword)
        kw = discord.utils.escape_markdown(keyword)

        if hits:
            (_, best), rest = hits[0], [e for _, e in hits[1:]]
            related = "関連：" + " / ".join(link(edition, e) for e in rest[:4]) if rest else ""
            none = discord.AllowedMentions.none()

            data = None
            if preview:
                try:
                    data = await page_image(edition, best.page)
                except Exception:
                    LOGGER.exception("M45のページ画像を作れませんでした: %s p.%s", edition, best.page)
            send = interaction.followup.send

            if data is not None:
                filename = f"m45_{edition}_p{best.page}.png"
                view = M45PageView(
                    interaction.user.id,
                    edition,
                    f"{name}　{best.label}",
                    related or None,
                    best.page,
                    await page_count(edition),
                )
                return await send(
                    embed=view.embed(),
                    file=discord.File(io.BytesIO(data), filename=filename),
                    view=view,
                    allowed_mentions=none,
                )

            lines = [f"📖 **{name}**　{link(edition, best)}"]
            if related:
                lines.append(related)
            if preview:
                lines.append("-# ⚠️ ページ画像は作れませんでした（リンクから開けます）")
            lines.append("-# p. はPDFビューアのページ番号（表紙が1ページ目）")
            return await send("\n".join(lines), allowed_mentions=none)

        # 指定した号になければ、他の号を探す
        lines = [f"「{kw}」は **{name}** に見つかりませんでした。"]
        for other in (LATEST, "2025"):
            if other == edition or (edition == "2026" and other == LATEST):
                continue  # 2026 と 2026-2 は見出しが同じ
            other_hits = search(other, keyword, limit=1)
            if other_hits:
                lines.append(f"📖 **{EDITIONS[other][0]}** にはあります：{link(other, other_hits[0][1])}")
        if len(lines) == 1:
            tops = [e.title for e in INDEX[LATEST] if e.level == 1][:12]
            lines.append("例：" + "、".join(t if len(t) <= 14 else t[:13] + "…" for t in tops))
        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @m45.autocomplete("keyword")
    async def keyword_autocomplete(self, interaction: discord.Interaction, current: str):
        chosen = getattr(interaction.namespace, "option", None)
        edition = chosen if chosen in EDITIONS else LATEST
        if current.strip():
            entries = [e for _, e in search(edition, current, limit=25)]
        else:
            entries = [e for e in INDEX[edition] if e.level == 1]
        choices = []
        for e in entries[:25]:
            value = " ".join(e.path)
            choices.append(
                app_commands.Choice(name=f"{e.label}（p.{e.page}）"[:100], value=value[:100])
            )
        return choices


async def setup(bot: commands.Bot):
    await bot.add_cog(M45(bot))
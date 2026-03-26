# bot.py
import os
import re
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
NEXON_API_KEY = os.getenv("NEXON_API_KEY")
GUILD_ID = os.getenv("GUILD_ID")  # 테스트 서버 ID 있으면 넣기(선택)

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 필요합니다.")
if not NEXON_API_KEY:
    raise RuntimeError("NEXON_API_KEY 환경변수가 필요합니다.")

BASE_URL = "https://open.api.nexon.com"
AUCTION_LIST_URL = f"{BASE_URL}/mabinogi/v1/auction/list"
AUCTION_HISTORY_URL = f"{BASE_URL}/mabinogi/v1/auction/history"
AUCTION_KEYWORD_URL = f"{BASE_URL}/mabinogi/v1/auction/keyword-search"

HEADERS = {
    "x-nxopen-api-key": NEXON_API_KEY
}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


class NexonAPIError(Exception):
    pass


async def fetch_json(url: str, params: dict | None = None) -> dict:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=HEADERS, params=params or {}) as resp:
            if resp.status == 429:
                raise NexonAPIError("넥슨 API 호출 제한에 걸렸어요. 잠시 후 다시 시도해 주세요.")
            if resp.status == 403:
                raise NexonAPIError("API 키 권한을 확인해 주세요.")
            if resp.status == 503:
                raise NexonAPIError("넥슨 API 점검 중일 수 있어요.")
            if resp.status >= 400:
                text = await resp.text()
                raise NexonAPIError(f"API 오류 {resp.status}: {text}")
            return await resp.json()


def pick_first(d: dict, *keys, default=None):
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def extract_rows(data: dict) -> list[dict]:
    """
    응답 구조가 조금 달라도 최대한 견디도록 유연하게 처리
    """
    candidates = [
        "auction_item",
        "auction_items",
        "items",
        "item",
        "data",
        "result",
    ]
    for key in candidates:
        value = data.get(key)
        if isinstance(value, list):
            return value

    # 한 단계 더 들어가 있는 경우
    for key in candidates:
        value = data.get(key)
        if isinstance(value, dict):
            for sub_key in ("auction_item", "items", "list", "rows"):
                sub_val = value.get(sub_key)
                if isinstance(sub_val, list):
                    return sub_val

    return []


def parse_price(row: dict) -> int:
    value = pick_first(
        row,
        "auction_price",
        "price",
        "unit_price",
        "lowest_price",
        default=10**18
    )
    try:
        return int(value)
    except (TypeError, ValueError):
        return 10**18


def parse_count(row: dict) -> int:
    value = pick_first(row, "item_count", "count", "quantity", default=1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def parse_item_name(row: dict) -> str:
    return str(
        pick_first(
            row,
            "item_name",
            "item_display_name",
            "name",
            "display_name",
            default=""
        )
    ).strip()


def parse_category(row: dict) -> str:
    return str(
        pick_first(
            row,
            "item_category",
            "category",
            default=""
        )
    ).strip()


def dedupe_keep_order(names: list[str]) -> list[str]:
    seen = set()
    result = []
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def rgb_match(item_name: str, r: int, g: int, b: int) -> bool:
    patterns = [
        rf"\(\s*{r}\s*,\s*{g}\s*,\s*{b}\s*\)",
        rf"{r}\s*,\s*{g}\s*,\s*{b}",
    ]
    return any(re.search(p, item_name) for p in patterns)


def build_price_embed(item_name: str, rows: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title=f"경매장 검색 결과: {item_name}",
        description="최저가 기준 상위 5건"
    )

    if not rows:
        embed.description = "검색 결과가 없습니다."
        embed.set_footer(text="Data based on NEXON Open API")
        return embed

    for i, row in enumerate(rows[:5], start=1):
        name = parse_item_name(row) or item_name
        price = parse_price(row)
        count = parse_count(row)
        category = parse_category(row)

        lines = [f"가격: **{price:,} 골드**", f"수량: {count}"]
        if category:
            lines.append(f"카테고리: {category}")

        embed.add_field(
            name=f"{i}. {name}",
            value="\n".join(lines),
            inline=False
        )

    embed.set_footer(text="Data based on NEXON Open API")
    return embed


class ItemSelect(discord.ui.Select):
    def __init__(self, item_names: list[str], requester_id: int):
        options = [
            discord.SelectOption(label=name[:100], value=name[:100])
            for name in item_names[:25]
        ]
        super().__init__(
            placeholder="아이템을 선택하세요",
            min_values=1,
            max_values=1,
            options=options
        )
        self.requester_id = requester_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "이 메뉴는 명령을 실행한 사용자만 사용할 수 있어요.",
                ephemeral=True
            )
            return

        selected_name = self.values[0]

        try:
            data = await fetch_json(AUCTION_LIST_URL, {"item_name": selected_name})
            rows = extract_rows(data)
            rows = sorted(rows, key=parse_price)
            embed = build_price_embed(selected_name, rows[:5])
            await interaction.response.edit_message(embed=embed, view=None)
        except NexonAPIError as e:
            await interaction.response.send_message(str(e), ephemeral=True)


class ItemSelectView(discord.ui.View):
    def __init__(self, item_names: list[str], requester_id: int):
        super().__init__(timeout=60)
        self.add_item(ItemSelect(item_names, requester_id))


@bot.event
async def setup_hook():
    # 테스트 서버가 있으면 guild sync가 훨씬 빠름
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"길드 동기화 완료: {GUILD_ID}")
    else:
        await bot.tree.sync()
        print("글로벌 동기화 완료")


@bot.event
async def on_ready():
    print(f"로그인 완료: {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="경매검색", description="일부 단어로 아이템 후보를 찾고 선택 후 최저가 5건을 보여줍니다.")
@app_commands.describe(keyword="예: 카카, 의자, 실크")
async def auction_search(interaction: discord.Interaction, keyword: str):
    await interaction.response.defer()

    try:
        data = await fetch_json(AUCTION_KEYWORD_URL, {"keyword": keyword})
        rows = extract_rows(data)

        names = dedupe_keep_order([parse_item_name(r) for r in rows])

        if not names:
            await interaction.followup.send("해당 검색어가 들어간 아이템을 찾지 못했어요.")
            return

        embed = discord.Embed(
            title=f"'{keyword}' 검색 결과",
            description="아래 목록에서 원하는 아이템을 선택하세요."
        )
        embed.set_footer(text="Data based on NEXON Open API")

        await interaction.followup.send(
            embed=embed,
            view=ItemSelectView(names[:10], interaction.user.id)
        )

    except NexonAPIError as e:
        await interaction.followup.send(str(e))


@bot.tree.command(name="지염검색", description="RGB로 지정 색상 염색 앰플을 찾고 선택 후 최저가 5건을 보여줍니다.")
@app_commands.describe(
    r="Red (0~255)",
    g="Green (0~255)",
    b="Blue (0~255)",
    kind="선택: 천옷, 금속, 원드 등"
)
async def dye_search(
    interaction: discord.Interaction,
    r: app_commands.Range[int, 0, 255],
    g: app_commands.Range[int, 0, 255],
    b: app_commands.Range[int, 0, 255],
    kind: str | None = None,
):
    await interaction.response.defer()

    try:
        keyword = "지정 색상 염색 앰플"
        if kind:
            keyword = f"{kind} {keyword}"

        data = await fetch_json(AUCTION_KEYWORD_URL, {"keyword": keyword})
        rows = extract_rows(data)

        matched = []
        for row in rows:
            name = parse_item_name(row)
            if not name:
                continue
            if "지정 색상 염색 앰플" not in name:
                continue
            if kind and kind not in name:
                continue
            if not rgb_match(name, r, g, b):
                continue
            matched.append(name)

        matched = dedupe_keep_order(matched)

        if not matched:
            await interaction.followup.send(
                f"RGB ({r}, {g}, {b})에 맞는 지정 색상 염색 앰플을 찾지 못했어요."
            )
            return

        embed = discord.Embed(
            title=f"지염 검색 결과: ({r}, {g}, {b})",
            description="아래 목록에서 원하는 아이템을 선택하세요."
        )
        embed.set_footer(text="Data based on NEXON Open API")

        await interaction.followup.send(
            embed=embed,
            view=ItemSelectView(matched[:10], interaction.user.id)
        )

    except NexonAPIError as e:
        await interaction.followup.send(str(e))


bot.run(DISCORD_TOKEN)

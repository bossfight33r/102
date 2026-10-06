"""Генератор JSON-фикстур в формате ответов YouTube Data API v3.

Запуск: uv run python tests/fixtures/generate_fixtures.py
Мир фикстур привязан к NOW = 2026-10-06T06:00:00Z. Заложены аутлайеры:
  tgLONGOUT01 (long, @techguru), tgSHORTOUT1 (short, @techguru), sbBREAKOUT1 (long, @smallbuilder).
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
OUT = Path(__file__).parent
rnd = random.Random(42)


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def channel(cid: str, title: str, handle: str, subs: int | None) -> dict:
    stats = {"viewCount": "1000000", "videoCount": "50", "hiddenSubscriberCount": subs is None}
    if subs is not None:
        stats["subscriberCount"] = str(subs)
    return {
        "kind": "youtube#channel",
        "id": cid,
        "snippet": {"title": title, "customUrl": handle, "description": f"Канал {title}"},
        "statistics": stats,
        "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}},
    }


CHANNELS = [
    channel("UCtechguru00000000000000", "Tech Guru", "@techguru", 120_000),
    channel("UCsmallbuilder0000000000", "Small Builder", "@smallbuilder", 4_000),
    channel("UCcandidate1000000000000", "AI Practice", "@aipractice", 50_000),
    channel("UCbigmedia00000000000000", "Big Media", "@bigmedia", 5_000_000),
    channel("UChiddensubs000000000000", "Hidden Subs", "@hiddensubs", None),
]

videos: list[dict] = []
playlists: dict[str, dict] = {}


def video(
    vid: str, cid: str, title: str, published: datetime, dur: str, views: int, desc: str = ""
) -> None:
    videos.append(
        {
            "kind": "youtube#video",
            "id": vid,
            "snippet": {
                "publishedAt": ts(published),
                "channelId": cid,
                "title": title,
                "description": desc or f"Описание: {title}",
                "tags": ["ai", "нейросети"],
                "thumbnails": {
                    "default": {"url": f"https://i.ytimg.com/vi/{vid}/default.jpg"},
                    "high": {"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"},
                },
            },
            "contentDetails": {"duration": dur},
            "statistics": {
                "viewCount": str(views),
                "likeCount": str(views // 25),
                "commentCount": str(views // 300),
            },
        }
    )
    pl = "UU" + cid[2:]
    playlists.setdefault(pl, {"items": []})["items"].append(
        {
            "kind": "youtube#playlistItem",
            "snippet": {
                "publishedAt": ts(published),
                "resourceId": {"kind": "youtube#video", "videoId": vid},
            },
            "contentDetails": {"videoId": vid, "videoPublishedAt": ts(published)},
        }
    )


def views_for(base: float, age_days: float, sigma: float = 0.25) -> int:
    mature = base * rnd.lognormvariate(0, sigma)
    frac = min(1.0, 0.35 + 0.65 * age_days / 7) if age_days < 7 else 1.0
    return int(mature * frac)


CHAPTERS = "0:00 Вступление\n1:30 Настройка\n5:10 Пример\n9:45 Итоги"

# @techguru: 24 long + 10 shorts, по одному аутлайеру в каждом формате
tg = "UCtechguru00000000000000"
for i in range(24):
    pub = NOW - timedelta(days=1 + i * 2.5, hours=3)
    age = (NOW - pub).total_seconds() / 86400
    video(
        f"tgL{i:03d}aaaaa",
        tg,
        f"Разбор AI-инструмента #{i}",
        pub,
        f"PT{rnd.randint(10, 24)}M{rnd.randint(0, 59)}S",
        views_for(20_000, age),
        CHAPTERS if i % 3 == 0 else "",
    )
video(
    "tgLONGOUT01",
    tg,
    "7 нейросетей, которые заменили мне ассистента (честный тест)",
    NOW - timedelta(days=10),
    "PT14M5S",
    260_000,
    CHAPTERS + "\nСсылки в описании",
)
for i in range(10):
    pub = NOW - timedelta(days=2 + i * 5)
    age = (NOW - pub).total_seconds() / 86400
    video(
        f"tgS{i:03d}aaaaa",
        tg,
        f"Быстрый AI-трюк #{i}",
        pub,
        f"PT{rnd.randint(20, 59)}S",
        views_for(6_000, age),
    )
video(
    "tgSHORTOUT1", tg, "Этот промпт экономит час в день", NOW - timedelta(days=8), "PT41S", 90_000
)

# @smallbuilder: 14 long, прорыв маленького канала
sb = "UCsmallbuilder0000000000"
for i in range(14):
    pub = NOW - timedelta(days=2 + i * 4)
    age = (NOW - pub).total_seconds() / 86400
    video(
        f"sbL{i:03d}aaaaa",
        sb,
        f"Собираю продукт с AI, день {i}",
        pub,
        f"PT{rnd.randint(8, 20)}M",
        views_for(1_500, age),
    )
video(
    "sbBREAKOUT1",
    sb,
    "Как я сделал SaaS за выходные без программиста",
    NOW - timedelta(days=12),
    "PT18M20S",
    45_000,
)

# кандидат: несколько видео
cand = "UCcandidate1000000000000"
for i in range(5):
    video(
        f"apL{i:03d}aaaaa",
        cand,
        f"AI на практике {i}",
        NOW - timedelta(days=3 + i * 6),
        "PT11M",
        views_for(8_000, 3 + i * 6),
    )
for cid, n in (("UCbigmedia00000000000000", 2), ("UChiddensubs000000000000", 1)):
    for i in range(n):
        video(
            f"{cid[2:5]}V{i:03d}aaaa",
            cid,
            f"Видео {cid[2:8]} {i}",
            NOW - timedelta(days=5 + i),
            "PT9M",
            500_000,
        )


def hit(vid: str, cid: str, title: str, days_ago: float) -> dict:
    return {
        "kind": "youtube#searchResult",
        "id": {"kind": "youtube#video", "videoId": vid},
        "snippet": {
            "publishedAt": ts(NOW - timedelta(days=days_ago)),
            "channelId": cid,
            "title": title,
        },
    }


SEARCH = {
    "нейросети для работы": {
        "items": [
            hit("apL000aaaaa", cand, "AI на практике 0", 3),
            hit("apL001aaaaa", cand, "AI на практике 1", 9),
            hit("bigV000aaaa", "UCbigmedia00000000000000", "Видео bigmed 0", 5),
            hit("tgLONGOUT01", tg, "7 нейросетей", 10),
            hit("oldOLDOLD01", cand, "Старое видео", 400),
        ]
    },
    "ai инструменты": {
        "items": [
            hit("apL002aaaaa", cand, "AI на практике 2", 15),
            hit("sbBREAKOUT1", sb, "SaaS за выходные", 12),
            hit("hidV000aaaa", "UChiddensubs000000000000", "Видео hidden 0", 5),
        ]
    },
}


def comment(text: str, likes: int) -> dict:
    return {
        "kind": "youtube#commentThread",
        "snippet": {
            "topLevelComment": {
                "snippet": {"textOriginal": text, "likeCount": likes, "authorDisplayName": "viewer"}
            }
        },
    }


COMMENTS = {
    "tgLONGOUT01": {
        "items": [
            comment("А какая из них работает бесплатно?", 340),
            comment("Можно отдельное видео про настройку на Mac?", 210),
            comment("Наконец-то честный тест без рекламы", 150),
            comment("Как это связать с Google Таблицами?", 90),
            comment("Сколько в итоге экономите времени в неделю?", 40),
        ]
    },
    "tgSHORTOUT1": {
        "items": [
            comment("Скинь сам промпт текстом", 500),
            comment("Работает ли на русском?", 120),
        ]
    },
    "sbBREAKOUT1": {
        "items": [
            comment("Сколько это стоило в месяц?", 80),
            comment("Покажи, как ты делал оплату", 60),
        ]
    },
}


def dump(name: str, data: object) -> None:
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


dump("channels.json", {"kind": "youtube#channelListResponse", "items": CHANNELS})
dump("videos.json", {"kind": "youtube#videoListResponse", "items": videos})
dump("playlist_items.json", playlists)
dump("search.json", SEARCH)
dump("comment_threads.json", COMMENTS)
print(f"channels={len(CHANNELS)} videos={len(videos)} playlists={len(playlists)}")

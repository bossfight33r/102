"""Кнопки карточек. callback_data: fb:<action>:<video_id> (≤ 64 байт)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from radar.schemas import Button, FeedbackAction

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup

ACTION_CODES = {
    FeedbackAction.TO_TOPICS: "t",
    FeedbackAction.DETAILS: "d",
    FeedbackAction.HIDE_CHANNEL: "h",
    FeedbackAction.NOT_RELEVANT: "n",
}
CODE_ACTIONS = {v: k for k, v in ACTION_CODES.items()}


def feedback_buttons(video_id: str) -> list[list[Button]]:
    def b(text: str, action: FeedbackAction) -> Button:
        return Button(text=text, callback_data=f"fb:{ACTION_CODES[action]}:{video_id}")

    return [
        [b("📌 В темы", FeedbackAction.TO_TOPICS), b("🔎 Подробнее", FeedbackAction.DETAILS)],
        [
            b("🙈 Скрыть канал", FeedbackAction.HIDE_CHANNEL),
            b("👎 Не то", FeedbackAction.NOT_RELEVANT),
        ],
    ]


def candidate_buttons(channel_id: str) -> list[list[Button]]:
    return [
        [
            Button(text="✅ Следить", callback_data=f"ch:a:{channel_id}"),
            Button(text="🙈 Скрыть", callback_data=f"ch:h:{channel_id}"),
        ]
    ]


def parse_callback(data: str) -> tuple[str, str, str] | None:
    """'fb:t:VIDEO' -> ('fb', 't', 'VIDEO')."""
    parts = data.split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def to_markup(buttons: list[list[Button]]) -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup  # aiogram — только боту

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.text, callback_data=b.callback_data) for b in row]
            for row in buttons
        ]
    )

"""Выбор размера фото из ``Message.photo`` (список ``PhotoSize`` от Telegram).

Telegram отдаёт одно и то же фото в нескольких размерах, от меньшего к большему.
Полный размер (последний) нужен для просмотра, а для превью в списках хватает
~320px — грузить полный скриншот ради плитки в админке значит платить лишней
секундой за каждую.
"""

from collections.abc import Sequence
from typing import Any


THUMBNAIL_MIN_WIDTH = 320


def pick_thumbnail_photo_size(sizes: Sequence[Any], min_width: int = THUMBNAIL_MIN_WIDTH) -> Any | None:
    """Наименьший размер шириной не меньше ``min_width``; если таких нет — самый большой.

    «Самый большой» — последний элемент, как и везде в проекте (``photo[-1]``).
    """
    if not sizes:
        return None
    wide_enough = [size for size in sizes if (getattr(size, 'width', 0) or 0) >= min_width]
    if wide_enough:
        return min(wide_enough, key=lambda size: size.width)
    return sizes[-1]

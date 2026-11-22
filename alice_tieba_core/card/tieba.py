"""贴吧帖子布局：主楼正文、大图和首屏楼层回复在同一张卡片中顺排。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import blocks as blk
from . import surface
from .palette import alpha, mix


@dataclass
class Segment:
    kind: str
    height: int
    data: Any = None


class TiebaPostBlock(blk.Block):
    """可测量的专用帖子页面，不借用其他站点的操作栏或封面布局。"""

    @staticmethod
    def _size(ctx: Any, value: float) -> int:
        return max(9, round(value * ctx.m.scale))

    @classmethod
    def _font(cls, ctx: Any, value: float, bold: bool = False) -> Any:
        return ctx.font(cls._size(ctx, value), bold=bold)

    @staticmethod
    def _forum(ctx: Any) -> str:
        name = ctx.model.forum_name
        return name if name.endswith("吧") else name + "吧" if name else "贴吧"

    @classmethod
    def _body(cls, ctx: Any) -> str:
        body = ctx.model.body
        # 兼容通用皮肤中的贴吧来源前缀；本皮肤已在顶部单独呈现吧名。
        prefix = f"贴吧：{cls._forum(ctx)}"
        if ctx.model.forum_name and body.startswith(prefix):
            body = body[len(prefix):].lstrip("\n")
        return body

    @classmethod
    def _plan(cls, ctx: Any, width: int) -> list[Segment]:
        scale = ctx.m.scale
        gap = max(12, round(22 * scale))
        plan = [Segment("header", round(54 * scale))]
        if ctx.model.has_author:
            plan.extend([Segment("gap", gap), Segment("author", round(72 * scale))])
        if ctx.model.title:
            font = cls._font(ctx, 34 * ctx.layout.headline_scale, True)
            lines = ctx.ts.fit(ctx.model.title, font, width, 4)
            plan.extend([Segment("gap", gap), Segment("title", ctx.ts.paragraph_height(lines, font, 1.12), (lines, font))])
        body = cls._body(ctx)
        if body:
            font = cls._font(ctx, 24)
            maximum = 12 if ctx.layout.key == "feed" else 24
            all_lines = ctx.ts.wrap(body, font, width)
            lines = ctx.ts.fit(body, font, width, maximum)
            plan.extend([Segment("gap", gap), Segment("body", ctx.ts.paragraph_height(lines, font, 1.34), (lines, font))])
            if len(all_lines) > maximum:
                plan.append(Segment("notice", round(35 * scale), "正文较长，完整内容请查看原帖"))
        if ctx.model.media:
            plan.append(Segment("gap", gap))
            for index, item in enumerate(ctx.model.media):
                natural_h = max(1, round(width / max(0.05, item.aspect)))
                media_h = min(natural_h, round(width * 2.2 * ctx.layout.media_scale))
                plan.append(Segment("image", media_h, (item, natural_h == media_h)))
                if index < len(ctx.model.media) - 1:
                    plan.append(Segment("gap", max(6, round(10 * scale))))
        if ctx.model.stats:
            font = cls._font(ctx, 18)
            text = "  ·  ".join(f"{value} {label}" if value else label for label, value in ctx.model.stats)
            lines = ctx.ts.wrap(text, font, width)
            plan.extend([Segment("gap", gap), Segment("stats", ctx.ts.paragraph_height(lines, font, 1.2) + round(16 * scale), (lines, font))])
        if ctx.model.warnings:
            font = cls._font(ctx, 17)
            for warning in ctx.model.warnings:
                lines = ctx.ts.wrap(warning, font, width - round(24 * scale))
                plan.extend([Segment("gap", round(12 * scale)), Segment("warning", ctx.ts.paragraph_height(lines, font, 1.3) + round(22 * scale), (lines, font))])
        if ctx.model.comments:
            plan.extend([Segment("gap", gap), Segment("comments_header", round(70 * scale))])
            avatar = round(42 * scale)
            text_w = width - avatar - round(14 * scale)
            for comment in ctx.model.comments:
                font = cls._font(ctx, 22)
                lines = ctx.ts.fit(comment.message, font, text_w, 8)
                height = round(34 * scale) + ctx.ts.paragraph_height(lines, font, 1.28)
                if comment.time:
                    height += round(31 * scale)
                plan.append(Segment("comment", height, (comment, lines, font)))
                plan.append(Segment("gap", gap))
        if ctx.model.url or ctx.model.watermark:
            font = cls._font(ctx, 14)
            lines = ctx.ts.wrap(ctx.model.url, font, width) if ctx.model.url else []
            height = ctx.ts.paragraph_height(lines, font, 1.18) + round(24 * scale)
            if ctx.model.watermark:
                height += round(26 * scale)
            plan.extend([Segment("gap", round(14 * scale)), Segment("footer", height, (lines, font))])
        return plan

    def _measure(self, ctx: Any, width: int) -> int:
        return sum(row.height for row in self._plan(ctx, width))

    @classmethod
    def _draw_header(cls, ctx: Any, layer: Any, x: int, y: int, width: int, height: int) -> None:
        font = cls._font(ctx, 26, True)
        caption = cls._font(ctx, 16)
        label = ctx.ts.ellipsize(cls._forum(ctx), font, max(100, width - round(110 * ctx.m.scale)))
        pill_w = min(width, ctx.ts.width(label, font) + round(34 * ctx.m.scale))
        fill = mix(ctx.panel_bg, ctx.accent, 0.045 if not ctx.pal.is_dark else 0.12)
        surface.panel(layer, (x, y, x + pill_w, y + height), height // 2, fill=alpha(fill, 255))
        top = y + (height - ctx.ts.line_height(font, 1.0)) // 2
        ctx.text(layer, (x + round(17 * ctx.m.scale), top), label, font, ctx.ink, bold=True)
        caption_w = ctx.ts.width("帖子", caption)
        ctx.text(layer, (x + width - caption_w, y + (height - ctx.ts.line_height(caption, 1.0)) // 2), "帖子", caption, ctx.ink_muted)

    @classmethod
    def _draw_author(cls, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        size = round(64 * ctx.m.scale)
        blk._comment_avatar(ctx, layer, x, y, size, ctx.model.author_name, ctx.model.avatar)
        cursor = x + size + round(16 * ctx.m.scale)
        font = cls._font(ctx, 24)
        badge_font = cls._font(ctx, 15)
        badge_text = "楼主"
        badge_w = ctx.ts.width(badge_text, badge_font) + round(16 * ctx.m.scale)
        level = ctx.model.author_level
        level_text = f"Lv.{level}" if level and level.isdecimal() else level
        level_w = ctx.ts.width(level_text, badge_font) + round(16 * ctx.m.scale) if level_text else 0
        name = ctx.ts.ellipsize(ctx.model.author_name, font, width - (cursor - x) - badge_w - level_w - round(20 * ctx.m.scale))
        cursor += ctx.text(layer, (cursor, y), name, font, ctx.ink)
        cursor += round(10 * ctx.m.scale)
        badge_h = round(25 * ctx.m.scale)
        blk._chip(ctx, layer, cursor, y + round(4 * ctx.m.scale), badge_text, fill=None, ink=ctx.accent_text,
                  border=alpha(ctx.accent, 150), font=badge_font, height=badge_h, radius=round(5 * ctx.m.scale), pad_x=round(8 * ctx.m.scale))
        if level_text:
            blk._chip(ctx, layer, cursor + badge_w + round(8 * ctx.m.scale), y + round(4 * ctx.m.scale), level_text,
                      fill=alpha(mix(ctx.panel_bg, (255, 153, 91), 0.24), 255), ink=ctx.ink_dim,
                      font=badge_font, height=badge_h, radius=round(5 * ctx.m.scale), pad_x=round(8 * ctx.m.scale))
        meta = "  ".join(value for value in (ctx.model.time_text, ctx.model.ip_location) if value)
        if meta:
            meta_font = cls._font(ctx, 17)
            text = ctx.ts.ellipsize(meta, meta_font, width - size - round(16 * ctx.m.scale))
            ctx.text(layer, (x + size + round(16 * ctx.m.scale), y + round(40 * ctx.m.scale)), text, meta_font, ctx.ink_muted)

    def draw(self, ctx: Any, layer: Any, x: int, y: int, width: int) -> None:
        scale = ctx.m.scale
        for row in self._plan(ctx, width):
            kind = row.kind
            if kind == "header":
                self._draw_header(ctx, layer, x, y, width, row.height)
            elif kind == "author":
                self._draw_author(ctx, layer, x, y, width)
            elif kind in {"title", "body", "stats"}:
                lines, font = row.data
                if kind == "stats":
                    surface.hairline(layer, x, y + row.height - round(3 * scale), x + width, ctx.hair)
                ctx.para(layer, (x, y), lines, font, ctx.ink_dim if kind == "stats" else ctx.ink,
                         leading={"title": 1.12, "body": 1.34, "stats": 1.2}[kind], bold=kind == "title")
            elif kind == "notice":
                font = self._font(ctx, 16)
                ctx.text(layer, (x, y + round(6 * scale)), row.data, font, ctx.accent_text)
            elif kind == "image":
                item, natural = row.data
                if natural:
                    tile = blk._tile_image(ctx, item, width, row.height)
                else:
                    try:
                        tile = surface.contain_fit(surface.open_image(item.path), width, row.height, alpha(ctx.pal.media_mat, 255))
                    except (OSError, ValueError):
                        tile = None
                if tile is not None:
                    tile = surface.round_image(tile, max(5, round(12 * scale)))
                    layer.alpha_composite(tile, (x, y))
                    if item.is_video and ctx.show_play_button:
                        blk._play_badge(ctx, layer, (x, y, x + width, y + row.height))
            elif kind == "warning":
                lines, font = row.data
                surface.panel(layer, (x, y, x + width, y + row.height), round(8 * scale), fill=alpha(ctx.warn, 15))
                ctx.para(layer, (x + round(12 * scale), y + round(11 * scale)), lines, font, ctx.warn, leading=1.3)
            elif kind == "comments_header":
                surface.panel(layer, (x, y, x + width, y + max(5, round(9 * scale))), 0, fill=alpha(ctx.pal.hairline, 255))
                font = self._font(ctx, 24, True)
                title = ctx.model.comment_section_title or "楼层回复"
                ctx.text(layer, (x, y + round(27 * scale)), title, font, ctx.ink, bold=True)
                note_font = self._font(ctx, 15)
                note = "首屏预览"
                ctx.text(layer, (x + width - ctx.ts.width(note, note_font), y + round(33 * scale)), note, note_font, ctx.ink_muted)
            elif kind == "comment":
                comment, lines, font = row.data
                avatar = round(42 * scale)
                blk._comment_avatar(ctx, layer, x, y, avatar, comment.username, comment.avatar)
                text_x = x + avatar + round(14 * scale)
                name_font = self._font(ctx, 19)
                likes = f"{comment.likes} 赞" if comment.likes and comment.likes != "未知" else ""
                likes_font = self._font(ctx, 16)
                likes_w = ctx.ts.width(likes, likes_font) if likes else 0
                name = ctx.ts.ellipsize(comment.username, name_font, width - (text_x - x) - likes_w - round(12 * scale))
                ctx.text(layer, (text_x, y), name, name_font, ctx.ink_dim)
                if likes:
                    ctx.text(layer, (x + width - likes_w, y + round(2 * scale)), likes, likes_font, ctx.ink_muted)
                body_y = y + round(34 * scale)
                body_h = ctx.para(layer, (text_x, body_y), lines, font, ctx.ink, leading=1.28)
                if comment.time:
                    meta_font = self._font(ctx, 16)
                    meta = ctx.ts.ellipsize(comment.time, meta_font, width - (text_x - x))
                    ctx.text(layer, (text_x, body_y + body_h + round(6 * scale)), meta, meta_font, ctx.ink_muted)
            elif kind == "footer":
                lines, font = row.data
                surface.hairline(layer, x, y, x + width, ctx.hair)
                cursor = y + round(14 * scale)
                if lines:
                    cursor += ctx.para(layer, (x, cursor), lines, font, ctx.accent_text, leading=1.18)
                if ctx.model.watermark:
                    text = ctx.ts.ellipsize(ctx.model.watermark, font, width)
                    ctx.text(layer, (x, cursor + round(10 * scale)), text, font, ctx.ink_muted)
            y += row.height

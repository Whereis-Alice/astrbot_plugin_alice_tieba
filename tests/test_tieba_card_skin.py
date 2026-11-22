"""贴吧专用卡片的来源、内容保真和真实界面风格。"""

import pytest
from PIL import Image

from alice_tieba_core.card import build_model, resolve_theme_key, resolve_theme_key_for_platform
from alice_tieba_core.rika_render.adapter import build_parse_result
from test_card_skins import make_result, render_probe


def post_model(tmp_path, *, body="主楼正文\n\n第二段正文", images=1):
    files = []
    for index in range(images):
        path = tmp_path / f"post-{index}.png"
        Image.new("RGB", (600, 900 if index == 0 else 400), (80 + index * 12, 160, 220)).save(path)
        files.append(path)
    result = make_result(
        platform="tieba", display_name="贴吧", author=("楼主名字", ""),
        title="我们需要更多好看的帖子卡片", text=body,
        url="https://tieba.baidu.com/p/123456789",
        extra={
            "forum_name": "星之卡比", "stats_line": "💬 21 👍 23 ⭐ 2",
            "comment_section_title": "楼层回复",
            "hot_comments": [
                {"username": "真实回帖用户", "message": "这是一条真实的首屏楼层回复。", "likes": None,
                 "time": "2026-11-22 13:30", "position_text": "第 2 楼"},
            ],
        },
    )
    return build_model(result, {"grid": files}, watermark="爱丽丝的贴吧工具")


def test_tieba_auto_and_chinese_aliases_resolve_to_dedicated_skin():
    assert resolve_theme_key_for_platform("auto", "tieba") == "tieba"
    assert resolve_theme_key("贴吧") == "tieba"
    assert resolve_theme_key("贴吧帖子") == "tieba"
    assert resolve_theme_key("哔哩哔哩") == "bilibili"


@pytest.mark.parametrize("width", [520, 800, 1080])
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_post_skin_has_real_forum_main_post_and_floor_preview(tmp_path, width, mode):
    model = post_model(tmp_path)
    probe = render_probe(model, width=width, mode=mode, theme_key="auto")
    assert probe.ctx.theme.key == "tieba"
    assert "星之卡比吧" in probe.joined_text
    assert "楼主名字" in probe.joined_text and "楼主" in probe.joined_text
    assert "我们需要更多好看的帖子卡片" in "".join(probe.texts)
    assert "首屏预览" in probe.joined_text and "第 2 楼" in probe.joined_text
    assert "未知" not in probe.joined_text
    assert not any(label in probe.joined_text for label in ("投币", "哔哩哔哩", "弹幕", "bilibili"))
    assert max(probe.text_bottoms) < probe.image.height - 16
    assert probe.image.width == width
    assert len(probe.tiles) == 1
    image_w, image_h = probe.tiles[0][1:]
    assert image_h > image_w, "主楼竖图应保持竖向大图，不能裁成视频封面"
    if mode == "light":
        assert probe.image.getpixel((0, 0)) == (255, 255, 255)


def test_long_body_has_explicit_notice_and_keeps_the_first_paragraph(tmp_path):
    model = post_model(tmp_path, body="开头内容\n\n" + "较长的主楼正文" * 1000, images=0)
    probe = render_probe(model, theme_key="tieba", mode="light")
    assert "开头内容" in probe.joined_text
    assert "正文较长，完整内容请查看原帖" in probe.joined_text
    assert probe.image.height < 3000


def test_multiple_images_are_visible_without_bilibili_thumbnail_grid(tmp_path):
    model = post_model(tmp_path, images=3)
    probe = render_probe(model, theme_key="tieba", mode="light")
    tiles = [(width, height) for _, width, height in probe.tiles]
    assert len(tiles) == 3
    assert len({width for width, _ in tiles}) == 1
    assert tiles[0][1] > tiles[1][1]


def test_adapter_keeps_tieba_forum_separate_and_does_not_invent_badges():
    result = build_parse_result({
        "platform": "tieba", "title": "帖子标题", "author": "楼主名字",
        "desc": "原文正文", "forum_name": "星之卡比",
    })
    model = build_model(result, {})
    assert model.forum_name == "星之卡比"
    assert model.author_level == model.ip_location == ""
    probe = render_probe(model, theme_key="tieba", mode="light")
    assert "星之卡比吧" in probe.joined_text
    assert "原文正文" in probe.joined_text
    assert "贴吧：星之卡比吧" not in probe.joined_text
    assert "Lv." not in probe.joined_text


def test_config_visibility_hides_the_forum_with_author():
    result = build_parse_result({
        "platform": "tieba", "title": "帖子标题", "author": "楼主名字",
        "desc": "原文正文", "forum_name": "星之卡比",
        "_text_metadata_fields": {"author": False},
    })
    model = build_model(result, {})
    assert model.forum_name == ""
    assert "星之卡比" not in render_probe(model, theme_key="tieba").joined_text

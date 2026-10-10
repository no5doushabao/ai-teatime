#!/usr/bin/env python3
"""给"AI 讨论会"生成像素头像（64x64 PNG）。

⚠️⚠️ **注意：这个脚本会重画全部头像** —— 包括 ayu / gpt / claude / grok / qwen 那五个
     **手绘立绘裁切版**。跑完它之后，必须再跑一次 `import_avatars.py` 把立绘版恢复回来
     （立绘的脸存在 `avatars-src/` 里）。2026-10-10 我就这么覆盖过一次。

设计：**统一的"圆底 + 白色符号"**，靠颜色和符号区分，风格才不裂
（跟元素图标 / 木桩假人同一套做法：硬边、少色、深色描边）。

⚠️ 用 DSH 自带的 Python 跑（它带 Pillow）；插件的 venv **没有 Pillow**。
    python gen_avatars.py
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 64
OUT = Path(__file__).resolve().parent / "static" / "avatars"   # ⚠️ 必须落在 static/ 下，
# 否则 http.server 的静态根（static/）找不到它 → 前端拿到 404（这个坑踩过一次）
DARK = (28, 26, 34)

# who, 底色, 亮部, 符号色, 符号画法
PEOPLE = [
    ("ayu",    (86, 150, 214), (140, 200, 245), (245, 250, 255)),   # 蓝发鲸鱼娘
    ("grok",   (40, 40, 46),   (108, 108, 122), (235, 235, 240)),   # 黑底 + X
    ("qwen",   (128, 96, 200), (176, 150, 235), (250, 246, 255)),   # 紫 + 扇
    ("claude", (214, 122, 92), (240, 170, 140), (255, 248, 240)),   # 橙 + 星芒
    ("gpt",    (28, 140, 112), (86, 190, 160), (240, 255, 250)),    # 绿 + 花环
    ("glm",    (58, 108, 186), (108, 156, 226), (245, 250, 255)),   # 蓝 + 文档（智谱）
    ("gemini", (124, 108, 214), (168, 156, 240), (250, 250, 255)),  # 紫蓝 + 四角星（Google）
    ("sf",     (32, 158, 168), (92, 205, 210), (245, 255, 255)),    # 青 + 芯片（硅基流动）
    ("boss",   (196, 158, 92), (232, 202, 140), (255, 252, 245)),   # ⭐ 阿包（旁听席）：茶色 + 茶杯
]


def new_mask():
    m = Image.new("L", (S, S), 0)
    return m, ImageDraw.Draw(m)


def layer(img, mask, rgb):
    img.paste(Image.new("RGBA", (S, S), rgb + (255,)), (0, 0), mask)


def draw_symbol(who: str, sym, dsym):
    """白色符号：每个人一个可辨识的几何形状（不描边，靠对比度）。"""
    c = (S - 1) / 2.0
    if who == "ayu":
        # 鲸鱼尾巴（两片尾鳍）+ 一只眼睛
        dsym.polygon([(c, c + 12), (c - 14, c - 6), (c - 4, c - 2)], fill=255)
        dsym.polygon([(c, c + 12), (c + 14, c - 6), (c + 4, c - 2)], fill=255)
        dsym.ellipse([c - 4, c + 2, c + 4, c + 10], fill=255)
        dsym.ellipse([c - 13, c - 10, c - 7, c - 4], fill=255)
    elif who == "grok":
        # X
        dsym.polygon([(c - 18, c - 18), (c - 11, c - 18), (c + 18, c + 18), (c + 11, c + 18)], fill=255)
        dsym.polygon([(c + 18, c - 18), (c + 11, c - 18), (c - 18, c + 18), (c - 11, c + 18)], fill=255)
    elif who == "qwen":
        # 折扇
        dsym.pieslice([c - 22, c - 4, c + 22, c + 40], 180, 360, fill=255)
        dsym.polygon([(c - 2, c + 16), (c + 2, c + 16), (c + 4, c + 26), (c - 4, c + 26)], fill=255)
        dsym.line([(c - 22, c + 16), (c + 22, c + 16)], fill=0, width=2)
    elif who == "claude":
        # 星芒（八道）
        import math
        for i in range(8):
            a = math.radians(i * 45)
            dsym.polygon([(c + 20 * math.cos(a), c + 20 * math.sin(a)),
                          (c + 6 * math.cos(a + 0.35), c + 6 * math.sin(a + 0.35)),
                          (c + 6 * math.cos(a - 0.35), c + 6 * math.sin(a - 0.35))], fill=255)
    elif who == "gpt":
        # 花环（六个圆点围一圈 + 中心空心）
        import math
        for i in range(6):
            a = math.radians(i * 60 - 90)
            x, y = c + 15 * math.cos(a), c + 15 * math.sin(a)
            dsym.ellipse([x - 6, y - 6, x + 6, y + 6], fill=255)
        dsym.ellipse([c - 7, c - 7, c + 7, c + 7], fill=0)
    elif who == "glm":
        # 文档（一页纸 + 折角 + 三行字）—— 智谱
        dsym.polygon([(c - 12, c - 16), (c + 7, c - 16), (c + 15, c - 8),
                      (c + 15, c + 16), (c - 12, c + 16)], fill=255)
        dsym.polygon([(c + 7, c - 16), (c + 15, c - 8), (c + 7, c - 8)], fill=0)
        for i in range(3):
            dsym.line([(c - 6, c - 2 + i * 7), (c + 9, c - 2 + i * 7)], fill=0, width=2)
    elif who == "gemini":
        # ⭐ 四角星（Gemini 的标志）—— Google
        import math
        for i in range(4):
            a = math.radians(i * 90)
            dsym.polygon([(c + 21 * math.cos(a), c + 21 * math.sin(a)),
                          (c + 5 * math.cos(a + 0.55), c + 5 * math.sin(a + 0.55)),
                          (c + 5 * math.cos(a - 0.55), c + 5 * math.sin(a - 0.55))], fill=255)
        dsym.ellipse([c - 3, c - 3, c + 3, c + 3], fill=255)
    elif who == "sf":
        # 芯片（方块 + 八根引脚）—— 硅基流动
        dsym.rectangle([c - 9, c - 9, c + 9, c + 9], fill=255)
        dsym.rectangle([c - 4, c - 4, c + 4, c + 4], fill=0)
        for i in (-6, 0, 6):
            dsym.line([(c + i, c - 9), (c + i, c - 16)], fill=255, width=2)
            dsym.line([(c + i, c + 9), (c + i, c + 16)], fill=255, width=2)
            dsym.line([(c - 9, c + i), (c - 16, c + i)], fill=255, width=2)
            dsym.line([(c + 9, c + i), (c + 16, c + i)], fill=255, width=2)
    elif who == "boss":
        # ⭐ 阿包（旁听席）：茶杯 —— 茶话会的主人，不是嘉宾
        for dx, dy in ((-6, 0), (0, -2), (6, 0)):                       # 热气
            dsym.line([(c + dx, c - 16 + dy), (c + dx - 2, c - 8 + dy)], fill=255, width=2)
        dsym.rounded_rectangle([c - 14, c - 4, c + 10, c + 12], radius=2, fill=255)  # 杯身
        dsym.rectangle([c - 10, c, c + 6, c + 8], fill=0)               # 杯内空
        dsym.arc([c + 7, c - 2, c + 20, c + 11], 270, 90, fill=255, width=3)         # 把手
        dsym.rectangle([c - 18, c + 14, c + 16, c + 17], fill=255)      # 茶碟


def build(who: str, base, light, sym_rgb) -> Image.Image:
    body, db = new_mask()
    light_m, dl = new_mask()
    sym, dsym = new_mask()

    db.ellipse([4, 4, S - 5, S - 5], fill=255)              # 圆底
    dl.ellipse([9, 9, S - 14, S - 22], fill=255)            # 左上高光
    draw_symbol(who, sym, dsym)

    solid = ImageChops.lighter(body, sym)
    outline = ImageChops.subtract(solid.filter(ImageFilter.MaxFilter(3)), solid)

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for mask, rgb in ((outline, DARK), (body, base), (light_m, light), (sym, sym_rgb)):
        layer(img, mask, rgb)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for who, base, light, sym_rgb in PEOPLE:
        im = build(who, base, light, sym_rgb)
        p = OUT / f"{who}.png"
        im.save(p)
        print(f"  {p.name}  {S}x{S}")


if __name__ == "__main__":
    main()

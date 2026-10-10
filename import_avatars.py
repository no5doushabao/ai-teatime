#!/usr/bin/env python3
"""把阿包丢进来的图片转成茶话会头像（64x64 圆形 PNG）。

用法（三步）：
    1. 把图丢进 avatars-src/，**文件名 = 嘉宾代号**（ayu / grok / qwen / claude / gpt / glm / sf / boss）
       支持 png / jpg / jpeg / webp / bmp，任意尺寸
    2. python import_avatars.py
    3. 刷新茶话会窗口（头像就换了）

做什么：
    居中裁成正方形 → 缩放到 64 → 圆形蒙版 → 加和现有风格一致的深色描边
    （这样"手绘大图"和"代码生成的像素头像"能混着用，风格不会裂）

⚠️ 用 DSH 的 Python 跑（它带 Pillow）；插件的 venv 没有 Pillow。
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PIL import Image, ImageDraw, ImageFilter

S = 64                              # 输出尺寸（和 gen_avatars.py 一致）
DARK = (28, 26, 34)
SRC = Path(__file__).resolve().parent / "avatars-src"
OUT = Path(__file__).resolve().parent / "static" / "avatars"
EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def center_square(im: Image.Image, top_bias: float = 0.08) -> Image.Image:
    """裁成正方形。

    ⚠️ 不平分：人像的"内容"通常在上半部分（脸在上边），居中裁容易裁到胸口/身子。
       top_bias=0 顶对齐 · 0.08 稍偏上（默认，适合头像）· 0.5 才是正中。
    """
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = int((h - side) * top_bias) if h > side else 0
    return im.crop((left, top, left + side, top + side))


def build(im: Image.Image) -> Image.Image:
    # 先放大到 4 倍再缩回来：抗锯齿更干净（大图缩到 64 容易糊成一团）
    big = center_square(im.convert("RGBA")).resize((S * 4, S * 4), Image.LANCZOS)
    body = Image.new("L", (S * 4, S * 4), 0)
    ImageDraw.Draw(body).ellipse([0, 0, S * 4 - 1, S * 4 - 1], fill=255)
    outline = ImageChops_subtract(body.filter(ImageFilter.MaxFilter(7)), body)

    canvas = Image.new("RGBA", (S * 4, S * 4), (0, 0, 0, 0))
    canvas.paste(Image.new("RGBA", (S * 4, S * 4), DARK + (255,)), (0, 0), outline)
    canvas.paste(big, (0, 0), body)
    return canvas.resize((S, S), Image.LANCZOS)


def ImageChops_subtract(a, b):
    from PIL import ImageChops
    return ImageChops.subtract(a, b)


def main() -> None:
    if not SRC.exists():
        SRC.mkdir(parents=True, exist_ok=True)
        print(f"建好了素材目录：{SRC}")
        print("把图丢进去（文件名 = 代号，例如 grok.png），再跑一次这个脚本。")
        return

    files = [p for p in sorted(SRC.iterdir()) if p.suffix.lower() in EXTS]
    if not files:
        print(f"{SRC} 里还没有图片。文件名要用代号：ayu / grok / qwen / claude / gpt / glm / sf / boss")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    for f in files:
        who = f.stem.lower()
        try:
            im = Image.open(f)
            im.load()
        except Exception as e:
            print(f"  ✗ {f.name}：读不了（{e}）")
            continue
        out = build(im)
        dst = OUT / f"{who}.png"
        out.save(dst)
        print(f"  ✓ {f.name}  {im.size[0]}x{im.size[1]} → {dst.name}  {S}x{S}")

    print(f"\n完成 {len(files)} 张。刷新茶话会窗口就能看到新头像。")


if __name__ == "__main__":
    main()

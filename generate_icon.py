"""Generate a sleek multi-resolution Windows .ico for Smart IPTV Player Pro."""

import math
import os
from PIL import Image, ImageDraw


def make_icon(out_path: str):
    size = 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Squircle dark-navy background with gradient simulation
    margin = 44
    radius = 210
    steps = 40
    for i in range(steps):
        t = i / (steps - 1)
        r = int(11 + t * (22 - 11))
        g = int(17 + t * (36 - 17))
        b = int(32 + t * (61 - 32))
        inset = int(t * (size // 2 - margin - 40))
        x0 = margin + inset // 2
        y0 = margin + inset // 3
        x1 = size - margin - inset // 2
        y1 = size - margin - inset // 2
        if x1 > x0 and y1 > y0:
            draw.rounded_rectangle([x0, y0, x1, y1], radius=max(30, radius - inset // 3), fill=(r, g, b, 255))

    # Glowing teal outer border
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius,
        outline=(0, 210, 196, 235),
        width=24
    )
    draw.rounded_rectangle(
        [margin + 14, margin + 14, size - margin - 14, size - margin - 14],
        radius=radius - 12,
        outline=(56, 189, 248, 95),
        width=8
    )

    # Broadcast / IPTV signal arcs above the screen
    cx = size // 2
    cy_arc = 290
    arc_specs = [
        (170, (56, 189, 248, 220), 22),
        (115, (0, 229, 212, 245), 22),
        (60, (224, 247, 250, 255), 20),
    ]
    for rad, color, w in arc_specs:
        draw.arc([cx - rad, cy_arc - rad, cx + rad, cy_arc + rad], start=225, end=315, fill=color, width=w)

    # Small glowing signal origin dot
    draw.ellipse([cx - 20, cy_arc - 20, cx + 20, cy_arc + 20], fill=(0, 229, 212, 255))

    # TV / Screen bezel
    sx0, sy0, sx1, sy1 = 165, 315, 859, 760
    draw.rounded_rectangle([sx0, sy0, sx1, sy1], radius=68, fill=(10, 22, 42, 255), outline=(0, 229, 212, 255), width=26)
    # Inner subtle cyan screen glow
    draw.rounded_rectangle([sx0 + 26, sy0 + 26, sx1 - 26, sy1 - 26], radius=46, fill=(15, 31, 58, 255), outline=(2, 132, 199, 150), width=6)

    # Minimalist TV stand
    draw.rounded_rectangle([cx - 42, sy1 - 4, cx + 42, 825], radius=12, fill=(0, 210, 196, 255))
    draw.rounded_rectangle([cx - 165, 815, cx + 165, 854], radius=19, fill=(56, 189, 248, 255))

    # Crisp Play triangle centered inside the TV screen
    scr_cy = (sy0 + sy1) // 2
    play_r = 128
    p_x_offset = 18
    tri = [
        (cx - int(play_r * 0.68) + p_x_offset, scr_cy - int(play_r * 0.85)),
        (cx - int(play_r * 0.68) + p_x_offset, scr_cy + int(play_r * 0.85)),
        (cx + int(play_r * 0.92) + p_x_offset, scr_cy),
    ]
    draw.polygon(tri, fill=(248, 250, 252, 255))

    # Coral-red LIVE indicator dot at top-right inside screen
    lx, ly = sx1 - 78, sy0 + 74
    draw.ellipse([lx - 26, ly - 26, lx + 26, ly + 26], fill=(239, 68, 68, 255))
    draw.ellipse([lx - 11, ly - 11, lx + 11, ly + 11], fill=(254, 202, 202, 230))

    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    img.save(out_path, format="ICO", sizes=sizes)


if __name__ == "__main__":
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
    make_icon(target)
    print(f"Generated {target} ({os.path.getsize(target)} bytes)")

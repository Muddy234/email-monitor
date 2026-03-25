"""Generate Clarion AI extension icons at 16, 48, 128px.

Draws the envelope+green-dot logo on a rounded-corner blue background.
Uses only Pillow (no cairosvg dependency).
"""

from PIL import Image, ImageDraw
import math


def draw_icon(size: int) -> Image.Image:
    """Draw the Clarion logo at the given pixel size."""
    # Use 4x supersampling for antialiasing
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background: rounded rect with brand gradient matching promo tiles
    # linear-gradient(135deg, #1E3A5F 0%, #2563EB 60%, #3B82F6 100%)
    corner_r = int(s * 0.18)
    # Draw gradient manually: iterate rows and interpolate colors
    for y in range(s):
        for x in range(s):
            # Project (x, y) onto the 135-degree gradient axis
            t = ((x + y) / (2 * s))  # normalized 0..1 along 135deg
            t = max(0.0, min(1.0, t))
            if t < 0.6:
                # Interpolate #1E3A5F → #2563EB
                f = t / 0.6
                r = int(30 + (37 - 30) * f)
                g = int(58 + (99 - 58) * f)
                b = int(95 + (235 - 95) * f)
            else:
                # Interpolate #2563EB → #3B82F6
                f = (t - 0.6) / 0.4
                r = int(37 + (59 - 37) * f)
                g = int(99 + (130 - 99) * f)
                b = int(235 + (246 - 235) * f)
            img.putpixel((x, y), (r, g, b, 255))
    # Apply rounded corners by masking
    mask = Image.new("L", (s, s), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=corner_r, fill=255)
    img.putalpha(mask)

    # --- Envelope ---
    # Envelope body proportions (relative to canvas)
    margin_x = s * 0.15
    margin_top = s * 0.25
    margin_bottom = s * 0.22
    ex1 = margin_x
    ey1 = margin_top
    ex2 = s - margin_x
    ey2 = s - margin_bottom
    env_w = ex2 - ex1
    env_h = ey2 - ey1

    stroke_w = max(1, int(s * 0.045))
    white = (255, 255, 255, 255)

    # Envelope body (rounded rect)
    env_r = int(s * 0.06)
    draw.rounded_rectangle(
        [ex1, ey1, ex2, ey2], radius=env_r, outline=white, width=stroke_w
    )

    # Envelope flap (V-shape from top-left to center to top-right)
    flap_cx = (ex1 + ex2) / 2
    flap_cy = ey1 + env_h * 0.55  # flap dips to ~55% of envelope height
    flap_stroke = stroke_w

    # Draw flap as two lines
    for t in range(flap_stroke):
        offset = t - flap_stroke // 2
        draw.line(
            [(ex1 + stroke_w, ey1 + stroke_w * 0.8 + offset),
             (flap_cx, flap_cy + offset)],
            fill=white, width=1
        )
        draw.line(
            [(flap_cx, flap_cy + offset),
             (ex2 - stroke_w, ey1 + stroke_w * 0.8 + offset)],
            fill=white, width=1
        )

    # Thicker flap lines for better visibility
    draw.line(
        [(ex1 + stroke_w, ey1 + stroke_w * 0.5),
         (flap_cx, flap_cy)],
        fill=white, width=stroke_w
    )
    draw.line(
        [(flap_cx, flap_cy),
         (ex2 - stroke_w, ey1 + stroke_w * 0.5)],
        fill=white, width=stroke_w
    )

    # --- Green notification dot ---
    green = (16, 185, 129, 255)  # #10B981
    dot_r = s * 0.10
    dot_cx = ex2 - env_w * 0.05
    dot_cy = ey1 - env_h * 0.02

    # Outer ring (pulsing effect)
    ring_r = dot_r * 1.6
    ring_color = (16, 185, 129, 80)
    draw.ellipse(
        [dot_cx - ring_r, dot_cy - ring_r, dot_cx + ring_r, dot_cy + ring_r],
        fill=ring_color
    )

    # Solid green dot
    draw.ellipse(
        [dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
        fill=green
    )

    # Downsample with high-quality resampling
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    from pathlib import Path

    out_dir = Path(__file__).parent.parent / "extension" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    for sz in (16, 48, 128):
        icon = draw_icon(sz)
        path = out_dir / f"icon{sz}.png"
        icon.save(path, "PNG")
        print(f"Saved {path} ({sz}x{sz})")

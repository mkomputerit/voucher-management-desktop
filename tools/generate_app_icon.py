"""Generate the original Voucher Management application icon.

The icon is intentionally built from simple neutral geometric primitives:
overlapping voucher tickets plus a confirmation badge. It contains no vendor,
venue or deployment branding and is reproducible from source.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _draw_ticket_icon(size: int = 1024) -> Image.Image:
    scale = size / 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    def box(values):
        return tuple(round(value * scale) for value in values)

    def width(value: int) -> int:
        return max(1, round(value * scale))

    def ticket_layer(bounds, fill, outline, angle=0):
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.rounded_rectangle(
            box(bounds),
            radius=round(76 * scale),
            fill=fill,
            outline=outline,
            width=width(18),
        )

        left, top, right, bottom = bounds
        notch_radius = round(38 * scale)
        for x in (left, right):
            for y in (top + 170, bottom - 170):
                cx, cy = round(x * scale), round(y * scale)
                draw.ellipse(
                    (
                        cx - notch_radius,
                        cy - notch_radius,
                        cx + notch_radius,
                        cy + notch_radius,
                    ),
                    fill=(0, 0, 0, 0),
                )

        # Neutral voucher lines, not text.
        line_y = top + 270
        for line_width in (320, 250, 290):
            draw.rounded_rectangle(
                box((left + 150, line_y, left + 150 + line_width, line_y + 24)),
                radius=round(12 * scale),
                fill=(115, 154, 185, 120),
            )
            line_y += 82

        if angle:
            layer = layer.rotate(
                angle,
                resample=Image.Resampling.BICUBIC,
                center=(size // 2, size // 2),
            )
        image.alpha_composite(layer)

    ticket_layer(
        (190, 135, 790, 720),
        (232, 246, 255, 255),
        (51, 137, 204, 255),
        angle=-8,
    )
    ticket_layer(
        (235, 185, 835, 770),
        (246, 251, 255, 255),
        (37, 111, 179, 255),
        angle=4,
    )

    # Confirmation badge.
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        box((585, 545, 905, 865)),
        fill=(36, 174, 93, 255),
        outline=(255, 255, 255, 255),
        width=width(22),
    )
    draw.line(
        [box((655, 705))[0:2], box((720, 770))[0:2], box((835, 635))[0:2]],
        fill=(255, 255, 255, 255),
        width=width(42),
        joint="curve",
    )

    return image


def _draw_small_ticket_icon(size: int) -> Image.Image:
    """Render a simplified, higher-contrast icon for 16/24 px Windows surfaces."""

    if size not in {16, 24}:
        raise ValueError("small icon artwork is defined only for 16 and 24 px")

    # Render a size-specific simplified composition at modest supersampling.
    # This is deliberately not a downscale of the 1024 px master: tiny Windows
    # surfaces need thicker outlines, darker blues and fewer internal details.
    ss = 8
    canvas = size * ss
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def p(value: float) -> int:
        return round(value * canvas)

    outline = (12, 72, 128, 255)
    rear_fill = (174, 215, 244, 255)
    front_fill = (212, 235, 250, 255)
    line_fill = (24, 91, 148, 255)
    green = (24, 153, 78, 255)

    outline_width = max(ss, round(canvas * 0.065))
    radius = round(canvas * 0.08)

    # Rear ticket.
    draw.rounded_rectangle(
        (p(0.12), p(0.12), p(0.72), p(0.68)),
        radius=radius,
        fill=rear_fill,
        outline=outline,
        width=outline_width,
    )

    # Front ticket, kept axis-aligned for crisp tiny-size silhouettes.
    draw.rounded_rectangle(
        (p(0.22), p(0.23), p(0.82), p(0.77)),
        radius=radius,
        fill=front_fill,
        outline=outline,
        width=outline_width,
    )

    # One strong voucher line instead of the three fine master-art lines.
    line_height = max(ss, round(canvas * 0.055))
    draw.rounded_rectangle(
        (p(0.34), p(0.43), p(0.65), p(0.43) + line_height),
        radius=max(1, line_height // 2),
        fill=line_fill,
    )

    # Confirmation badge.
    draw.ellipse(
        (p(0.49), p(0.47), p(0.98), p(0.96)),
        fill=green,
        outline=(255, 255, 255, 255),
        width=max(ss, round(canvas * 0.03)),
    )
    check_width = max(ss * 2, round(canvas * 0.082))
    draw.line(
        [
            (p(0.62), p(0.73)),
            (p(0.71), p(0.82)),
            (p(0.89), p(0.61)),
        ],
        fill=(255, 255, 255, 255),
        width=check_width,
        joint="curve",
    )

    return image.resize((size, size), Image.Resampling.LANCZOS)


def ensure_app_icon(path: Path, *, preview_png: Path | None = None) -> Path:
    """Create the ICO if needed and return its path."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    source = _draw_ticket_icon()
    frames = []
    for value in ICON_SIZES:
        if value in {16, 24}:
            frame = _draw_small_ticket_icon(value)
        else:
            frame = source.resize((value, value), Image.Resampling.LANCZOS)
        frames.append(frame)

    # Supply an exact frame for every requested size. Pillow otherwise falls
    # back to scaling a single source image; 16/24 px intentionally use their
    # dedicated high-contrast artwork instead.
    source.save(
        path,
        format="ICO",
        sizes=[(value, value) for value in ICON_SIZES],
        append_images=frames,
    )

    if preview_png is not None:
        preview_png = Path(preview_png)
        preview_png.parent.mkdir(parents=True, exist_ok=True)
        source.save(preview_png, format="PNG")

    return path


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    target = root / ".generated-assets" / "VoucherManagement.ico"
    preview = root / ".generated-assets" / "VoucherManagement.png"
    print(ensure_app_icon(target, preview_png=preview))

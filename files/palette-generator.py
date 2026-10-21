#!/usr/bin/env python3
"""Generate a Noctalia palette from a wallpaper.

Writes a palette file that Noctalia reads as a custom palette, with a `dark` and
a `light` variant. Noctalia picks the variant that matches the current theme
mode, so one file serves both modes and a mode change needs no regeneration.

The palette contract is Noctalia's community palette format. Each mode needs
sixteen colour roles and a terminal block. The terminal block is mandatory: a
palette without one fails to parse and Noctalia falls back to a built-in.

Usage:
    palette-generator.py --wallpaper IMAGE --output FILE [options]

Options:
    --variant duotone|tonal   How the two modes relate to the image.
                              duotone (default) keeps the image's real hues and
                              makes light washed out and dark deep.
                              tonal uses one seed hue the way Noctalia's own
                              schemes do.
    --seed N                  Seed for the random generator. The same seed and
                              the same image always give the same palette.
    --random                  Apply a random rotation and shuffle to the
                              accent roles, anchored on the image's hue.
    --contrast RATIO          Minimum contrast for text pairs. Default 4.5.
    --print                   Print the palette instead of writing it.
    --check                   Report the contrast of every pair and exit.

The script never writes a palette that fails its own contrast check.
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import os
import random
import sys

TEXT_CONTRAST = 4.5
UI_CONTRAST = 3.0

# The eight terminal colours, and the hue each one keeps by convention.
ANSI_HUES = {
    "red": 8,
    "green": 130,
    "yellow": 48,
    "blue": 218,
    "magenta": 305,
    "cyan": 186,
}


# --------------------------------------------------------------------- colour

def hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"not a hex colour: {value!r}")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def rgb_to_hsl(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    h, l, s = colorsys.rgb_to_hls(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    return (h * 360.0, s, l)


def hsl_to_rgb(h: float, s: float, l: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb((h % 360.0) / 360.0, l, s)
    return (round(r * 255), round(g * 255), round(b * 255))


def hsl_hex(h: float, s: float, l: float) -> str:
    return rgb_to_hex(hsl_to_rgb(h, s, l))


def _channel(value: float) -> float:
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_channel(c / 255.0) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    a, b = luminance(first), luminance(second)
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


def contrast_of(first: str, second: str) -> float:
    return contrast_ratio(hex_to_rgb(first), hex_to_rgb(second))


def ensure_contrast(foreground: str, background: str, target: float) -> str:
    """Move a colour's lightness away from the background until it passes.

    Only the lightness moves, so the hue of the colour is preserved. Both
    directions are tried, because a mid tone background can be unreachable by
    going only one way: white on a mid brown is about 3 to 1, and darkening
    that same brown is the way to pass. The direction that changes the
    lightness least wins, so the colour stays as close to the original as the
    contrast rule allows.
    """
    fg = hex_to_rgb(foreground)
    bg = hex_to_rgb(background)
    if contrast_ratio(fg, bg) >= target:
        return foreground

    h, s, l = rgb_to_hsl(fg)

    def walk(direction: float) -> str | None:
        for step in range(1, 101):
            moved = max(0.0, min(1.0, l + direction * 0.01 * step))
            candidate = hsl_hex(h, s, moved)
            if contrast_ratio(hex_to_rgb(candidate), bg) >= target:
                return candidate
        return None

    darker = walk(-1.0)
    lighter = walk(1.0)
    if darker and lighter:
        # Keep whichever moved the lightness least, so the colour stays as close
        # to the original as the contrast rule allows.
        darker_move = abs(rgb_to_hsl(hex_to_rgb(darker))[2] - l)
        lighter_move = abs(rgb_to_hsl(hex_to_rgb(lighter))[2] - l)
        return darker if darker_move <= lighter_move else lighter
    if darker:
        return darker
    if lighter:
        return lighter

    # Nothing in the lightness range works, which happens for a mid tone
    # background. Black or white always works, so use the better of the two.
    black = hsl_hex(h, 0.0, 0.0)
    white = hsl_hex(h, 0.0, 1.0)
    black_ratio = contrast_ratio(hex_to_rgb(black), bg)
    white_ratio = contrast_ratio(hex_to_rgb(white), bg)
    return black if black_ratio >= white_ratio else white


# ----------------------------------------------------------------- extraction

def dominant_colors(path: str, count: int = 10) -> list[tuple[tuple[int, int, int], int]]:
    """The most common colours in an image, most common first."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow is a hard requirement
        raise SystemExit(f"Pillow is required to read a wallpaper: {exc}")

    with Image.open(path) as opened:
        image = opened.convert("RGB")
        image = image.resize((128, 128), Image.Resampling.LANCZOS)
        quantised = image.quantize(colors=count, method=Image.Quantize.MEDIANCUT)
        palette = quantised.getpalette()
        counts = sorted(quantised.getcolors(), reverse=True)
    out = []
    for pixels, index in counts:
        out.append((tuple(palette[index * 3:index * 3 + 3]), pixels))
    return out


def usable_hues(colors) -> list[tuple[float, float, float]]:
    """The colours of an image that can carry a hue, best first.

    Near-black, near-white and grey pixels are dropped. A grey pixel has no
    hue to preserve, so including one would flatten the palette.
    """
    scored = []
    for rgb, pixels in colors:
        h, s, l = rgb_to_hsl(rgb)
        if s < 0.15 or l < 0.10 or l > 0.94:
            continue
        scored.append((s * pixels, h, s, l))
    scored.sort(reverse=True)

    chosen: list[tuple[float, float, float]] = []
    for _weight, h, s, l in scored:
        if any(_hue_distance(h, other[0]) < 35.0 for other in chosen):
            continue
        chosen.append((h, s, l))
        if len(chosen) == 3:
            break

    if not chosen:
        # A grey image. Fall back to a neutral seed so the palette still builds.
        chosen.append((210.0, 0.25, 0.5))
    while len(chosen) < 3:
        chosen.append((chosen[0][0], chosen[0][1], chosen[0][2]))
    return chosen


def _hue_distance(first: float, second: float) -> float:
    diff = abs(first - second) % 360.0
    return min(diff, 360.0 - diff)


# -------------------------------------------------------------------- palette

def _roles(
    primary: str,
    on_primary: str,
    secondary: str,
    on_secondary: str,
    tertiary: str,
    on_tertiary: str,
    surface: str,
    on_surface: str,
    surface_variant: str,
    on_surface_variant: str,
    error: str,
    on_error: str,
    outline: str,
    shadow: str,
) -> dict[str, str]:
    return {
        "mPrimary": primary,
        "mOnPrimary": on_primary,
        "mSecondary": secondary,
        "mOnSecondary": on_secondary,
        "mTertiary": tertiary,
        "mOnTertiary": on_tertiary,
        "mError": error,
        "mOnError": on_error,
        "mSurface": surface,
        "mOnSurface": on_surface,
        "mSurfaceVariant": surface_variant,
        "mOnSurfaceVariant": on_surface_variant,
        "mOutline": outline,
        "mShadow": shadow,
        "mHover": tertiary,
        "mOnHover": on_tertiary,
    }


def build_variant(
    hues: list[tuple[float, float, float]],
    variant: str,
    dark: bool,
    random_mode: bool = False,
    rng: random.Random | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Build the sixteen roles and the terminal colours for one mode."""
    anchor_h, anchor_s, _anchor_l = hues[0]
    family = [h[0] for h in hues]

    if variant == "tonal":
        # One seed hue, the way Noctalia's own schemes work.
        accent_hues = [anchor_h, anchor_h, anchor_h]
        accent_sat = [max(anchor_s, 0.45), max(anchor_s * 0.6, 0.30), max(anchor_s * 0.8, 0.35)]
    else:
        # The image's real hues, each accent role taking a different one.
        accent_hues = [hues[0][0], hues[1][0], hues[2][0]]
        accent_sat = [max(hues[0][1], 0.45), max(hues[1][1], 0.35), max(hues[2][1], 0.30)]

    if random_mode and rng is not None:
        # Keep the anchor on the image's hue, move the others nearby, and
        # shuffle which role gets which hue.
        accent_hues = [
            anchor_h + rng.uniform(-45.0, 45.0),
            anchor_h + rng.uniform(-45.0, 45.0),
            anchor_h + rng.uniform(-45.0, 45.0),
        ]
        accent_sat = [rng.uniform(0.45, 0.85) for _ in range(3)]
        rng.shuffle(accent_hues)

    if dark:
        # Deep and saturated.
        accent_light = 0.62
        surface_l, surface_variant_l = 0.11, 0.16
        surface_s, surface_variant_s = 0.05, 0.06
        on_surface_l = 0.94
        chroma_scale = 1.0
    else:
        # Washed out and high key.
        accent_light = 0.60
        surface_l, surface_variant_l = 0.975, 0.93
        surface_s, surface_variant_s = 0.03, 0.04
        on_surface_l = 0.16
        chroma_scale = 0.62

    primary = hsl_hex(accent_hues[0], min(accent_sat[0] * chroma_scale, 1.0), accent_light)
    secondary = hsl_hex(accent_hues[1], min(accent_sat[1] * chroma_scale, 1.0), accent_light)
    tertiary = hsl_hex(accent_hues[2], min(accent_sat[2] * chroma_scale, 1.0), accent_light)

    # The "on" colour sits on its accent. In dark mode it is a pale tint of the
    # accent hue, in light mode a deep one. The contrast pass below fixes the
    # exact value, so these are only a starting point.
    on_lightness = 0.95 if dark else 0.15
    on_primary = hsl_hex(accent_hues[0], 0.20, on_lightness)
    on_secondary = hsl_hex(accent_hues[1], 0.20, on_lightness)
    on_tertiary = hsl_hex(accent_hues[2], 0.20, on_lightness)

    surface = hsl_hex(anchor_h, surface_s, surface_l)
    surface_variant = hsl_hex(anchor_h, surface_variant_s, surface_variant_l)
    on_surface = hsl_hex(anchor_h, 0.08, on_surface_l)
    on_surface_variant = hsl_hex(anchor_h, 0.10, on_surface_l + (0.03 if dark else -0.03))
    error = "#ba1a1a" if dark else "#b3261e"
    on_error = "#ffffff"
    outline = hsl_hex(anchor_h, 0.10, 0.55 if dark else 0.45)
    shadow = "#000000"

    roles = _roles(
        primary=primary,
        on_primary=on_primary,
        secondary=secondary,
        on_secondary=on_secondary,
        tertiary=tertiary,
        on_tertiary=on_tertiary,
        surface=surface,
        on_surface=on_surface,
        surface_variant=surface_variant,
        on_surface_variant=on_surface_variant,
        error=error,
        on_error=on_error,
        outline=outline,
        shadow=shadow,
    )
    return roles, {}


def build_terminal(roles: dict[str, str], hues, dark: bool, target: float) -> dict[str, str]:
    """The terminal block. Noctalia refuses a palette without one."""
    anchor_h, anchor_s, _ = hues[0]
    chroma = max(min(anchor_s, 0.85), 0.35)
    normal_l = 0.62 if dark else 0.45
    bright_l = 0.78 if dark else 0.32
    background = roles["mSurface"]

    terminal: dict[str, object] = {"normal": {}, "bright": {}}
    for name, hue in ANSI_HUES.items():
        normal = hsl_hex(hue, chroma, normal_l)
        bright = hsl_hex(hue, chroma, bright_l)
        terminal["normal"][name] = ensure_contrast(normal, background, UI_CONTRAST)
        terminal["bright"][name] = ensure_contrast(bright, background, UI_CONTRAST)

    black = hsl_hex(anchor_h, 0.05, 0.08 if dark else 0.18)
    white = hsl_hex(anchor_h, 0.05, 0.96 if dark else 0.92)
    terminal["normal"]["black"] = black
    terminal["normal"]["white"] = white
    terminal["bright"]["black"] = hsl_hex(anchor_h, 0.05, 0.20 if dark else 0.30)
    terminal["bright"]["white"] = hsl_hex(anchor_h, 0.05, 1.0 if dark else 0.98)

    terminal["foreground"] = roles["mOnSurface"]
    terminal["background"] = background
    terminal["cursor"] = roles["mPrimary"]
    terminal["cursorText"] = roles["mOnPrimary"]
    terminal["selectionFg"] = roles["mOnPrimary"]
    terminal["selectionBg"] = roles["mPrimary"]
    return terminal


def enforce_contrast(roles: dict[str, str], target: float) -> dict[str, str]:
    """Make every text pair readable. Noctalia trusts the roles we give it."""
    out = dict(roles)

    # The accents are used as focus rings and selection marks, so they must
    # stand out from the surface even though no text sits on them. This runs
    # first, because moving an accent changes the contrast of the text on it.
    for accent in ("mPrimary", "mSecondary", "mTertiary"):
        out[accent] = ensure_contrast(out[accent], out["mSurface"], UI_CONTRAST)
    # The outline is a UI element, so it needs the lower bar too.
    out["mOutline"] = ensure_contrast(out["mOutline"], out["mSurface"], UI_CONTRAST)

    # Now the text pairs, against the accents as they finally are.
    pairs = [
        ("mOnPrimary", "mPrimary"),
        ("mOnSecondary", "mSecondary"),
        ("mOnTertiary", "mTertiary"),
        ("mOnError", "mError"),
        ("mOnSurface", "mSurface"),
        ("mOnSurfaceVariant", "mSurfaceVariant"),
        ("mOnHover", "mHover"),
    ]
    for foreground, background in pairs:
        out[foreground] = ensure_contrast(out[foreground], out[background], target)
    return out


def build_palette(
    wallpaper: str,
    variant: str = "duotone",
    seed: int = 0,
    random_mode: bool = False,
    target: float = TEXT_CONTRAST,
) -> dict[str, dict[str, object]]:
    colors = dominant_colors(wallpaper)
    hues = usable_hues(colors)

    rng = None
    if random_mode:
        digest = hashlib.sha256(f"{wallpaper}:{seed}".encode()).hexdigest()
        rng = random.Random(int(digest[:16], 16))

    result: dict[str, dict[str, object]] = {}
    for name, dark in (("dark", True), ("light", False)):
        roles, _ = build_variant(hues, variant, dark, random_mode, rng)
        roles = enforce_contrast(roles, target)
        terminal = build_terminal(roles, hues, dark, target)
        roles["terminal"] = terminal
        result[name] = roles
    # Noctalia ignores keys it does not know. These record how the palette was
    # made, so the settings window can show it and group palettes by wallpaper.
    result["wallpaper"] = wallpaper
    result["generator"] = "random" if random_mode else "ours"
    result["variant"] = variant
    result["seed"] = seed
    return result


# --------------------------------------------------------------------- checks

REQUIRED_ROLES = (
    "mPrimary", "mOnPrimary", "mSecondary", "mOnSecondary",
    "mTertiary", "mOnTertiary", "mError", "mOnError",
    "mSurface", "mOnSurface", "mSurfaceVariant", "mOnSurfaceVariant",
    "mOutline", "mShadow", "mHover", "mOnHover",
)
REQUIRED_TERMINAL = (
    "normal", "bright", "foreground", "background",
    "cursor", "cursorText", "selectionFg", "selectionBg",
)


def find_saved_palette(directory: str, wallpaper: str) -> str:
    """The name of a saved palette that belongs to this wallpaper.

    A palette file records the wallpaper it came from under a "wallpaper" key.
    When several were saved for one wallpaper, the one written last wins,
    because that is the one the person chose most recently.
    """
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return ""
    best_name = ""
    best_time = -1.0
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if data.get("wallpaper") != wallpaper:
            continue
        # Only a palette the person kept counts. The live file the generator
        # writes records the wallpaper too, but it is replaced every time.
        if data.get("saved") is not True:
            continue
        try:
            written = os.path.getmtime(path)
        except OSError:
            written = 0.0
        if written >= best_time:
            best_time = written
            best_name = name[:-5]
    return best_name


def check_format(palette: dict) -> list[str]:
    """Every role and terminal key Noctalia needs must be present.

    Noctalia accepts a palette that points at a file and then rejects it when
    it reads it, so a missing key is worth catching here. The terminal block
    matters most: without it the whole palette fails to parse.
    """
    failures = []
    for mode in ("dark", "light"):
        roles = palette.get(mode)
        if not isinstance(roles, dict):
            failures.append(f"{mode}: the mode is missing")
            continue
        for role in REQUIRED_ROLES:
            if role not in roles:
                failures.append(f"{mode}: the role {role} is missing")
        terminal = roles.get("terminal")
        if not isinstance(terminal, dict):
            failures.append(f"{mode}: the terminal block is missing")
            continue
        for key in REQUIRED_TERMINAL:
            if key not in terminal:
                failures.append(f"{mode}: terminal.{key} is missing")
        for group in ("normal", "bright"):
            colours = terminal.get(group)
            if not isinstance(colours, dict):
                failures.append(f"{mode}: terminal.{group} is missing")
                continue
            for name in ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"):
                if name not in colours:
                    failures.append(f"{mode}: terminal.{group}.{name} is missing")
    return failures


def check_palette(palette: dict) -> list[str]:
    """Every pair that must be readable. Returns the failures."""
    failures = []
    pairs = [
        ("mOnPrimary", "mPrimary", TEXT_CONTRAST),
        ("mOnSecondary", "mSecondary", TEXT_CONTRAST),
        ("mOnTertiary", "mTertiary", TEXT_CONTRAST),
        ("mOnError", "mError", TEXT_CONTRAST),
        ("mOnSurface", "mSurface", TEXT_CONTRAST),
        ("mOnSurfaceVariant", "mSurfaceVariant", TEXT_CONTRAST),
        ("mOnHover", "mHover", TEXT_CONTRAST),
        ("mOutline", "mSurface", UI_CONTRAST),
        ("mPrimary", "mSurface", UI_CONTRAST),
        ("mSecondary", "mSurface", UI_CONTRAST),
        ("mTertiary", "mSurface", UI_CONTRAST),
    ]
    for mode, roles in palette.items():
        # The file also holds a "wallpaper" key, which is a path and not a set
        # of colours.
        if not isinstance(roles, dict):
            continue
        for foreground, background, target in pairs:
            ratio = contrast_of(roles[foreground], roles[background])
            if ratio < target:
                failures.append(
                    f"{mode}: {foreground} on {background} is {ratio:.2f}:1, needs {target}:1"
                )
        terminal = roles.get("terminal", {})
        background = terminal.get("background", "#000000")
        # The six chromatic colours must be readable on the terminal background.
        # Black and white are skipped on purpose: they are the background's own
        # extremes, and a real terminal theme keeps them low contrast.
        for group in ("normal", "bright"):
            for name, value in terminal.get(group, {}).items():
                if name in ("black", "white"):
                    continue
                ratio = contrast_of(value, background)
                if ratio < UI_CONTRAST:
                    failures.append(
                        f"{mode}: terminal {group}.{name} on background is {ratio:.2f}:1, needs {UI_CONTRAST}:1"
                    )
        # The terminal text itself must be readable.
        ratio = contrast_of(terminal.get("foreground", "#ffffff"), background)
        if ratio < TEXT_CONTRAST:
            failures.append(
                f"{mode}: terminal foreground on background is {ratio:.2f}:1, needs {TEXT_CONTRAST}:1"
            )
    return failures


# ------------------------------------------------------------------------ main

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Noctalia palette from a wallpaper.")
    parser.add_argument("--wallpaper", required=True, help="the image to read")
    parser.add_argument("--output", help="where to write the palette JSON")
    parser.add_argument("--variant", default="duotone", choices=("duotone", "tonal"))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--random", action="store_true", help="randomise the accents")
    parser.add_argument("--contrast", type=float, default=TEXT_CONTRAST)
    parser.add_argument("--palette-dir", help="where the saved palettes live")
    parser.add_argument("--find-saved", action="store_true",
                        help="print the name of the saved palette for this wallpaper")
    parser.add_argument("--print", dest="show", action="store_true")
    parser.add_argument("--check", action="store_true", help="report contrast and exit")
    args = parser.parse_args()

    if not os.path.isfile(args.wallpaper):
        print(f"error: no such wallpaper: {args.wallpaper}", file=sys.stderr)
        return 1

    if args.find_saved:
        directory = args.palette_dir or os.path.join(
            os.path.dirname(os.path.abspath(args.wallpaper)), "palettes"
        )
        name = find_saved_palette(directory, os.path.abspath(args.wallpaper))
        if name:
            print(name)
        return 0

    try:
        palette = build_palette(
            args.wallpaper,
            variant=args.variant,
            seed=args.seed,
            random_mode=args.random,
            target=args.contrast,
        )
    except (OSError, ValueError) as exc:
        print(f"error: could not read the wallpaper: {exc}", file=sys.stderr)
        return 1

    failures = check_format(palette) + check_palette(palette)
    for failure in failures:
        print(f"warning: {failure}", file=sys.stderr)
    if failures and args.output:
        print("error: the palette failed its own check, nothing was written", file=sys.stderr)
        return 1

    if args.show or args.check:
        print(json.dumps(palette, indent=2))
        return 0

    if not args.output:
        print("error: either --output or --print is required", file=sys.stderr)
        return 1

    parent = os.path.dirname(os.path.abspath(args.output))
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = f"{args.output}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(palette, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, args.output)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

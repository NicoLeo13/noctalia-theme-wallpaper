#!/usr/bin/env python3
"""Graphical settings for the Noctalia theme wallpaper switcher.

Edits ~/.config/noctalia/theme-wallpaper.conf, which is the same file the
theme_mode_changed hook reads. Saving needs no reload, because the hook re-reads
the file on every switch.

Uses the standard library and the GTK 4 / libadwaita introspection bindings only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

APP_ID = "dev.local.NoctaliaThemeWallpaper"
THUMB_SIZE = 190
MAX_THUMBS = 300
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".jxl", ".avif")
GRID_COLUMNS = 4  # also the width the empty state spans

# value, combo label, explanation
STRATEGIES = (
    ("fixed", "One image per mode", "Uses LIGHT_WALLPAPER and DARK_WALLPAPER."),
    ("random", "Random from a folder", "Picks a random image from the folder on every switch."),
    ("cycle", "Cycle a folder", "Walks the folder in order and wraps, so nothing repeats."),
)
STRATEGY_VALUES = [v for v, _l, _d in STRATEGIES]
STRATEGY_LABELS = [l for _v, l, _d in STRATEGIES]

DESKTOP_PAGE = "Desktop wallpaper"
LOCKSCREEN_PAGE = "KDE lock screen"
PALETTE_PAGE = "Palette"

# The four colours Noctalia shows beside a palette name.
PREVIEW_FIELDS = ("mPrimary", "mSecondary", "mTertiary", "mError")

# The colour roles shown in the preview, in the order they are drawn. The first
# part is the key as Noctalia names it, so the lookup cannot get it wrong.
SWATCH_ROLES = (
    ("mPrimary", "Primary"),
    ("mSecondary", "Secondary"),
    ("mTertiary", "Tertiary"),
    ("mError", "Error"),
    ("mSurface", "Surface"),
    ("mSurfaceVariant", "Surface variant"),
    ("mOnSurface", "Text"),
    ("mOutline", "Outline"),
)

# The three generators, and the explanation shown under the selector.
GENERATORS = (
    ("noctalia", "Noctalia draws it", "Noctalia makes the palette from the wallpaper, with the scheme you pick in Noctalia."),
    ("ours", "This tool draws it", "A palette is made from the wallpaper and applied as a Noctalia custom palette."),
    ("random", "This tool draws it at random", "The accents move around the hue of the wallpaper, so each wallpaper still feels like itself."),
)

# What to do when the wallpaper already has a palette saved for it.
SAVED_BEHAVIOUR = (
    ("always", "Always draw a new one", "A fresh palette is drawn every time the wallpaper changes."),
    ("prefer", "Use the saved one", "The palette you kept for that wallpaper is used instead."),
)

VARIANTS = (
    ("duotone", "Keep the picture's hues", "Light is washed out and high key, dark is deep and saturated."),
    ("tonal", "One seed hue, like Noctalia", "All three accents come from one colour of the picture."),
)


def load_generator():
    """Load the palette generator.

    It lives next to the hook script when the program is installed, and in
    the files folder when it is run straight from a checkout. Both are tried.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    explicit = os.environ.get("THEME_WALLPAPER_PALETTE_GENERATOR")
    if explicit:
        candidates.append(explicit)
    candidates.append(os.path.join(here, "hooks", "palette-generator.py"))
    candidates.append(os.path.join(here, "..", "files", "palette-generator.py"))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("palette_generator", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            continue
        return module
    return None


def palette_dir() -> str:
    explicit = os.environ.get("THEME_WALLPAPER_PALETTE_DIR")
    if explicit:
        return explicit
    return os.path.join(config_home(), "palettes")


def config_home() -> str:
    explicit = os.environ.get("NOCTALIA_CONFIG_HOME")
    if explicit:
        return explicit
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "noctalia")


DEFAULT_CONF = os.path.join(config_home(), "theme-wallpaper.conf")
DEFAULT_HOOK = os.path.join(config_home(), "hooks", "theme-wallpaper.sh")


def expand(path: str) -> str:
    """Match the shell script: a leading ~/ means the home directory."""
    if path.startswith("~/"):
        return os.path.join(os.path.expanduser("~"), path[2:])
    if path == "~":
        return os.path.expanduser("~")
    return path


def parse_line(line: str) -> tuple[str | None, str | None]:
    """Read one KEY=VALUE line the same way the shell script reads it."""
    # ASCII whitespace only, so this matches the shell's [[:space:]] class.
    # str.strip() would also remove Unicode spaces such as U+00A0.
    s = line.strip(" \t\r\n\v\f")
    if not s or s.startswith("#") or "=" not in s:
        return None, None
    key, _, value = s.partition("=")
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not key:
        return None, None
    return key, value


class Config:
    """A KEY=VALUE file that keeps its comments and key order when saved."""

    def __init__(self, path: str):
        self.path = path
        self.lines: list[str] = []
        self.values: dict[str, str] = {}
        self._disk_text = ""
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8", newline="\n") as fh:
                # Split on newlines only. str.splitlines() also splits on
                # \v, \f and several Unicode marks, which the shell reader
                # does not treat as line breaks, so the two would disagree
                # and a save would corrupt the file.
                self.lines = fh.read().split("\n")
        except OSError:
            self.lines = []
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        self._disk_text = "\n".join(self.lines) + ("\n" if self.lines else "")
        self.values = {}
        for line in self.lines:
            key, value = parse_line(line)
            if key is not None:
                self.values[key] = value

    def get(self, key: str, default: str = "") -> str:
        value = self.values.get(key, "")
        return value if value else default

    def set(self, key: str, value: str) -> None:
        # A newline inside a value would become a second line with no "=",
        # which the reader then drops, silently destroying the setting.
        if "\n" in value or "\r" in value:
            raise ValueError(f"refusing to write a newline into {key}")
        self.values[key] = value
        out: list[str] = []
        replaced = False
        for line in self.lines:
            existing, _ = parse_line(line)
            if existing == key:
                if replaced:
                    continue  # collapse a duplicated key, the last one wins
                out.append(f"{key} = {value}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            while out and not out[-1].strip():
                out.pop()
            if out:
                out.append("")
            out.append(f"{key} = {value}")
        self.lines = out

    def on_disk(self) -> str:
        """The raw text currently on disk, or an empty string."""
        try:
            with open(self.path, encoding="utf-8", newline="\n") as fh:
                return fh.read()
        except OSError:
            return ""

    def reload_if_changed(self) -> bool:
        """Re-read when someone edited the file while this window was open.

        The window has no unsaved-state model, because the hook reads the
        file on every switch. Without this check, a save would overwrite a
        hand edit made in another editor after the window opened.

        The test is against the text as it was last read, not against the
        current in-memory text. Comparing with the in-memory text would
        mistake the window's own pending change for an outside edit and throw
        that change away.
        """
        if self.on_disk() == self._disk_text:
            return False
        self.load()
        return True

    def rendered_text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def save(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
        except OSError:
            mode = None

        # A unique temporary name, so two windows cannot consume each
        # other's file. mkstemp creates it with mode 0600 already.
        fd, tmp = tempfile.mkstemp(dir=parent or ".", prefix=".theme-wallpaper-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self.rendered_text())
                fh.flush()
                # Keep the original mode, so a 0600 file does not become 0644.
                if mode is not None:
                    os.fchmod(fh.fileno(), mode)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, self.path)
        self._disk_text = self.rendered_text()


def scan_images(directory: str) -> list[str]:
    """Images directly inside a folder, by the same rules as the shell script."""
    directory = expand(directory.strip())
    if not directory or not os.path.isdir(directory):
        return []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    found: list[str] = []
    for name in names:
        if not name.lower().endswith(IMAGE_EXTS):
            continue
        full = os.path.join(directory, name)
        if os.path.isfile(full):
            found.append(full)
            if len(found) >= MAX_THUMBS:
                break
    return found


def make_thumbnail(path: str):
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, THUMB_SIZE, THUMB_SIZE, True)
    except GLib.Error:
        return None
    return Gdk.Texture.new_for_pixbuf(pixbuf)


def run_hook(hook: str, conf: str, dry_run: bool) -> tuple[int, str]:
    env = dict(os.environ)
    env["THEME_WALLPAPER_CONF"] = conf
    # Set it either way. Copying the environment alone would let an
    # inherited DRY_RUN=1 turn "Apply now" into a silent no-op.
    env["THEME_WALLPAPER_DRY_RUN"] = "1" if dry_run else "0"
    try:
        done = subprocess.run([hook], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"could not run the hook: {exc}"
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    if not lines:
        lines = [ln for ln in done.stderr.splitlines() if ln.strip()]
    return done.returncode, "\n".join(lines) if lines else "(no output)"


def current_theme_mode() -> str:
    try:
        done = subprocess.run(
            ["noctalia", "msg", "theme-mode-get"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() or "unknown"


class PickerGroup(Adw.PreferencesGroup):
    """A thumbnail grid plus the image chosen for one mode.

    When mirror is set, the group borrows that group's folder instead of
    declaring its own, so the two stay in step and there is one source of truth.
    """

    def __init__(self, title, folder_key, file_key, on_change, mirror=None):
        super().__init__(title=title)
        self.folder_key = folder_key
        self.file_key = file_key
        self.on_change = on_change
        self.mirror = mirror
        self.images: list[str] = []
        self.selected = ""
        self.current = ""
        self.buttons: dict[str, Gtk.Button] = {}
        self.folder_row = None

        if mirror is None:
            self.folder_row = Adw.EntryRow(title="Folder to browse")
            self.folder_row.set_show_apply_button(False)
            browse = Gtk.Button(valign=Gtk.Align.CENTER, icon_name="folder-open-symbolic")
            browse.set_tooltip_text("Choose the folder to browse")
            browse.add_css_class("flat")
            browse.connect("clicked", self._on_browse)
            self.folder_row.add_suffix(browse)
            self.add(self.folder_row)

        self.status = Adw.ActionRow()
        self.add(self.status)

        # A single scrolling row of thumbnails. A wrapped grid looks tidy but
        # two stacked grids of four columns do not fit a normal window, and the
        # pictures get squeezed to a fraction of their height. A filmstrip keeps
        # every thumbnail at full size and scrolls sideways instead.
        self.grid = Gtk.Grid(
            column_spacing=8,
            row_spacing=8,
            margin_top=6,
            margin_bottom=6,
        )
        self.grid.set_halign(Gtk.Align.START)

        self.scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_width=True,
            propagate_natural_height=True,
            min_content_height=THUMB_SIZE + 12,
        )
        self.scroller.set_child(self.grid)
        # An explicit height, because a PreferencesGroup hands its children the
        # leftover space and will happily squeeze this to nothing, which makes
        # every thumbnail shrink to a sliver.
        self.scroller.set_size_request(-1, THUMB_SIZE + 20)
        self.scroller.set_valign(Gtk.Align.START)
        self.add(self.scroller)

        # The empty state lives inside the grid, so it occupies the same place
        # the thumbnails would.
        self.empty = Adw.StatusPage(
            icon_name="image-missing-symbolic",
            title="No images in this folder",
            description="Add some images, or point the folder somewhere else.",
        )
        self.empty.set_size_request(-1, 170)
        self._next_cell = 0
        self.grid.attach(self.empty, 0, 0, GRID_COLUMNS, 1)

    # ---------------------------------------------------------------- state
    def load_from(self, cfg: Config) -> None:
        if self.folder_row is not None:
            self.folder_row.set_text(cfg.get(self.folder_key))
        self.set_file(cfg.get(self.file_key))

    def set_file(self, path: str) -> None:
        self.selected = expand(path.strip()) if path else ""
        self.refresh_grid()

    def current_file(self) -> str:
        return self.selected

    def current_folder(self) -> str:
        if self.folder_row is not None:
            return self.folder_row.get_text().strip()
        if self.mirror is not None:
            return self.mirror.current_folder()
        return ""

    # ------------------------------------------------------------ rendering
    def refresh_grid(self) -> None:
        while (child := self.grid.get_first_child()) is not None:
            self.grid.remove(child)
        self._next_cell = 0

        self.images = scan_images(self.current_folder())

        rendered = 0
        for path in self.images:
            texture = make_thumbnail(path)
            if texture is None:
                continue
            button = Gtk.ToggleButton()
            button.set_child(Gtk.Picture.new_for_paintable(texture))
            button.set_tooltip_text(path)
            button.add_css_class("flat")
            button.set_size_request(THUMB_SIZE, -1)
            if path == self.selected:
                button.set_active(True)
                button.add_css_class("suggested-action")
            button.connect("clicked", self._on_thumb, path)
            self.grid.attach(button, self._next_cell, 0, 1, 1)
            self._next_cell += 1
            self.buttons[path] = button
            rendered += 1

        self.grid.attach(self.empty, 0, 0, GRID_COLUMNS, 1)
        # Gate on what was rendered. A folder can list images that all
        # fail to decode, which would otherwise leave a blank strip and a
        # misleading "No image selected".
        self.empty.set_visible(rendered == 0)

        if not self.selected:
            self.status.set_title("No image selected")
            self.status.set_subtitle("Type a path above, or pick a thumbnail")
            return
        name = os.path.basename(self.selected)
        if self.selected in self.images:
            self.status.set_title(name)
            self.status.set_subtitle(self.selected)
        else:
            self.status.set_title(f"{name} (not in this folder)")
            self.status.set_subtitle(self.selected)

    def set_current(self, path: str) -> None:
        """Mark the wallpaper that is on screen, without rebuilding the strip."""
        self.current = path or ""
        for candidate, button in self.buttons.items():
            if candidate == self.current:
                button.add_css_class("suggested-action")
            else:
                button.remove_css_class("suggested-action")

    def _on_thumb(self, _button, path: str) -> None:
        self.selected = path
        self.refresh_grid()
        self.on_change(self)

    def _on_browse(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Choose a wallpaper folder")
        start = expand(self.current_folder())
        if os.path.isdir(start):
            dialog.set_initial_folder(Gio.File.new_for_path(start))
        dialog.select_folder(self.get_root(), None, self._on_folder_chosen)

    def _on_folder_chosen(self, source, result) -> None:
        try:
            folder = source.select_folder_finish(result)
        except GLib.Error:
            return
        if folder and folder.get_path():
            self.folder_row.set_text(folder.get_path())
            self.refresh_grid()
            # A mirrored group follows the folder it borrows.
            if self.mirror is not None:
                self.mirror.refresh_grid()
            self.on_change(self)


class PaletteDots(Gtk.Box):
    """Four small colour dots, like Noctalia shows beside a palette name.

    Noctalia shows the primary, the secondary, the tertiary and the error
    colour of a palette, so a list of palettes can be told apart at a glance.
    """

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                         valign=Gtk.Align.CENTER)
        self.dots: list[Gtk.Label] = []
        self.providers: list[Gtk.CssProvider] = []
        for _ in range(len(PREVIEW_FIELDS)):
            dot = Gtk.Label(label="")
            dot.set_size_request(13, 13)
            dot.add_css_class("palette-dot")
            provider = Gtk.CssProvider()
            dot.get_style_context().add_provider(
                provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            self.append(dot)
            self.dots.append(dot)
            self.providers.append(provider)

    def set_palette(self, data: dict, mode: str) -> None:
        roles = data.get(mode) if isinstance(data, dict) else None
        if not isinstance(roles, dict):
            roles = {}
        for dot, provider, key in zip(self.dots, self.providers, PREVIEW_FIELDS):
            value = roles.get(key, "")
            if value:
                provider.load_from_data(f"label {{ background-color: {value}; }}")
                dot.set_text(" ")
            else:
                provider.load_from_data("label { background-color: transparent; }")
                dot.set_text("")


class PaletteStrip(Gtk.Box):
    """A row of colour swatches, one per role."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.START)
        self.labels: dict[str, Gtk.Label] = {}
        self.providers: dict[str, Gtk.CssProvider] = {}
        # A surface swatch is the same colour as the panel it sits on, so every
        # swatch needs a border to be seen at all.
        border = Gtk.CssProvider()
        border.load_from_data(
            ".palette-swatch { border: 1px solid alpha(128,128,128,0.75); border-radius: 3px; }"
        )
        for key, title in SWATCH_ROLES:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            swatch = Gtk.Label(label="")
            swatch.add_css_class("palette-swatch")
            swatch.get_style_context().add_provider(
                border, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            swatch.set_size_request(46, 30)
            caption = Gtk.Label(label=title)
            caption.add_css_class("caption")
            caption.add_css_class("dim-label")
            box.append(swatch)
            box.append(caption)
            self.append(box)
            self.labels[key] = swatch

    def set_palette(self, palette: dict, mode: str) -> None:
        """Show one mode of a palette, or blank the strip when there is none."""
        roles = palette.get(mode) if isinstance(palette, dict) else None
        if not isinstance(roles, dict):
            for label in self.labels.values():
                label.set_text("")
            return
        for key, _title in SWATCH_ROLES:
            value = roles.get(key, "")
            label = self.labels[key]
            label.set_text("")
            if not value:
                continue
            # One provider per swatch, reloaded each time. Adding a new
            # provider on every change would pile them up on the context.
            provider = self.providers.get(key)
            if provider is None:
                provider = Gtk.CssProvider()
                self.providers[key] = provider
                label.get_style_context().add_provider(
                    provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            provider.load_from_data(f"label {{ background-color: {value}; }}")
            label.set_text(" ")


class ThemeWallpaperWindow(Adw.ApplicationWindow):
    def __init__(self, application, conf_path: str, hook_path: str):
        super().__init__(application=application, title="Noctalia Theme Wallpaper")
        self.set_default_size(960, 740)
        self.conf_path = conf_path
        self.hook_path = hook_path
        self.cfg = Config(conf_path)
        self._loading = True

        header = Adw.HeaderBar()
        open_button = Gtk.Button(label="Open file")
        open_button.set_tooltip_text(f"Edit {conf_path} in your text editor")
        open_button.connect("clicked", self._on_open_file)
        header.pack_start(open_button)

        self.desktop_content = Adw.PreferencesPage()
        self.lock_content = Adw.PreferencesPage()
        self.palette_content = Adw.PreferencesPage()
        self._preview: dict = {}
        self._candidate: dict = {}
        self._generated_for = ""
        self._mine_rows: list = []
        self._all_rows: list = []
        self._dots: list = []

        # A TabView with its own bar, because a bare ViewStack shows no way to
        # reach the second page. append() wraps each child in an AdwTabPage,
        # and that page's title is what the tab bar shows, so set it here
        # rather than on the PreferencesPage.
        self.tabs = Adw.TabView()
        self.tabs.append(self.desktop_content)
        self.tabs.append(self.lock_content)
        self.tabs.append(self.palette_content)
        for page, title, icon in (
            (self.tabs.get_nth_page(0), DESKTOP_PAGE, "preferences-desktop-wallpaper-symbolic"),
            (self.tabs.get_nth_page(1), LOCKSCREEN_PAGE, "system-lock-screen-symbolic"),
            (self.tabs.get_nth_page(2), PALETTE_PAGE, "applications-graphics-symbolic"),
        ):
            page.set_title(title)
            page.set_icon(Gio.ThemedIcon.new(icon))

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=6,
                         margin_bottom=6, margin_start=12, margin_end=12)
        self.apply_button = Gtk.Button(label="Apply now", tooltip_text="Run the hook for the current mode")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda _b: self._apply(False))
        self.dry_button = Gtk.Button(label="Dry run", tooltip_text="Show what would change, change nothing")
        self.dry_button.connect("clicked", lambda _b: self._apply(True))
        footer.append(self.apply_button)
        footer.append(self.dry_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.tabs)
        content.append(footer)

        self.toasts = Adw.ToastOverlay(child=content)
        toolbar = Adw.ToolbarView(content=self.toasts)
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(Adw.TabBar(view=self.tabs, autohide=False))
        self.set_content(toolbar)

        self._build_desktop_page()
        self._build_lockscreen_page()
        self._build_palette_page()

        # The desktop changes without the window being told, so the window
        # looks at the world now and then. Without this the swatches show a
        # palette that is no longer the one on screen.
        self._watch = GLib.timeout_add_seconds(3, self._tick)

        self._loading = False

    # ------------------------------------------------------------- desktop
    def _build_desktop_page(self) -> None:
        page = self.desktop_content

        now = Adw.PreferencesGroup(title="Right now")
        page.add(now)
        self.now_row = Adw.ActionRow(title="The wallpaper on screen")
        now.add(self.now_row)

        # A PreferencesPage only accepts groups, so the loose rows go in one.
        top = Adw.PreferencesGroup()
        page.add(top)

        self.strategy_row = Adw.ComboRow(
            title="How to choose", model=Gtk.StringList.new(STRATEGY_LABELS)
        )
        self.strategy_row.connect("notify::selected", self._on_strategy_changed)
        top.add(self.strategy_row)

        self.strategy_desc = Adw.ActionRow()
        top.add(self.strategy_desc)

        self.pickers: list[PickerGroup] = []
        for title, folder_key, file_key in (
            ("Light mode", "LIGHT_DIR", "LIGHT_WALLPAPER"),
            ("Dark mode", "DARK_DIR", "DARK_WALLPAPER"),
        ):
            picker = PickerGroup(title, folder_key, file_key, self._on_change)
            page.add(picker)
            self.pickers.append(picker)

        bottom = Adw.PreferencesGroup()
        page.add(bottom)
        self.mode_row = Adw.ActionRow(title="Checking the current theme mode")
        bottom.add(self.mode_row)
        page.set_vexpand(True)

    # ------------------------------------------------------------- palette
    def _build_palette_page(self) -> None:
        page = self.palette_content

        top = Adw.PreferencesGroup()
        page.add(top)

        self.generator_row = Adw.ComboRow(
            title="Who draws the palette",
            model=Gtk.StringList.new([label for _v, label, _d in GENERATORS]),
        )
        self.generator_row.connect("notify::selected", self._on_generator_changed)
        top.add(self.generator_row)

        self.generator_desc = Adw.ActionRow()
        top.add(self.generator_desc)

        self.variant_row = Adw.ComboRow(
            title="How the two modes differ",
            model=Gtk.StringList.new([label for _v, label, _d in VARIANTS]),
        )
        self.variant_row.connect("notify::selected", self._on_variant_changed)
        top.add(self.variant_row)


        self.variant_desc = Adw.ActionRow()
        top.add(self.variant_desc)

        self.saved_row = Adw.ComboRow(
            title="When a wallpaper has a saved palette",
            model=Gtk.StringList.new([label for _v, label, _d in SAVED_BEHAVIOUR]),
        )
        self.saved_row.connect("notify::selected", self._on_saved_changed)
        top.add(self.saved_row)

        self.saved_desc = Adw.ActionRow()
        top.add(self.saved_desc)

        self.palette_group = Adw.PreferencesGroup(title="The palette right now")
        page.add(self.palette_group)

        self.palette_strip = PaletteStrip()
        self.palette_group.add(self.palette_strip)

        self.palette_info = Adw.ActionRow()
        self.palette_group.add(self.palette_info)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.generate_button = Gtk.Button(label="Draw a new one")
        self.generate_button.set_tooltip_text("Make another palette from the current wallpaper")
        self.generate_button.connect("clicked", lambda _b: self._on_generate())
        self.apply_button = Gtk.Button(label="Use this palette")
        self.apply_button.set_tooltip_text("Make this the palette the desktop uses now")
        self.apply_button.connect("clicked", lambda _b: self._on_apply_candidate())
        self.save_button = Gtk.Button(label="Save this palette")
        self.save_button.add_css_class("suggested-action")
        self.save_button.set_tooltip_text("Keep this palette in the list and in Noctalia")
        self.save_button.connect("clicked", lambda _b: self._on_save_palette())
        buttons.append(self.generate_button)
        buttons.append(self.apply_button)
        buttons.append(self.save_button)
        self.palette_group.add(buttons)

        self.mine_group = Adw.PreferencesGroup(title="Saved for this wallpaper")
        page.add(self.mine_group)

        self.all_group = Adw.PreferencesGroup(title="All custom palettes")
        page.add(self.all_group)
        page.set_vexpand(True)

    # ---------------------------------------------------------- lockscreen
    def _build_lockscreen_page(self) -> None:
        page = self.lock_content

        now = Adw.PreferencesGroup(title="Right now")
        page.add(now)
        self.lock_now_row = Adw.ActionRow(title="The lock screen image")
        now.add(self.lock_now_row)

        top = Adw.PreferencesGroup()
        page.add(top)

        self.lock_sync = Adw.SwitchRow(
            title="Keep the lock screen in sync",
            subtitle="Write the image into the KDE lock screen settings on every switch",
        )
        self.lock_sync.connect("notify::active", self._on_lock_changed)
        top.add(self.lock_sync)

        self.lock_source = Adw.ComboRow(
            title="Lock screen image",
            model=Gtk.StringList.new(["Match the desktop wallpaper", "Use a different image"]),
        )
        self.lock_source.connect("notify::selected", self._on_lock_changed)
        top.add(self.lock_source)

        self.lock_desc = Adw.ActionRow()
        top.add(self.lock_desc)

        self.lock_pickers: list[PickerGroup] = []
        for title, file_key, mirror in (
            ("Light mode lock screen", "LOCKSCREEN_LIGHT_WALLPAPER", self.pickers[0]),
            ("Dark mode lock screen", "LOCKSCREEN_DARK_WALLPAPER", self.pickers[1]),
        ):
            picker = PickerGroup(title, None, file_key, self._on_change, mirror=mirror)
            page.add(picker)
            self.lock_pickers.append(picker)
        page.set_vexpand(True)

    # -------------------------------------------------------------- loading
    def load_config(self) -> None:
        self._loading = True
        self.cfg.load()

        strategy = self.cfg.get("STRATEGY", "fixed").strip().lower()
        index = STRATEGY_VALUES.index(strategy) if strategy in STRATEGY_VALUES else 0
        self.strategy_row.set_selected(index)

        for picker in self.pickers + self.lock_pickers:
            picker.load_from(self.cfg)

        self.lock_sync.set_active(self.cfg.get("LOCKSCREEN_SYNC", "true") != "false")
        self.lock_source.set_selected(
            0 if self.cfg.get("LOCKSCREEN_SOURCE", "desktop").strip().lower() == "desktop" else 1
        )

        generator = self.cfg.get("PALETTE_GENERATOR", "noctalia").strip().lower()
        values = [v for v, _l, _d in GENERATORS]
        self.generator_row.set_selected(values.index(generator) if generator in values else 0)
        variant = self.cfg.get("PALETTE_VARIANT", "duotone").strip().lower()
        vvalues = [v for v, _l, _d in VARIANTS]
        self.variant_row.set_selected(vvalues.index(variant) if variant in vvalues else 0)
        behaviour = self.cfg.get("PALETTE_SAVED", "always").strip().lower()
        svalues = [v for v, _l, _d in SAVED_BEHAVIOUR]
        self.saved_row.set_selected(svalues.index(behaviour) if behaviour in svalues else 0)

        self._loading = False
        self._update_visibility()
        self._update_palette_visibility()
        self._update_mode_label()
        self._refresh_palette()

    def _strategy(self) -> str:
        index = self.strategy_row.get_selected()
        return STRATEGY_VALUES[index] if 0 <= index < len(STRATEGY_VALUES) else "fixed"

    def _update_visibility(self) -> None:
        strategy = self._strategy()
        folder_mode = strategy in ("random", "cycle")

        for value, _label, desc in STRATEGIES:
            if value == strategy:
                self.strategy_desc.set_title(desc)
                break

        for picker in self.pickers:
            if picker.folder_row is None:
                continue
            if folder_mode:
                picker.folder_row.set_title("Folder this mode picks from")
                picker.folder_row.set_tooltip_text("Saved as LIGHT_DIR or DARK_DIR")
            else:
                picker.folder_row.set_title("Folder to browse")
                picker.folder_row.set_tooltip_text("Not saved unless the strategy uses a folder")

        lock_on = self.lock_sync.get_active()
        custom = self.lock_source.get_selected() == 1
        self.lock_source.set_sensitive(lock_on)
        self.lock_desc.set_sensitive(lock_on)
        if not lock_on:
            self.lock_desc.set_title("The lock screen is left untouched")
        elif custom:
            self.lock_desc.set_title("Each mode uses its own lock screen image")
        else:
            self.lock_desc.set_title("The lock screen mirrors the desktop wallpaper")
        for picker in self.lock_pickers:
            picker.set_visible(lock_on and custom)

    # ------------------------------------------------------- palette actions
    def _tick(self) -> bool:
        """Look at the world now and then, and follow it."""
        try:
            self._refresh_palette()
            self._refresh_now()
        except Exception:
            pass
        return True

    def _refresh_now(self) -> None:
        """Show what the desktop is showing, on both tabs."""
        wallpaper = self._current_wallpaper()
        self.now_row.set_title("The wallpaper on screen")
        self.now_row.set_subtitle(os.path.basename(wallpaper) if wallpaper else "Nothing yet")

        lock = self._current_lock_screen()
        self.lock_now_row.set_title("The lock screen image")
        self.lock_now_row.set_subtitle(os.path.basename(lock) if lock else "Nothing yet")

        # The lock screen follows the desktop, so the desktop path is the one
        # worth marking in the pickers.
        for picker in self.pickers:
            picker.set_current(wallpaper)

    def _current_lock_screen(self) -> str:
        """The image the KDE lock screen shows right now.

        The file lives in the config folder next to the Noctalia one, which is
        where KDE keeps it and where the hook writes it.
        """
        path = os.environ.get(
            "THEME_WALLPAPER_LOCKSCREEN_RC", os.path.expanduser("~/.config/kscreenlockerrc")
        )
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("Image="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def _current_wallpaper(self) -> str:
        try:
            done = subprocess.run(
                ["noctalia", "msg", "wallpaper-get"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout.strip().split("\n")[0].strip()

    def _generator(self) -> str:
        index = self.generator_row.get_selected()
        values = [v for v, _l, _d in GENERATORS]
        return values[index] if 0 <= index < len(values) else "noctalia"

    def _saved_behaviour(self) -> str:
        index = self.saved_row.get_selected()
        values = [v for v, _l, _d in SAVED_BEHAVIOUR]
        return values[index] if 0 <= index < len(values) else "always"

    def _variant(self) -> str:
        index = self.variant_row.get_selected()
        values = [v for v, _l, _d in VARIANTS]
        return values[index] if 0 <= index < len(values) else "duotone"

    def _seed(self) -> int:
        raw = self.cfg.get("PALETTE_SEED", "0").strip()
        try:
            return int(raw)
        except ValueError:
            return 0

    def _on_generator_changed(self, *_args) -> None:
        self._update_palette_visibility()
        if not self._loading:
            self.save_config(silent=True)
        self._refresh_palette()

    def _on_variant_changed(self, *_args) -> None:
        if not self._loading:
            self.save_config(silent=True)
        self._refresh_palette()

    def _on_saved_changed(self, *_args) -> None:
        if not self._loading:
            self.save_config(silent=True)
        self._update_palette_visibility()

    def _update_palette_visibility(self) -> None:
        generator = self._generator()
        ours = generator in ("ours", "random")
        self.variant_row.set_sensitive(ours)
        self.variant_desc.set_sensitive(ours)
        self.generate_button.set_sensitive(ours)
        self.save_button.set_sensitive(ours)
        self.apply_button.set_sensitive(bool(getattr(self, "_candidate", None)))
        for value, label, description in GENERATORS:
            if value == generator:
                self.generator_desc.set_title(description)
                break
        for value, label, description in VARIANTS:
            if value == self._variant():
                self.variant_desc.set_title(description)
                break
        for value, label, description in SAVED_BEHAVIOUR:
            if value == self._saved_behaviour():
                self.saved_desc.set_title(description)
                break

    def _refresh_palette(self) -> None:
        """Bring the window in line with what the desktop is showing.

        The live palette is read from its file, so the swatches always tell
        the truth. A palette is only drawn when the wallpaper changed, or when
        the person asked for a new one.
        """
        wallpaper = self._current_wallpaper()
        generator = self._generator()

        if generator == "noctalia":
            self.palette_strip.set_palette({}, "dark")
            self.palette_info.set_title(os.path.basename(wallpaper) if wallpaper else "No wallpaper")
            self.palette_info.set_subtitle(
                "Noctalia draws the live palette. Choose another generator to see it here."
            )
            self._refresh_saved_palettes(wallpaper)
            return

        if wallpaper != self._generated_for:
            module = load_generator()
            if module is None:
                self.palette_strip.set_palette({}, "dark")
                self.palette_info.set_title("The palette generator is not installed")
                self.palette_info.set_subtitle("Run install.sh to put it next to the hook script.")
                self._refresh_saved_palettes(wallpaper)
                return
            try:
                self._preview = module.build_palette(
                    wallpaper,
                    variant=self._variant(),
                    seed=self._seed(),
                    random_mode=(generator == "random"),
                )
            except Exception as exc:
                self.palette_strip.set_palette({}, "dark")
                self.palette_info.set_title("The palette could not be drawn")
                self.palette_info.set_subtitle(str(exc))
                self._refresh_saved_palettes(wallpaper)
                return
            self._generated_for = wallpaper

        self._refresh_palette_strip(self._preview, wallpaper)
        self._refresh_saved_palettes(wallpaper)

    def _refresh_palette_strip(self, generated: dict, wallpaper: str) -> None:
        """Show the palette on screen.

        A palette that was just drawn wins, because that is the one the person
        is looking at. Otherwise the live palette is shown, so the window
        always tells the truth about what is on the desktop.
        """
        candidate = getattr(self, "_candidate", None)
        if candidate:
            data = candidate
            source = "A new palette you drew"
        else:
            name, data = self._active_palette_data()
            if data:
                source = f"The live palette, {name}"
            elif generated:
                data = generated
                source = "A palette drawn from the wallpaper"
            else:
                data = {}
                source = "No palette file to show"

        mode = self._preview_mode()
        self.palette_strip.set_palette(data, mode)
        self.palette_info.set_title(os.path.basename(wallpaper) if wallpaper else "No wallpaper")
        self.palette_info.set_subtitle(f"{source} - {self._composition_of(data)} - the {mode} side")

    def _preview_mode(self) -> str:
        mode = ""
        try:
            done = subprocess.run(
                ["noctalia", "msg", "theme-mode-get"], capture_output=True, text=True, timeout=10
            )
            mode = done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return mode if mode in ("light", "dark") else "dark"

    def _load_palette_file(self, name: str) -> dict:
        path = os.path.join(palette_dir(), f"{name}.json")
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _active_palette_data(self) -> tuple[str, dict]:
        """The palette Noctalia is using, and what is in it."""
        name = self._active_palette()
        if not name:
            return "", {}
        return name, self._load_palette_file(name)

    def _composition_of(self, data: dict) -> str:
        """How a palette was made, in words."""
        if not data:
            return "Not a file this window knows"
        parts = []
        generator = data.get("generator")
        if generator:
            parts.append("random" if generator == "random" else "drawn by this tool")
        variant = data.get("variant")
        if variant:
            parts.append(variant)
        if "seed" in data:
            parts.append(f"seed {data['seed']}")
        if not parts:
            parts.append("made by Noctalia or by hand")
        return ", ".join(parts)

    def _on_apply_candidate(self) -> None:
        """Write the palette on screen over the live one and apply it."""
        candidate = getattr(self, "_candidate", None)
        if not candidate:
            self.toasts.add_toast(Adw.Toast.new("There is no new palette to apply"))
            return
        name = "ours-random" if self._generator() == "random" else "ours"
        self._write_palette_to(name, candidate, self._current_wallpaper(), saved=False)
        self._apply_palette(name)
        self._candidate = {}
        self._update_palette_visibility()
        self._refresh_palette()

    def _on_generate(self) -> None:
        """Draw another palette from the same wallpaper and show it."""
        self.cfg.set("PALETTE_SEED", str(self._seed() + 1))
        self.save_config(silent=True)
        module = load_generator()
        if module is None:
            self.toasts.add_toast(Adw.Toast.new("The palette generator is not installed"))
            return
        try:
            self._candidate = module.build_palette(
                self._current_wallpaper(),
                variant=self._variant(),
                seed=self._seed(),
                random_mode=(self._generator() == "random"),
            )
        except Exception as exc:
            self.toasts.add_toast(Adw.Toast.new(f"Could not draw a palette: {exc}"))
            return
        self._update_palette_visibility()
        self._refresh_palette()
        self.toasts.add_toast(Adw.Toast.new("Drew a new palette"))

    def _on_save_palette(self) -> None:
        """Write the palette that is on screen into the palettes folder."""
        palette = getattr(self, "_preview", None)
        if not palette:
            self.toasts.add_toast(Adw.Toast.new("There is no palette to save yet"))
            return
        wallpaper = self._current_wallpaper()
        stem = os.path.splitext(os.path.basename(wallpaper))[0] if wallpaper else "wallpaper"
        suggested = f"{stem}-r{self._seed()}"

        dialog = Adw.AlertDialog(
            heading="Save this palette",
            body="The name shows up in Noctalia and in this window. A name that starts with the wallpaper name groups it under that wallpaper.",
        )
        entry = Adw.EntryRow(title="Name")
        entry.set_text(suggested)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_extra_child(entry)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(dlg, response):
            if response != "save":
                return
            name = entry.get_text().strip()
            if not name:
                self.toasts.add_toast(Adw.Toast.new("The name cannot be empty"))
                return
            self._write_palette(name, palette, wallpaper)
            dlg.close()

        dialog.connect("response", on_response)
        dialog.present(self)

    def _write_palette_to(self, name: str, palette: dict, wallpaper: str, saved: bool) -> None:
        """Write a palette into the folder Noctalia reads."""
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name).strip("-")
        if not safe:
            self.toasts.add_toast(Adw.Toast.new("That name cannot be used"))
            return
        folder = palette_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{safe}.json")
            payload = dict(palette)
            # Noctalia ignores keys it does not know. The first records which
            # wallpaper the palette came from, and the second marks it as one
            # the person kept rather than the live file the generator writes.
            payload["wallpaper"] = wallpaper
            if saved:
                payload["saved"] = True
            else:
                payload.pop("saved", None)
            # Record how it was made, even for a palette that was made before
            # this was recorded.
            payload.setdefault("generator", "random" if self._generator() == "random" else "ours")
            payload.setdefault("variant", self._variant())
            payload.setdefault("seed", self._seed())
            temporary = f"{path}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        except OSError as exc:
            self.toasts.add_toast(Adw.Toast.new(f"Could not save: {exc}"))
            return
        self._refresh_saved_palettes(wallpaper)
        return safe

    def _write_palette(self, name: str, palette: dict, wallpaper: str) -> None:
        safe = self._write_palette_to(name, palette, wallpaper, saved=True)
        if not safe:
            return
        self.toasts.add_toast(Adw.Toast.new(f"Saved {safe}"))
        self._apply_palette(safe)

    def _saved_palettes(self) -> list[tuple[str, str, bool, dict]]:
        """Every palette file, with the wallpaper it came from and whether it was saved."""
        folder = palette_dir()
        found: list[tuple[str, str, bool, dict]] = []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return found
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(folder, name)
            stem = name[:-5]
            wallpaper = ""
            was_saved = False
            data: dict = {}
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                wallpaper = data.get("wallpaper", "")
                was_saved = data.get("saved") is True
            except (OSError, ValueError):
                data = {}
            found.append((stem, wallpaper, was_saved, data))
        return found

    def _refresh_saved_palettes(self, wallpaper: str) -> None:
        # An AdwPreferencesGroup keeps its rows inside a box of its own, so
        # get_first_child() gives that box and not a row. Removing it would
        # break the group, so the rows that were added are remembered and
        # removed by name.
        for group, rows in ((self.mine_group, self._mine_rows), (self.all_group, self._all_rows)):
            for row in rows:
                group.remove(row)
            rows.clear()
        self._dots.clear()

        stem = os.path.splitext(os.path.basename(wallpaper))[0] if wallpaper else ""
        saved = self._saved_palettes()
        # The ones the person kept for this wallpaper go in the first list. The
        # live file the generator writes is not one of them, because it is
        # replaced every time the wallpaper changes.
        mine = [(name, wp, data) for name, wp, was_saved, data in saved
                if was_saved and stem and name.startswith(stem)]
        theirs = [(name, wp, data) for name, wp, _s, data in saved
                  if (name, wp) not in [(n, w) for n, w, _d in mine]]

        for items, group, rows in (
            (mine, self.mine_group, self._mine_rows),
            (theirs, self.all_group, self._all_rows),
        ):
            for name, wp, data in items:
                row = Adw.ActionRow(title=name)
                dots = PaletteDots()
                dots.set_palette(data, self._preview_mode())
                row.add_prefix(dots)
                self._dots.append(dots)
                details = []
                made = self._composition_of(data)
                if made:
                    details.append(made)
                if wp:
                    details.append(os.path.basename(wp))
                row.set_subtitle(", ".join(details) or "No wallpaper recorded")
                apply_button = Gtk.Button(label="Use")
                apply_button.set_valign(Gtk.Align.CENTER)
                apply_button.add_css_class("suggested-action")
                apply_button.connect("clicked", lambda _b, n=name: self._apply_palette(n))
                row.add_suffix(apply_button)
                delete_button = Gtk.Button(label="Delete")
                delete_button.set_valign(Gtk.Align.CENTER)
                delete_button.add_css_class("destructive-action")
                delete_button.connect("clicked", lambda _b, n=name: self._delete_palette(n))
                row.add_suffix(delete_button)
                group.add(row)
                rows.append(row)

        if not mine:
            row = Adw.ActionRow(title="Nothing saved for this wallpaper yet")
            row.set_subtitle("Draw one and save it to keep it here")
            self.mine_group.add(row)
            self._mine_rows.append(row)
        if not theirs:
            row = Adw.ActionRow(title="No other custom palettes")
            self.all_group.add(row)
            self._all_rows.append(row)

    def _apply_palette(self, name: str) -> None:
        try:
            done = subprocess.run(
                ["noctalia", "msg", "color-scheme-set", "custom", name],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.toasts.add_toast(Adw.Toast.new(f"Could not apply: {exc}"))
            return
        if done.returncode == 0:
            self.toasts.add_toast(Adw.Toast.new(f"Using {name}"))
        else:
            self.toasts.add_toast(Adw.Toast.new("Noctalia would not take that palette"))

    def _active_palette(self) -> str:
        """The name of the palette Noctalia is using, or an empty string."""
        try:
            done = subprocess.run(
                ["noctalia", "msg", "color-scheme-get"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        parts = done.stdout.strip().split()
        if len(parts) == 2 and parts[0] == "custom":
            return parts[1]
        return ""

    def _delete_palette(self, name: str) -> None:
        path = os.path.join(palette_dir(), f"{name}.json")
        was_active = self._active_palette() == name
        try:
            os.remove(path)
        except OSError as exc:
            self.toasts.add_toast(Adw.Toast.new(f"Could not delete: {exc}"))
            return
        if was_active:
            # Noctalia would keep pointing at a file that is no longer there,
            # and it would quietly fall back to a built-in. Put it back on the
            # palette the generator draws.
            self._apply_palette("ours")
        self.toasts.add_toast(Adw.Toast.new(f"Deleted {name}"))
        self._refresh_saved_palettes(self._current_wallpaper())

    # --------------------------------------------------------------- events
    def _on_strategy_changed(self, *_args) -> None:
        self._update_visibility()
        if not self._loading:
            self.save_config(silent=True)

    def _on_lock_changed(self, *_args) -> None:
        # Switching to a separate lock screen image starts from the desktop
        # choice, so the two pages do not open in a half-empty state.
        if self.lock_source.get_selected() == 1:
            for lock_picker, desktop_picker in zip(self.lock_pickers, self.pickers):
                if not lock_picker.current_file() and desktop_picker.current_file():
                    lock_picker.set_file(desktop_picker.current_file())
        if not self._loading:
            self.save_config(silent=True)
        self._update_visibility()

    def _on_change(self, _picker) -> None:
        if self._loading:
            return
        # Someone may have edited the file in another editor while this
        # window was open. Re-read first so their edit survives.
        if self.cfg.reload_if_changed():
            self.load_config()
            return
        self.save_config(silent=True)

    def _on_open_file(self, _button) -> None:
        uri = GLib.filename_to_uri(os.path.abspath(self.conf_path), None)
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error:
            self.toasts.add_toast(Adw.Toast.new(f"Path: {self.conf_path}"))

    def _update_mode_label(self) -> None:
        mode = current_theme_mode()
        if mode in ("light", "dark"):
            self.mode_row.set_title(f"Noctalia is currently in {mode}")
            self.mode_row.set_subtitle("Apply now sets the wallpaper for that mode")
        else:
            self.mode_row.set_title(f"Noctalia reports mode {mode}")
            self.mode_row.set_subtitle("The hook needs light or dark to pick an image")
        self._sync_color_scheme(mode)

    def _sync_color_scheme(self, mode: str) -> None:
        """Follow Noctalia's mode, so the window matches the desktop it configures.

        AdwStyleManager is the supported route. Setting the old GtkSettings
        prefer-dark-theme key is unsupported under libadwaita and warns.
        """
        manager = Adw.StyleManager.get_default()
        if mode == "dark":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        elif mode == "light":
            manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        else:
            manager.set_color_scheme(Adw.ColorScheme.DEFAULT)

    # --------------------------------------------------------------- saving
    def save_config(self, silent: bool = False) -> None:
        if not self._loading and self.cfg.reload_if_changed():
            self.load_config()
            return
        strategy = self._strategy()
        self.cfg.set("STRATEGY", strategy)

        for picker in self.pickers:
            if strategy in ("random", "cycle"):
                self.cfg.set(picker.folder_key, picker.current_folder())
            self.cfg.set(picker.file_key, picker.current_file())

        lock_on = self.lock_sync.get_active()
        custom = self.lock_source.get_selected() == 1
        self.cfg.set("LOCKSCREEN_SYNC", "true" if lock_on else "false")
        self.cfg.set("LOCKSCREEN_SOURCE", "custom" if custom else "desktop")
        for picker in self.lock_pickers:
            if lock_on and custom:
                self.cfg.set(picker.file_key, picker.current_file())

        self.cfg.set("PALETTE_GENERATOR", self._generator())
        self.cfg.set("PALETTE_VARIANT", self._variant())
        self.cfg.set("PALETTE_SEED", str(self._seed()))
        self.cfg.set("PALETTE_SAVED", self._saved_behaviour())

        try:
            self.cfg.save()
        except OSError as exc:
            self.toasts.add_toast(Adw.Toast.new(f"Could not save: {exc}"))
            return
        if not silent:
            self.toasts.add_toast(Adw.Toast.new(f"Saved to {self.conf_path}"))

    # -------------------------------------------------------------- actions
    def _apply(self, dry_run: bool) -> None:
        self.save_config(silent=True)
        if not os.path.isfile(self.hook_path):
            self.toasts.add_toast(Adw.Toast.new(f"Hook not found: {self.hook_path}"))
            return
        self._update_mode_label()
        code, output = run_hook(self.hook_path, self.conf_path, dry_run)
        if dry_run:
            self.toasts.add_toast(Adw.Toast.new("Dry run finished"))
        elif code == 0:
            self.toasts.add_toast(Adw.Toast.new("Wallpaper applied"))
        else:
            self.toasts.add_toast(Adw.Toast.new("The hook reported a problem"))
        self._show_output(output, error=(code != 0 and not dry_run))

    def _show_output(self, text: str, error: bool = False) -> None:
        dialog = Adw.Window(title="Hook output", modal=True, transient_for=self)
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )

        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text)
        if error:
            view.add_css_class("error")
        box.append(Gtk.ScrolledWindow(vexpand=True, min_content_height=260, child=view))

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        copy_button = Gtk.Button(label="Copy")
        copy_button.connect("clicked", lambda _b: view.get_clipboard().set(text))
        close_button = Gtk.Button(label="Close")
        close_button.add_css_class("suggested-action")
        close_button.connect("clicked", lambda _b: dialog.close())
        buttons.append(copy_button)
        buttons.append(close_button)
        box.append(buttons)

        dialog.set_content(box)
        dialog.present()


class ThemeWallpaperApp(Adw.Application):
    def __init__(self, conf_path: str, hook_path: str):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.conf_path = conf_path
        self.hook_path = hook_path

    def do_activate(self) -> None:
        window = ThemeWallpaperWindow(self, self.conf_path, self.hook_path)
        window.load_config()
        window.present()


def main() -> int:
    conf_path = os.environ.get("THEME_WALLPAPER_CONF", DEFAULT_CONF)
    hook_path = os.environ.get("THEME_WALLPAPER_HOOK", DEFAULT_HOOK)
    return ThemeWallpaperApp(conf_path, hook_path).run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
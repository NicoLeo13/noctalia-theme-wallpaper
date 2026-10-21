# Noctalia theme → wallpaper switcher

Makes the desktop wallpaper follow Noctalia's resolved theme mode: a light
wallpaper in light mode, a dark wallpaper in dark mode. Reacts to both manual
toggles and the automatic sunrise/sunset flip when `[theme].mode = "auto"`.

Works on Noctalia 5.2+ (plugin API irrelevant — this uses the shell hook system,
not the plugin API).

## How it works

Noctalia fires a `theme_mode_changed` hook whenever the resolved mode flips
between `light` and `dark`, and exports `$NOCTALIA_THEME_MODE`. A small script
maps that mode to a wallpaper you choose and applies it with
`noctalia msg wallpaper-set`.

It is fully event-driven: no polling, no background process, no wakeups.

```
GNOME/KDE color-scheme, control center, theme-mode-toggle, sunrise/sunset
        │
        ▼
  Noctalia [theme].mode ──► theme_mode_changed hook
                                  │
                                  ▼
                        theme-wallpaper.sh
                                  │
                    STRATEGY = fixed | random | cycle
                                  │
                                  ▼
                    noctalia msg wallpaper-set <path>
```

## Install

```sh
./install.sh
```

Then either open **Noctalia Theme Wallpaper** from the app menu, or edit the file
by hand:

```sh
vim ~/.config/noctalia/theme-wallpaper.conf
```

Test without changing anything:

```sh
THEME_WALLPAPER_DRY_RUN=1 ~/.config/noctalia/hooks/theme-wallpaper.sh
noctalia msg theme-mode-toggle
```

Run the hook only if you changed Noctalia settings. The wallpaper hook file is
read on every switch, so ordinary config edits need no reload.

To remove:

```sh
./install.sh --uninstall
```

## Configuration

`~/.config/noctalia/theme-wallpaper.conf`

| Key | Meaning |
| --- | --- |
| `STRATEGY` | `fixed`, `random`, or `cycle` |
| `DARK_WALLPAPER` / `LIGHT_WALLPAPER` | exact image per mode, for `fixed` |
| `DARK_DIR` / `LIGHT_DIR` | folder to scan per mode, for `random` / `cycle` |
| `CONNECTORS` | optional space-separated outputs; empty = all outputs in sync |
| `LOCKSCREEN_SYNC` | `false` leaves the KDE lock screen alone entirely |
| `LOCKSCREEN_SOURCE` | `desktop` (default) or `custom` |
| `LOCKSCREEN_DARK_WALLPAPER` / `LOCKSCREEN_LIGHT_WALLPAPER` | lock screen image per mode, for `custom` |

Both strategies you asked for are supported:

- **`fixed`** — one specific image per mode.
- **`random`** — a fresh random image from that mode's folder on every switch.
- **`cycle`** — bonus: walks the folder in order and wraps, so no image repeats
  until every one has been used.

For `random` and `cycle`, create the folders and put images in them:

```sh
mkdir -p ~/Pictures/Wallpapers/{dark,light}
```

Only image files directly inside each folder are used; subfolders are ignored.

## KDE lock screen

On KDE Plasma the lock screen is `kscreenlocker_greet`. It reads its background
from the `[Greeter][Wallpaper][org.kde.image][General]` group in
`~/.config/kscreenlockerrc`, which is the same block KDE's own Screen Locking
settings page writes. There is no separate tool. The hook rewrites the `Image=`
and `PreviewImage=` keys in place.

Because `kscreenlocker_greet` starts fresh each time the screen locks, writing the
file before a lock is enough. No reload and no extra process is involved.

The lock screen follows **every** wallpaper change, not only a theme mode
change. The hook listens for Noctalia's `wallpaper_changed` event, so picking a
new wallpaper in the Noctalia panel, with a plugin, or with another tool all
update the lock screen too. When the wallpaper was changed by something else,
the hook reconciles the lock screen and leaves the desktop wallpaper alone.

`LOCKSCREEN_SOURCE` chooses the behavior:

- `desktop` (default) — the lock screen shows the same image as the desktop. There
  is nothing extra to configure and the two can never disagree.
- `custom` — the lock screen uses its own image per mode. An empty value falls
  back to the desktop wallpaper for that mode, so a half-filled setup still works.

The write is atomic and preserves every other key in the file, because KDE also
writes this file from its own settings page. Atomic here means a reader never
sees a half-written file. It does not stop KDE and this script from writing at
the same instant, which is extremely unlikely but not impossible. Spaces and `%` in a path are
percent-encoded, since KDE stores a URL. If `kscreenlockerrc` does not exist the
step is skipped rather than fabricating a KDE config.

This only applies to KDE's lock screen. For the Noctalia lock screen, enable
`[lockscreen]` and leave its `wallpaper` key empty, and it uses the desktop
wallpaper per output, which this hook already switches. Note that enabling
`[lockscreen]` hands session locking to Noctalia, and its blurred-desktop
background needs screencopy, which KWin does not provide.

## Palette

The tool can draw the colour palette from the wallpaper as well, so the whole
desktop shares one set of colours.

```toml
PALETTE_GENERATOR = noctalia   # noctalia | ours | random
PALETTE_SCHEME = m3-rainbow    # used when the generator is noctalia
PALETTE_VARIANT = duotone      # duotone | tonal
PALETTE_SEED = 0               # for the random generator
```

| Generator | What it does |
| --- | --- |
| `noctalia` | Noctalia draws the palette, with the scheme named in `PALETTE_SCHEME`. The tool does not touch the palette. |
| `ours` | The tool draws a palette from the wallpaper and applies it as a Noctalia custom palette. |
| `random` | The same, but the accent colours move around the hue of the wallpaper, so each wallpaper still feels like itself. |

`PALETTE_VARIANT` decides how the two modes relate to the picture. `duotone`
keeps the picture's real hues, with light washed out and dark deep. `tonal` uses
one seed hue for all three accents, the way Noctalia's own schemes do.

The palette is a pure function of the wallpaper, so one file is enough. The file
holds a dark and a light variant, so a theme mode change needs no regeneration
at all. The generator guarantees the contrast of every colour pair it supplies
and refuses to write a palette that fails its own check, so a broken palette
never reaches the desktop.

Setting `PALETTE_GENERATOR = noctalia` hands the palette back to Noctalia
completely. That is also the ownership switch. While the generator is `ours` or
`random` the tool owns the palette source, and a change made in Noctalia's own
settings lasts only until the next wallpaper change.

## Design notes

**Why a hook and not a Luau plugin.** Noctalia plugins run as Luau services, and
a service's only lifecycle callbacks are `onConfigChanged()` and
`onOutputsChanged()` — there is no theme-change callback. A plugin would have to
poll `noctalia.isDarkMode()` on a timer. The hook system is event-driven, so this
approach reacts instantly with nothing running in the background.

**Why not the built-in `directory_light` / `directory_dark`.** Noctalia already
has theme-aware wallpaper folders, and it is the right tool when `shell_mode` is
pinned to `dark` or `light`. It does not work in `auto` mode — the docs state
Auto falls back to the plain `directory` — and it changes what folder the picker
*browses* rather than pinning a wallpaper per mode. This hook covers `auto` and
pins exact images.

**Startup.** `theme_mode_changed` only fires on an actual change, so it would
never run on a session that starts already in dark mode. The `started` hook
covers that; the script asks Noctalia for the current mode when
`$NOCTALIA_THEME_MODE` is empty.

**Safety.** Every failure path logs and exits without changing anything: a
missing config, a typo'd path, an empty folder, an unknown strategy, an
unreadable mode, or a failed `wallpaper-set`. A bad config can never blank the
desktop. Failures go to the journal:

```sh
journalctl -t noctalia-theme-wallpaper -f
```

**No loops.** Applying a wallpaper fires `wallpaper_changed`, not
`theme_mode_changed`, so the hook cannot retrigger itself. The script also skips
the IPC call when the target image is already applied, avoiding a redundant
reload and its transition flash.

**Reconciled, not short-circuited.** The desktop wallpaper and the lock screen
are two separate states that can drift apart, so the lock screen step runs even
when the desktop image is already applied and nothing needed changing.

**The lock screen write is a no-op when nothing changed.** If the file already
points at the right image, the hook leaves it completely alone. Rewriting an
identical file would give it a new inode, and that is what breaks the file watch
a desktop uses to notice a theme change.

**The hook listens for three events.** `theme_mode_changed` picks and applies
the image. `started` covers a session that boots already in a given mode.
`wallpaper_changed` covers a wallpaper change made by anything else, and it only
reconciles the lock screen so the script never changes the wallpaper back.

**Palette synergy.** Your `[theme].source = "wallpaper"` means Noctalia
regenerates the palette from the current wallpaper on every change. So the
wallpaper you pick per mode also drives your colors automatically.

## Graphical settings

An optional GTK 4 / libadwaita window edits the same config file. Start it from
the app menu as **Noctalia Theme Wallpaper**.

It gives you a thumbnail grid for picking the light and dark wallpaper, folder
pickers, the strategy selector, the lock screen mode, and **Apply now** and
**Dry run** buttons. Dry run shows exactly what the hook would do and changes
nothing.

The window follows Noctalia's current theme mode, so it is dark when Noctalia is
dark. The mode is also shown at the bottom, so it is clear which wallpaper
**Apply now** will set.

Saving needs no reload. The hook re-reads the config on every switch, so a saved
change is live at the next mode change.

The GUI needs only `python3-gobject`, GTK 4 and libadwaita, all of which are
already on Fedora by default. `install.sh` checks for them and skips the GUI if
they are missing. The hook never depends on the GUI.

### The palette window

The **Palette** tab shows the palette the current wallpaper produces, with a
swatch for every colour role. **Draw a new one** makes another palette from the
same wallpaper, and **Save this palette** keeps it.

A saved palette is written into `~/.config/noctalia/palettes/`, which is the
folder Noctalia reads. It therefore appears in Noctalia's own custom palette
list straight away, with a preview, and needs nothing else to work.

The window lists the saved palettes twice. **Saved for this wallpaper** holds
the ones that belong to the wallpaper on screen, and **All custom palettes**
holds every one, including the ones Noctalia or you made. Each row has a **Use**
button that applies it and a **Delete** button that removes it.

The name of a saved palette is up to you. A name that starts with the wallpaper
name groups it under that wallpaper.

### Config file handling

The GUI edits `KEY=VALUE` lines in place. It keeps your comments, blank lines and
key order, rewrites only the keys you changed, and appends new keys at the end.
Unknown keys are left alone. A duplicated key is collapsed to one.

The Python reader and the shell reader are deliberately identical, including
quote stripping and the one-layer `~` expansion, so the window and the hook read
the same value from the same file. That guarantee has limits worth naming. A
folder containing only symlinked images, a file name holding a newline, and
non-ASCII spaces are the cases where the two can still disagree, so a value
that looks wrong in one place and right in the other is worth reporting.

## Files

```
install.sh                              install / uninstall
files/hooks-theme-wallpaper.toml        [hooks] wiring (theme_mode_changed, started)
files/theme-wallpaper.sh                the script Noctalia runs
files/theme-wallpaper.conf.example      config template with docs
gui/theme-wallpaper-gui.py              GTK 4 / libadwaita settings window
gui/theme-wallpaper-gui.desktop         app menu launcher
```

The hooks live in their own `hooks-theme-wallpaper.toml` so they merge into the
Noctalia config stack without editing any existing `*.toml`.

## Notes

- Noctalia's `auto` mode follows **sunrise/sunset** from your `[location]`, not
  the GNOME or KDE color scheme. This switcher follows Noctalia's mode. If you
  change appearance in KDE's System Settings, that does not move Noctalia's mode.
- With `[theme].mode = "auto"`, the flip happens once at sunrise and once at
  sunset. If you want different wallpapers at other times of day, that is a
  different feature — say the word and it can be added.
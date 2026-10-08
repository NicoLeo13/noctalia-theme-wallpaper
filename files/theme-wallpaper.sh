#!/usr/bin/env bash
#
# theme-wallpaper.sh -- apply a wallpaper that matches Noctalia's resolved theme mode.
#
# Wired to Noctalia's `theme_mode_changed` and `started` hooks. Reads the resolved
# mode from $NOCTALIA_THEME_MODE (falling back to `noctalia msg theme-mode-get`) and
# applies the wallpaper configured for that mode.
#
# Config: ~/.config/noctalia/theme-wallpaper.conf  (see the .example file)
#
# Set THEME_WALLPAPER_DRY_RUN=1 to log the decision without touching the wallpaper.

# Deliberately no `set -e`: every failure path here should log and exit quietly
# rather than kill the hook, and a missing wallpaper must never blank the desktop.
set -uo pipefail

CONF="${THEME_WALLPAPER_CONF:-$HOME/.config/noctalia/theme-wallpaper.conf}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/noctalia"
LOG_TAG="noctalia-theme-wallpaper"
DRY_RUN="${THEME_WALLPAPER_DRY_RUN:-0}"

log() {
	# Always write to the journal, including dry runs. The README tells a
	# user to debug with the journal, so a dry run that logs nowhere makes
	# the tool that exists for debugging useless.
	logger -t "$LOG_TAG" -- "$*" 2>/dev/null || true
	printf '%s\n' "$*"
}

# Skip everything: never blank the desktop because of a config typo.
bail() {
	log "skipped: $*"
	exit 0
}

declare -A CFG=()

load_conf() {
	[[ -r "$CONF" ]] || return 1
	local line key val
	while IFS= read -r line || [[ -n "$line" ]]; do
		line="${line#"${line%%[![:space:]]*}"}" # trim leading space
		[[ -z "$line" || "$line" == \#* ]] && continue
		[[ "$line" != *=* ]] && continue
		key="${line%%=*}"
		val="${line#*=}"
		key="${key%"${key##*[![:space:]]}"}" # trim trailing space
		val="${val#"${val%%[![:space:]]*}"}"
		val="${val%"${val##*[![:space:]]}"}"
		# strip one layer of matching quotes
		if [[ ${#val} -ge 2 && "${val:0:1}" == '"' && "${val: -1}" == '"' ]]; then
			val="${val:1:${#val}-2}"
		elif [[ ${#val} -ge 2 && "${val:0:1}" == "'" && "${val: -1}" == "'" ]]; then
			val="${val:1:${#val}-2}"
		fi
		[[ -n "$key" ]] && CFG["$key"]="$val"
	done <"$CONF"
	return 0
}

# cfg <key> <default>
cfg() {
	local v="${CFG[$1]-}"
	if [[ -n "$v" ]]; then
		printf '%s' "$v"
	else
		printf '%s' "$2"
	fi
}

# A leading ~/ in a configured path should behave the way the Noctalia settings UI
# implies it does, so expand it here instead of failing with "no such file".
expand() {
	local p="$1"
	if [[ "$p" == "~/"* ]]; then
		printf '%s/%s' "$HOME" "${p#\~/}"
	elif [[ "$p" == "~" ]]; then
		printf '%s' "$HOME"
	else
		printf '%s' "$p"
	fi
}

load_conf || bail "config not readable: $CONF"

# ---------------------------------------------------------------- resolve mode
# `theme_mode_changed` provides this. The `started` hook does not, so ask Noctalia.
mode="${NOCTALIA_THEME_MODE:-}"
if [[ -z "$mode" ]]; then
	mode="$(noctalia msg theme-mode-get 2>/dev/null | tr -d '[:space:]')"
fi
case "$mode" in
dark | light) ;;
*) bail "could not resolve theme mode (got '${mode:-empty}')" ;;
esac

if [[ "$mode" == "dark" ]]; then
	dir_key="DARK_DIR"
	file_key="DARK_WALLPAPER"
else
	dir_key="LIGHT_DIR"
	file_key="LIGHT_WALLPAPER"
fi

strategy="$(cfg STRATEGY fixed)"
strategy="${strategy,,}"

# ---------------------------------------------------------------- pick wallpaper
chosen=""
index_state=""
images=()
outputs=()
keep=()
img=""

case "$strategy" in
fixed)
	chosen="$(cfg "$file_key" "")"
	[[ -n "$chosen" ]] || bail "fixed: $file_key is not set"
	chosen="$(expand "$chosen")"
	;;

random | cycle)
	dir="$(cfg "$dir_key" "")"
	[[ -n "$dir" ]] || bail "$strategy: $dir_key is not set"
	dir="$(expand "$dir")"
	[[ -d "$dir" ]] || bail "$strategy: not a directory: $dir"

	# Deterministic order so `cycle` is stable across invocations.
	# -print0 and sort -z, because a file name may contain a newline and
	# a line-based pipeline would split one file into two fake entries.
	# -type f -o -type l keeps symlinked wallpapers, which a dotfiles setup
	# relies on. Broken symlinks are filtered out by the test below.
	mapfile -d '' -t images < <(
		find "$dir" -maxdepth 1 \( -type f -o -type l \) \
			\( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \
			-o -iname '*.webp' -o -iname '*.jxl' -o -iname '*.avif' \) -print0 2>/dev/null |
			LC_ALL=C sort -z
	)
	# Drop anything that does not resolve to a readable file.
	keep=()
	for img in ${images[@]+"${images[@]}"}; do
		[[ -f "$img" && -r "$img" ]] && keep+=("$img")
	done
	images=(${keep[@]+"${keep[@]}"})
	[[ ${#images[@]} -gt 0 ]] || bail "$strategy: no images in $dir"

	if [[ "$strategy" == "random" ]]; then
		chosen="${images[RANDOM % ${#images[@]}]}"
	else
		# cycle: advance through the folder, wrapping, so no repeat until the end
		index_state="$STATE_DIR/theme-wallpaper-$mode.idx"
		mkdir -p "$STATE_DIR" 2>/dev/null || true
		last="-1"
		if [[ -r "$index_state" ]]; then
			read -r last <"$index_state" || last="-1"
		fi
		[[ "$last" =~ ^-?[0-9]+$ ]] || last="-1"
		idx=$(((last + 1) % ${#images[@]}))
		if ! printf '%s\n' "$idx" >"$index_state" 2>/dev/null; then
			log "cycle: cannot write $index_state, so the order will not advance"
		fi
		chosen="${images[$idx]}"
	fi
	;;

*)
	bail "unknown STRATEGY '${strategy}' (use fixed, random, or cycle)"
	;;
esac

[[ -n "$chosen" && -f "$chosen" ]] || bail "chosen wallpaper is not a readable file: ${chosen:-empty}"
[[ -r "$chosen" ]] || bail "chosen wallpaper is not readable: $chosen"

# ------------------------------------------------------------ KDE lock screen
# kscreenlocker_greet reads its background from the Greeter group in
# kscreenlockerrc every time the screen locks, so writing the file before a lock
# is enough. There is no reload step and no daemon.

LOCKSCREEN_RC="${THEME_WALLPAPER_LOCKSCREEN_RC:-$HOME/.config/kscreenlockerrc}"
LOCKSCREEN_GROUP='[Greeter][Wallpaper][org.kde.image][General]'

# KDE stores a URL, so encode the two characters that appear in real paths.
url_encode() {
	local s="$1" out="" i c
	for ((i = 0; i < ${#s}; i++)); do
		c="${s:i:1}"
		case "$c" in
		' ' | '%' | '#' | '?' | '"' | '<' | '>' | '\\' | '^' | '`' | '{' | '}' | '|' | $'\n')
			printf -v c '%%%02X' "'$c"
			;;
		esac
		out+="$c"
	done
	printf '%s' "$out"
}

# Rewrite Image= and PreviewImage= inside the Greeter group, creating the group
# when it is absent. Every other key in the file is preserved, and the write is
# atomic, because KDE also writes this file from its own settings page.
set_lockscreen_image() {
	local image="$1"
	local file="$LOCKSCREEN_RC"
	local url="file://$(url_encode "$image")"
	local -a lines new
	local i saw_group=0 saw_image=0 saw_prev=0 in_group=0
	local tmp perms dir base target

	# Refuse anything that does not look like a settings file, so a stray
	# THEME_WALLPAPER_LOCKSCREEN_RC cannot append INI to an unrelated file.
	base="$(basename -- "$file")"
	case "$base" in
	*.rc | *.conf | kscreenlockerrc) ;;
	*) log "lockscreen: $base does not look like a settings file (skipped)"; return 0 ;;
	esac

	# Follow a symlink and write to the real file. Without this, mv would
	# replace the link itself, and stat would report the link mode, which
	# is always 0777, leaving a world-writable settings file behind.
	target="$(readlink -f -- "$file" 2>/dev/null || printf '%s' "$file")"
	[[ -f "$target" ]] || {
		log "lockscreen: no $file (skipped)"
		return 0
	}

	mapfile -t lines <"$target" || {
		log "lockscreen: cannot read $file (skipped)"
		return 0
	}

	# Rewrite every matching group, not just the first. KConfig merges
	# duplicate sections and the last value wins, so leaving a second copy
	# untouched would silently keep the old image.
	for ((i = 0; i < ${#lines[@]}; i++)); do
		local stripped="${lines[i]%$'\r'}"
		stripped="${stripped// /}"
		if [[ "$stripped" == \[* ]]; then
			if [[ "$stripped" == "$LOCKSCREEN_GROUP" ]]; then
				in_group=1
				saw_group=1
				new+=("${stripped}")
				continue
			fi
			in_group=0
			new+=("${lines[i]}")
			continue
		fi

		if ((in_group)); then
			case "${lines[i]%$'\r'}" in
			Image=*)
				new+=("Image=$url")
				saw_image=1
				continue
				;;
			PreviewImage=*)
				new+=("PreviewImage=$url")
				saw_prev=1
				continue
				;;
			esac
		fi
		new+=("${lines[i]}")
	done

	if ((saw_group == 0)); then
		while [[ ${#new[@]} -gt 0 && -z "${new[-1]//[[:space:]]/}" ]]; do
			new=("${new[@]:0:${#new[@]}-1}")
		done
		new+=("" "$LOCKSCREEN_GROUP")
	elif ((saw_image == 0 || saw_prev == 0)); then
		# Add the missing keys right after the group header.
		local -a merged=()
		local inside=0
		for line in ${new[@]+"${new[@]}"}; do
			merged+=("$line")
			if ((inside == 0)); then
				[[ "${line// /}" == "$LOCKSCREEN_GROUP" ]] && inside=1
			elif [[ "$line" == \[* ]]; then
				inside=2
			fi
		done
		if [[ $inside == 1 ]]; then
			((saw_image == 0)) && merged+=("Image=$url")
			((saw_prev == 0)) && merged+=("PreviewImage=$url")
		fi
		new=(${merged[@]+"${merged[@]}"})
	fi

	# Write beside the real file so the rename stays on one filesystem and
	# stays atomic.
	dir="$(dirname -- "$target")"
	tmp="$dir/.${base}.twp.$$"
	if ! printf '%s
' "${new[@]}" >"$tmp"; then
		rm -f "$tmp"
		log "lockscreen: cannot write $file (skipped)"
		return 0
	fi

	perms="$(stat -Lc '%a' -- "$target" 2>/dev/null || echo 600)"
	if ! mv -f -- "$tmp" "$target"; then
		rm -f "$tmp"
		log "lockscreen: cannot replace $file (skipped)"
		return 0
	fi
	chmod "$perms" -- "$target" 2>/dev/null || true
	log "lockscreen: set -> $(basename "$image")"
	return 0
}

sync_lockscreen() {
	local mode="$1" desktop_image="$2" lock_image lock_source lv

	[[ "$(cfg LOCKSCREEN_SYNC true)" != "false" ]] || return 0

	lock_image="$desktop_image"
	lock_source="$(cfg LOCKSCREEN_SOURCE desktop)"
	lock_source="${lock_source,,}"
	case "$lock_source" in
	desktop) ;;
	custom)
		if [[ "$mode" == "dark" ]]; then
			lv="$(cfg LOCKSCREEN_DARK_WALLPAPER "")"
		else
			lv="$(cfg LOCKSCREEN_LIGHT_WALLPAPER "")"
		fi
		if [[ -z "$lv" ]]; then
			log "lockscreen: custom source with no image for $mode, mirroring desktop"
		else
			lv="$(expand "$lv")"
			if [[ -f "$lv" && -r "$lv" ]]; then
				lock_image="$lv"
			else
				log "lockscreen: $lv unusable, mirroring desktop"
			fi
		fi
		;;
	*)
		# The desktop wallpaper is already applied at this point, so this is
		# an error, not a skip. Reporting "skipped" here would be false.
		log "error: unknown LOCKSCREEN_SOURCE '${lock_source}' (use desktop or custom)"
		return 0
		;;
	esac

	if [[ "$DRY_RUN" == "1" || "$DRY_RUN" == "true" ]]; then
		log "DRY RUN lockscreen ($lock_source): would point at $(basename "$lock_image")"
		return 0
	fi

	set_lockscreen_image "$lock_image"
}

# ---------------------------------------------------------------- apply
# The desktop wallpaper is skipped when it is already applied, but the lock
# screen is still reconciled, because the two can drift apart.
if [[ "$DRY_RUN" == "1" || "$DRY_RUN" == "true" ]]; then
	log "DRY RUN $mode ($strategy): would set $(basename "$chosen")"
else
	current="$(noctalia msg wallpaper-get 2>/dev/null | head -1 | tr -d '[:space:]')"
	if [[ -n "$current" && "$current" == "$chosen" ]]; then
		log "$mode: desktop wallpaper already applied ($(basename "$chosen"))"
	else
		connectors="$(cfg CONNECTORS "")"
		if [[ -n "$connectors" ]]; then
			ok=1
			# Split on spaces without globbing. A bare `for c in $var` would
			# also expand any `*` against the current directory.
			read -r -a outputs <<<"$connectors"
			for c in ${outputs[@]+"${outputs[@]}"}; do
				if noctalia msg wallpaper-set "$c" "$chosen" >/dev/null 2>&1; then
					log "$mode: set on $c -> $chosen"
				else
					log "$mode: FAILED on $c (is '$c' connected?)"
					ok=0
				fi
			done
			[[ "$ok" == "1" ]] || bail "one or more outputs failed"
		else
			if noctalia msg wallpaper-set "$chosen" >/dev/null 2>&1; then
				log "$mode: set -> $chosen"
			else
				bail "wallpaper-set failed for $chosen"
			fi
		fi
	fi
fi

sync_lockscreen "$mode" "$chosen"

exit 0
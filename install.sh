#!/usr/bin/env bash
# Utter installer and uninstaller -- the only part of Utter that touches the network.
#
#   install.sh [-y|--yes] [--no-autostart] [--dry-run] [--prefix DIR]
#   install.sh --uninstall [--purge] [--prefix DIR]
#
# What it installs, all inside the XDG roots (or inside --prefix, which maps every root
# there for a throwaway or staged install):
#
#   $XDG_DATA_HOME/utter/venv                          the venv and the installed package
#   $XDG_DATA_HOME/utter/models                        the two pinned release files
#   $XDG_DATA_HOME/applications/utter.desktop          launcher; the portal app-id is its basename
#   $XDG_DATA_HOME/icons/hicolor/scalable/apps/utter.svg
#   $XDG_CONFIG_HOME/autostart/utter.desktop           written unless --no-autostart
#   ~/.local/bin/utter                                 a shim that execs the venv binary
#
# Guarantees this script keeps:
#
#   * It never runs sudo. Anything missing is reported with the exact command to run.
#   * Nothing is downloaded before the consent gate, whose default answer is no.
#   * Both downloads resume (curl -C -), are checked against a pinned SHA-256 and only then
#     moved into place, so a partially downloaded file never appears in models/. A mismatch
#     deletes the staged file and exits non-zero. A file already in models/ whose digest
#     matches is reported and never fetched again.
#   * A digest override (UTTER_MODEL_SHA256 / UTTER_VOICES_SHA256) is honoured *only* when
#     the matching URL override (UTTER_MODEL_URL / UTTER_VOICES_URL) is also set. The pairing
#     is what keeps a stray digest variable from weakening a download that still points at
#     the pinned release; it is also how the test suite installs in seconds. The digests are
#     never overridable on their own, and the pinned values below are literals.
#   * --dry-run prints the plan -- the pinned digests, every target path, and whether each
#     artifact is already present or would be written -- and touches nothing at all, not even
#     the --prefix directory.
#   * Both pinned digests are recorded in the app's own config.toml -- created with the
#     app's Config schema when it is absent, its existing values kept when it is not --
#     because that is what the runtime and `utter doctor` verify the model files against.
#     --uninstall keeps that file unless --purge. Logs under $XDG_STATE_HOME/utter are
#     generated, not user state, so --uninstall removes them too.
#   * A --prefix install keeps every path inside --prefix, including its shim's XDG roots, so
#     the prefix never leaks into the real home. uv's managed CPython then lands under
#     $prefix/share/uv/python as well; that is the isolation working, not a stray file, and
#     --uninstall leaves it alone because it is uv's, not Utter's.
#
# Exit status: 0 installed or uninstalled, 1 declined or failed, 2 bad arguments or a
# missing prerequisite.

set -euo pipefail

readonly MODEL_NAME="kokoro-v1.0.int8.onnx"
readonly VOICES_NAME="voices-v1.0.bin"
readonly MODEL_URL_PINNED="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx"
readonly VOICES_URL_PINNED="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
# Measured on this machine against the cached files in ~/.local/share/utter/models/ (89.1 MB
# and 26.9 MB). These are the digests doctor and the runtime verify against.
readonly MODEL_SHA256_PINNED="6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb"
readonly VOICES_SHA256_PINNED="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"

say() { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit "${2:-1}"; }
ask() { # a prompt on a terminal stays on the line the answer will be typed on; a piped
    # stdin does not echo the newline, so there the prompt ends its own line instead.
    if [ -t 0 ]; then printf '%s ' "$1"; else printf '%s\n' "$1"; fi
}

usage() {
    cat <<'TEXT'
Utter installer

  install.sh [-y|--yes] [--no-autostart] [--dry-run] [--prefix DIR]
  install.sh --uninstall [--purge] [--prefix DIR]

  -y, --yes        answer yes to the consent gate and the autostart question
  --no-autostart   do not install an autostart entry
  --dry-run        print exactly what would change, and change nothing
  --prefix DIR     install entirely under DIR instead of the XDG roots
  --uninstall      remove the venv, models, launcher, icon, autostart entry and shim
  --purge          with --uninstall: remove config.toml as well
  -h, --help       this text
TEXT
}

assume_yes=no
autostart=prompt
dry_run=no
uninstall=no
purge=no
prefix=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        -y | --yes) assume_yes=yes ;;
        --no-autostart) autostart=no ;;
        --dry-run) dry_run=yes ;;
        --uninstall) uninstall=yes ;;
        --purge) purge=yes ;;
        --prefix)
            [ "$#" -ge 2 ] || die "--prefix needs a directory" 2
            prefix="$2"
            shift
            ;;
        --prefix=*) prefix="${1#--prefix=}" ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "unknown argument: $1" 2
            ;;
    esac
    shift
done

if [ "$purge" = yes ] && [ "$uninstall" = no ]; then
    die "--purge only makes sense with --uninstall" 2
fi

if [ -n "$prefix" ]; then
    case "$prefix" in
        /*) ;;
        *) prefix="$PWD/$prefix" ;;
    esac
    prefix="${prefix%/}"
    [ -n "$prefix" ] || die "--prefix must not be /" 2
    data_home="$prefix/share"
    config_home="$prefix/config"
    state_home="$prefix/state"
    bin_dir="$prefix/bin"
    # --prefix is authoritative for every root the installer writes. A stray UTTER_*
    # override in the caller's environment (the test suite sets them) must not redirect a
    # prefixed install halfway into the real home.
    unset UTTER_DATA_HOME UTTER_CONFIG_HOME UTTER_STATE_HOME UTTER_RUNTIME_DIR
else
    data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
    config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
    state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
    bin_dir="$HOME/.local/bin"
fi
export XDG_DATA_HOME="$data_home" XDG_CONFIG_HOME="$config_home" XDG_STATE_HOME="$state_home"

utter_data="$data_home/utter"
venv_dir="$utter_data/venv"
venv_binary="$venv_dir/bin/utter"
models_dir="$utter_data/models"
stage_dir="$utter_data/.stage"
applications_dir="$data_home/applications"
entry_path="$applications_dir/utter.desktop"
icon_dir="$data_home/icons/hicolor/scalable/apps"
icon_path="$icon_dir/utter.svg"
autostart_dir="$config_home/autostart"
autostart_path="$autostart_dir/utter.desktop"
config_path="$config_home/utter/config.toml"
shim_path="$bin_dir/utter"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
template="$script_dir/assets/utter.desktop.in"
icon_source="$script_dir/assets/utter.svg"

# Temp files live next to their destination so the final move is a rename within one
# filesystem. The trap removes whatever a failure left half-written; the staged download is
# deliberately *not* in this list, because a partial download is the resume point.
cleanup() {
    rm -f -- "$entry_path.tmp.$$" "$autostart_path.tmp.$$" "$icon_path.tmp.$$" "$shim_path.tmp.$$"
}
trap cleanup EXIT

model_url="$MODEL_URL_PINNED"
voices_url="$VOICES_URL_PINNED"
model_sha256="$MODEL_SHA256_PINNED"
voices_sha256="$VOICES_SHA256_PINNED"

# The pairing rule: a digest override counts only next to its URL override.
if [ -n "${UTTER_MODEL_URL:-}" ]; then
    model_url="$UTTER_MODEL_URL"
    if [ -n "${UTTER_MODEL_SHA256:-}" ]; then
        model_sha256="$UTTER_MODEL_SHA256"
    fi
fi
if [ -n "${UTTER_VOICES_URL:-}" ]; then
    voices_url="$UTTER_VOICES_URL"
    if [ -n "${UTTER_VOICES_SHA256:-}" ]; then
        voices_sha256="$UTTER_VOICES_SHA256"
    fi
fi

# --- preflight ------------------------------------------------------------------------

missing_required=""
missing_recommended=""

check_tool() { # name, command, required|recommended|optional
    local name="$1" command="$2" kind="$3" path
    if path="$(command -v "$command" 2>/dev/null)"; then
        printf '  %-14s ok          %s\n' "$name" "$path"
        return 0
    fi
    case "$kind" in
        required)
            printf '  %-14s MISSING\n' "$name"
            missing_required="$missing_required $command"
            ;;
        recommended)
            printf '  %-14s MISSING\n' "$name"
            missing_recommended="$missing_recommended $command"
            ;;
        optional)
            printf '  %-14s not installed (optional: espeakng-loader bundles the library and its data)\n' "$name"
            ;;
    esac
}

check_clipboard() {
    local command path
    for command in wl-paste xclip xsel; do
        if path="$(command -v "$command" 2>/dev/null)"; then
            printf '  %-14s ok          %s\n' "clipboard" "$path"
            return 0
        fi
    done
    printf '  %-14s MISSING (wl-clipboard on Wayland, xclip or xsel on X11)\n' "clipboard"
    missing_recommended="$missing_recommended wl-clipboard"
}

detect_process() { # first matching process name, or "unknown"
    local process
    if command -v pgrep >/dev/null 2>&1; then
        for process in "$@"; do
            if pgrep -x "$process" >/dev/null 2>&1; then
                printf '%s' "$process"
                return 0
            fi
        done
    fi
    printf 'unknown'
}

preflight_report() {
    say "Preflight"
    note "session:    ${XDG_SESSION_TYPE:-unknown}${XDG_CURRENT_DESKTOP:+ ($XDG_CURRENT_DESKTOP)}"
    if [ -n "${WAYLAND_DISPLAY:-}" ]; then
        note "display:    Wayland ($WAYLAND_DISPLAY)"
    elif [ -n "${DISPLAY:-}" ]; then
        note "display:    X11 ($DISPLAY)"
    else
        note "display:    none detected (the tray needs a desktop session; the CLI still works)"
    fi
    note "compositor: $(detect_process kwin_wayland mutter gnome-shell sway Hyprland Xorg)"
    note "audio:      $(detect_process pipewire pulseaudio)"
    check_tool uv uv required
    check_tool curl curl required
    check_clipboard
    check_tool espeak-ng espeak-ng optional
    if [ -n "$missing_required" ] || [ -n "$missing_recommended" ]; then
        say "  Install what is missing with:"
        if [ -n "$missing_required" ]; then
            printf '    sudo dnf install%s\n' "$missing_required"
        fi
        if [ -n "$missing_recommended" ]; then
            printf '    sudo dnf install%s\n' "$missing_recommended"
        fi
        say "  Utter never runs sudo; run those lines yourself and re-run this installer."
    fi
    say ""
}

# --- consent --------------------------------------------------------------------------

print_ledger() {
    say "Download ledger -- this is the only step of Utter that touches the network:"
    printf '  %-22s %8s   %s\n' "$MODEL_NAME" "88.07 MiB" "$model_url"
    printf '  %-22s %8s   %s\n' "$VOICES_NAME" "26.91 MiB" "$voices_url"
    printf '  %-22s %8s   %s\n' "CPython 3.12 interpreter" "~30 MiB" "(only if uv has not cached one)"
    printf '  %-22s %8s   %s\n' "Python wheels" "~250 MiB" "(PyQt6, onnxruntime and the rest)"
    note "A file already in $models_dir is verified, not downloaded again."
    note "Both files must match their pinned SHA-256 before they are installed:"
    printf '  %-22s %s\n' "$MODEL_NAME" "$model_sha256"
    printf '  %-22s %s\n' "$VOICES_NAME" "$voices_sha256"
}

consent_gate() {
    if [ "$assume_yes" = yes ]; then
        say "Consent: --yes given, proceeding without prompting."
        return 0
    fi
    ask 'Proceed with the download and the install? [y/N]'
    local answer=""
    read -r answer || answer=""
    case "$answer" in
        [yY] | [yY][eE][sS]) say "Consent given." ;;
        *)
            say "Declined: nothing was downloaded and nothing was installed."
            exit 1
            ;;
    esac
}

# --- steps ----------------------------------------------------------------------------

install_venv() {
    if [ "$dry_run" = yes ]; then
        if [ -x "$venv_binary" ]; then
            say "  already present  $venv_dir (the package would be re-installed to refresh it)"
        else
            say "  would write      $venv_dir (uv venv --python 3.12, then the package and its"
            say "                   dependencies from $script_dir/pyproject.toml)"
        fi
        return 0
    fi
    say "  venv             $venv_dir"
    if ! uv venv --allow-existing --python 3.12 "$venv_dir"; then
        die "could not create the venv at $venv_dir (uv needs CPython 3.12, which it fetches)" 1
    fi
    if ! uv pip install --python "$venv_dir/bin/python" "$script_dir"; then
        printf 'error: the package did not install into %s\n' "$venv_dir" >&2
        printf 'That venv is incomplete and no launcher was installed; re-run this installer\n' >&2
        printf 'once the cause is fixed, and it resumes from here.\n' >&2
        exit 1
    fi
}

install_model_file() { # url, sha256, name
    local url="$1" expected="$2" name="$3" dst="$models_dir/$3" part actual
    if [ -f "$dst" ]; then
        actual="$(sha256sum -- "$dst" | cut -d' ' -f1)"
        if [ "$actual" = "$expected" ]; then
            say "  already present  $dst (sha256 verified, not downloaded again)"
            return 0
        fi
        if [ "$dry_run" = yes ]; then
            say "  would write      $dst (the installed copy fails its digest: expected $expected,"
            say "                   found $actual) by downloading $name from $url, sha256 $expected"
            return 0
        fi
        warn "$dst does not match its expected digest (expected $expected, found $actual); fetching it again"
    fi
    if [ "$dry_run" = yes ]; then
        say "  would write      $dst (download, sha256 $expected)"
        say "                   from $url"
        say "                   resumable, into $stage_dir/$name.part, verified, then moved into place"
        return 0
    fi
    mkdir -p -- "$models_dir" "$stage_dir"
    part="$stage_dir/$name.part"
    say "  downloading      $name from $url"
    if ! curl -fL -C - --retry 3 --retry-delay 2 --connect-timeout 20 --progress-bar -o "$part" "$url"; then
        printf 'error: download failed: %s\n' "$url" >&2
        printf 'The partial file is kept at %s, so re-running this installer resumes it.\n' "$part" >&2
        exit 1
    fi
    actual="$(sha256sum -- "$part" | cut -d' ' -f1)"
    if [ "$actual" != "$expected" ]; then
        rm -f -- "$part"
        printf 'error: sha256 mismatch for %s\n' "$name" >&2
        printf '  expected %s\n' "$expected" >&2
        printf '  actual   %s\n' "$actual" >&2
        printf 'The downloaded file was deleted and nothing was installed.\n' >&2
        exit 1
    fi
    mv -f -- "$part" "$dst"
    say "  installed        $dst (sha256 verified)"
}

render_desktop_entry() { # destination, exec path, extra line (optional)
    local dst="$1" exec_path="$2" extra="${3:-}" tmp line
    if [ "$dry_run" = yes ]; then
        say "  would write      $dst (Exec=$exec_path)"
        return 0
    fi
    mkdir -p -- "$(dirname -- "$dst")"
    tmp="$dst.tmp.$$"
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "${line//@EXEC@/$exec_path}"
    done < "$template" > "$tmp"
    if [ -n "$extra" ]; then
        printf '%s\n' "$extra" >> "$tmp"
    fi
    mv -f -- "$tmp" "$dst"
    say "  wrote            $dst"
}

install_icon() {
    if [ "$dry_run" = yes ]; then
        say "  would write      $icon_path"
        return 0
    fi
    mkdir -p -- "$icon_dir"
    cp -- "$icon_source" "$icon_path.tmp.$$"
    mv -f -- "$icon_path.tmp.$$" "$icon_path"
    say "  wrote            $icon_path"
}

install_autostart() {
    if [ "$autostart" = prompt ] && [ "$assume_yes" = yes ]; then
        autostart=yes  # --yes takes the prompt's default, which is yes
    fi
    if [ "$autostart" = prompt ] && [ "$dry_run" != yes ]; then
        ask 'Start Utter automatically at login? [Y/n]'
        local answer=""
        read -r answer || answer=""
        case "$answer" in
            [nN]*) autostart=no ;;
            *) autostart=yes ;;
        esac
    fi
    if [ "$autostart" = no ]; then
        note "autostart         skipped (--no-autostart)"
        return 0
    fi
    if [ "$autostart" = prompt ]; then
        # A dry run does not take the answer, so it reports the question instead.
        say "  would ask        start Utter automatically at login? [Y/n]"
    fi
    render_desktop_entry "$autostart_path" "$venv_binary" "X-GNOME-Autostart-enabled=true"
}

install_shim() {
    local tmp prefix_exports=""
    if [ "$dry_run" = yes ]; then
        if [ -f "$shim_path" ]; then
            say "  already present  $shim_path"
        else
            say "  would write      $shim_path (execs $venv_binary with a repaired PATH)"
        fi
        return 0
    fi
    if [ -n "$prefix" ]; then
        # A prefixed install keeps its own XDG roots, so its shim points the app at the
        # prefix rather than at the real home the app would otherwise use.
        prefix_exports="XDG_DATA_HOME='$data_home'
XDG_CONFIG_HOME='$config_home'
XDG_STATE_HOME='$state_home'
export XDG_DATA_HOME XDG_CONFIG_HOME XDG_STATE_HOME"
    fi
    mkdir -p -- "$bin_dir"
    tmp="$shim_path.tmp.$$"
    cat > "$tmp" <<EOF
#!/bin/sh
# Utter launcher, installed by install.sh.
#
# It exists for one reason: a process started from a desktop entry or a systemd user unit
# does not necessarily inherit the PATH a login shell gives, and Utter shells out to
# wl-paste/xclip/xsel for the selection and to espeak-ng for phonemes. The interpreter in
# the venv is found by absolute path, so PATH is the only thing that needs repairing.
PATH="\$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin\${PATH:+:\$PATH}"
export PATH
$prefix_exports
exec "$venv_binary" "\$@"
EOF
    chmod 0755 "$tmp"
    mv -f -- "$tmp" "$shim_path"
    say "  wrote            $shim_path"
}

record_digests() {
    if [ "$dry_run" = yes ]; then
        if [ -f "$config_path" ]; then
            say "  would write      $config_path (consent and both digests, keeping the values"
            say "                   the file already holds)"
        else
            say "  would write      $config_path (a new config holding the consent and both digests)"
        fi
        return 0
    fi
    mkdir -p -- "$(dirname -- "$config_path")"
    "$venv_dir/bin/python" - "$model_sha256" "$voices_sha256" "$config_path" <<'PY'
import sys
from pathlib import Path

from utter.config import Config

# The app's own loader and writer, so the result is exactly the shape Config reads: a
# config that already exists keeps every value it holds (an unreadable key it never knew
# about is not preserved, exactly as when the app saves), and an absent one is created
# with the schema's defaults plus this install's record. The runtime and `utter doctor`
# verify the model files against these two digests, so a fresh install must write them.
path = Path(sys.argv[3])
config = Config.load(path)
config.install.consent_given = True
config.install.model_sha256 = sys.argv[1]
config.install.voices_sha256 = sys.argv[2]
config.save(path)
PY
    say "  wrote            $config_path (consent and both pinned digests)"
}

refresh_desktop_database() {
    if [ "$dry_run" = yes ]; then
        return 0
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -- "$applications_dir" >/dev/null 2>&1 ||
            warn "update-desktop-database failed on $applications_dir; harmless"
    fi
}

post_install_notes() {
    say ""
    say "Utter is installed."
    note "launcher:  $entry_path"
    note "command:   $shim_path   (or $venv_binary)"
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *) note "note: $bin_dir is not on your PATH; add it to run utter from a shell." ;;
    esac
    say ""
    say "The first run asks the desktop for three global shortcuts: Super+Alt+S speaks the"
    say "selection, Super+Alt+X stops, Super+Alt+P pauses. The desktop shows a one-time dialog;"
    say "press the keys it offers and accept it once."
    say "Without a global-shortcuts portal, bind 'utter speak' and 'utter stop' in your desktop's"
    say "own shortcut settings instead: the CLI reads the selection and does the same job."
    say ""
    say "Uninstall with: bash $script_dir/install.sh --uninstall    (add --purge to remove config.toml)"
}

# --- uninstall ------------------------------------------------------------------------

remove_path() { # path, description
    local path="$1" what="$2"
    case "$path" in
        "" | "/") die "refusing to remove '$path'" 2 ;;
    esac
    if [ ! -e "$path" ]; then
        note "nothing at       $path ($what)"
        return 0
    fi
    if [ "$dry_run" = yes ]; then
        say "  would remove     $path ($what)"
        return 0
    fi
    rm -rf -- "$path"
    say "  removed          $path ($what)"
}

stop_running_instance() {
    if [ "$dry_run" = yes ]; then
        say "  would stop       a running Utter instance, if there is one"
        return 0
    fi
    if ! command -v pkill >/dev/null 2>&1; then
        warn "pkill is unavailable: stop a running Utter yourself before removing its venv"
        return 0
    fi
    if pkill -f -- "$venv_dir/bin/" >/dev/null 2>&1; then
        say "  stopped          the running instance"
        sleep 1
    fi
}

do_uninstall() {
    say "Uninstalling Utter"
    stop_running_instance
    remove_path "$venv_dir" "venv"
    remove_path "$models_dir" "model files"
    remove_path "$stage_dir" "download staging"
    remove_path "$entry_path" "desktop entry"
    remove_path "$icon_path" "icon"
    remove_path "$autostart_path" "autostart entry"
    remove_path "$shim_path" "launcher shim"
    remove_path "$state_home/utter" "logs"
    if [ "$purge" = yes ]; then
        remove_path "$config_home/utter" "config"
        say "Purged: config.toml is gone too."
    elif [ -f "$config_path" ]; then
        say ""
        say "Kept $config_path -- run with --purge to remove it as well."
    fi
    say ""
    say "Utter is uninstalled."
}

# --- main -----------------------------------------------------------------------------

say "Utter installer"
say ""
preflight_report

if [ "$uninstall" = yes ]; then
    do_uninstall
    exit 0
fi

if [ "$dry_run" != yes ] && [ -n "$missing_required" ]; then
    die "cannot install: missing$missing_required (see the preflight report above)" 2
fi

[ -f "$template" ] ||
    die "missing $template: this installer must stay next to the assets/ directory of the Utter checkout" 2
[ -f "$icon_source" ] ||
    die "missing $icon_source: this installer must stay next to the assets/ directory of the Utter checkout" 2

print_ledger
say ""
consent_gate

say ""
say "Plan"
if [ "$dry_run" = yes ]; then
    say "  (dry run: nothing below is written)"
else
    say "  (the two model files are fetched only if they are missing or fail their digest)"
fi
say ""
say "Venv and package"
install_venv
say ""
say "Model files"
install_model_file "$model_url" "$model_sha256" "$MODEL_NAME"
install_model_file "$voices_url" "$voices_sha256" "$VOICES_NAME"
say ""
say "Desktop integration"
render_desktop_entry "$entry_path" "$venv_binary"
install_icon
install_autostart
install_shim
refresh_desktop_database
say ""
say "Configuration"
record_digests

if [ "$dry_run" = yes ]; then
    say ""
    if [ -n "$missing_required" ]; then
        warn "this plan cannot run yet: missing$missing_required"
        warn "install them (see the preflight report above) and re-run install.sh"
        exit 2
    fi
    say "Dry run: nothing above was written."
    say ""
    exit 0
fi

post_install_notes

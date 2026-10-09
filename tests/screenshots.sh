#!/usr/bin/sh
# tests/screenshots.sh
# Makes the pictures the README shows, from the real code and nothing else:
#
#   preview.png             the card of the last 7 days
#   docs/card-month.png     the card of the last 30 days
#   docs/card-light.png     the 7-day card in a light theme
#   docs/bar-widget.png     the bar widget, drawn from BarWidget.qml at 4x
#
# The cards are `omawrapped card` run on the sample days and repositories
# of tests/make_sample.py. The widget is drawn by a headless Quickshell from
# the widget's own QML and the shell's Ui kit, with a stand-in service that
# reports 3h 07m. Everything runs in a temporary home: your session, your
# recorded data and your Omarchy configuration are not read or touched.
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
OMARCHY=${OMARCHY_PATH:-/usr/share/omarchy}
THEME=$OMARCHY/themes/matte-black
LIGHT=$OMARCHY/themes/catppuccin-latte
RUN=$(/usr/bin/mktemp -d "${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR is not set}/ow-shot.XXXXXX")
case "$RUN" in /*/ow-shot.??????) ;; *) echo "screenshots: unusable run dir" >&2; exit 1 ;; esac
trap 'rm -rf -- "$RUN"' EXIT

HOME_DIR=$RUN/home
mkdir -p "$HOME_DIR/.local/state/omarchy/current/theme" "$RUN/root" "$REPO/docs"
# The shell's Color singleton reads the active theme from here at start.
cp -- "$THEME/colors.toml" "$HOME_DIR/.local/state/omarchy/current/theme/colors.toml"
# One plugin installed in this home: this one.
mkdir -p "$HOME_DIR/.config/omarchy/plugins/io.github.davidgudovic.omawrapped"
cp -- "$REPO/manifest.json" "$HOME_DIR/.config/omarchy/plugins/io.github.davidgudovic.omawrapped/manifest.json"

run() {
  env -i HOME="$HOME_DIR" XDG_RUNTIME_DIR="$RUN" XDG_DATA_HOME="$RUN/sample/data" \
    XDG_CONFIG_HOME="$HOME_DIR/.config" XDG_CACHE_HOME="$RUN/cache" XDG_STATE_HOME="$HOME_DIR/.local/state" \
    PATH=/usr/bin LANG=C.UTF-8 OMARCHY_PATH="$OMARCHY" "$@"
}

# The bar widget.
cp -- "$REPO/tests/harness/screenshot.qml" "$RUN/root/shell.qml"
ln -s -- "$OMARCHY/shell/Ui" "$RUN/root/Ui"
ln -s -- "$OMARCHY/shell/Commons" "$RUN/root/Commons"
run QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=4 QS_DISABLE_CRASH_HANDLER=1 QS_NO_RELOAD_POPUP=1 \
  QS_DISABLE_FILE_WATCHER=1 OW_REPO="$REPO" OW_OUT="$REPO/docs/bar-widget.png" \
  /usr/bin/timeout -k 2 30 /usr/bin/quickshell -p "$RUN/root/shell.qml" > "$RUN/out.txt" 2>&1 || true
grep -ao 'OW-SHOT .*' "$RUN/out.txt" || { echo "screenshots: the widget was not drawn" >&2; tail -n 20 "$RUN/out.txt" >&2; exit 1; }
if grep -aq 'OW-SHOT FAIL' "$RUN/out.txt"; then exit 1; fi
if grep -a ' WARN' "$RUN/out.txt" | grep -a -e "$REPO/"; then echo "screenshots: the widget warned while it was drawn" >&2; exit 1; fi

# The cards. Nothing is copied, opened or announced.
run /usr/bin/python3 -B "$REPO/tests/make_sample.py" "$RUN/sample" --repos
card() {
  run "$REPO/bin/omawrapped" card --repos "$RUN/sample/repos" --copy none "$@" > /dev/null
}
card --week --theme "$THEME" -o "$REPO/preview.png"
card --month --theme "$THEME" -o "$REPO/docs/card-month.png"
card --week --theme "$LIGHT" -o "$REPO/docs/card-light.png"

for picture in preview.png docs/card-month.png docs/card-light.png docs/bar-widget.png; do
  echo "screenshots: $picture $(/usr/bin/file -b "$REPO/$picture" | cut -d, -f1-2)"
done

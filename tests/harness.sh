#!/usr/bin/sh
# tests/harness.sh <service|widget>
# Runs tests/harness/<case>.qml in its own headless Quickshell and reports
# PASS or FAIL. The shell gets a private home, runtime and data directory
# and no compositor, so nothing here can reach the live session or its data.
set -eu

CASE=${1:?usage: harness.sh <service|widget>}
case "$CASE" in service|widget) ;; *) echo "harness: unknown case" >&2; exit 2 ;; esac

REPO=$(cd "$(dirname "$0")/.." && pwd)
SHELL_DIR=${OMARCHY_PATH:-/usr/share/omarchy}/shell
BASE=${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR is not set}
RUN=$(/usr/bin/mktemp -d "$BASE/ow-test.XXXXXX")
case "$RUN" in /*/ow-test.??????) ;; *) echo "harness: unusable run dir" >&2; exit 1 ;; esac
trap 'rm -rf -- "$RUN"' EXIT

# qs.Ui and qs.Commons resolve against the shell root, so the case gets a
# throwaway root with links to the first-party kit. The links live outside
# the repository, which may hold none.
mkdir "$RUN/home" "$RUN/root" "$RUN/bin"
cp -- "$REPO/tests/harness/$CASE.qml" "$RUN/root/shell.qml"
ln -s -- "$SHELL_DIR/Ui" "$RUN/root/Ui"
ln -s -- "$SHELL_DIR/Commons" "$RUN/root/Commons"

# Stand-ins for every desktop program the plugin can start, first in PATH,
# so a case can read what would have happened instead of it happening.
#
# The notifier appends its arguments, one per line, and "==" after each call.
# Only the widget starts it, and only with one of its two fixed sentences:
# what the command has to say goes over the session bus, further down.
cat > "$RUN/bin/omarchy-notification-send" <<'STUB'
#!/usr/bin/sh
{ printf '%s\n' "$@"; echo "=="; } >> "$OW_RUN/widget-said.txt"
STUB
# Omarchy's menu: answers with the line in menu-choice, or is dismissed.
cat > "$RUN/bin/omarchy-menu-select" <<'STUB'
#!/usr/bin/sh
printf '%s\n' "$@" > "$OW_RUN/menu-asked.txt"
[ -s "$OW_RUN/menu-choice" ] || exit 1
cat "$OW_RUN/menu-choice"
STUB
# The rest only write down that they were started, and with what. That
# includes Omarchy's command for changing a setting: a case may pause, and
# must never do it to the real bar.
for name in nautilus uwsm-app xdg-open wl-copy notify-send omarchy-bar; do
  printf '#!/usr/bin/sh\necho "%s $*" >> "$OW_RUN/started.txt"\n' "$name" > "$RUN/bin/$name"
done
# The command asks the shell for its sampler. Here there is none to find,
# and the real shell must not be asked.
printf '#!/usr/bin/sh\nexit 1\n' > "$RUN/bin/omarchy-shell"
chmod +x "$RUN"/bin/*

# A session bus of this run's own, with a stand-in notification service that
# writes down what it is sent. Without it the bus address leads nowhere: the
# real session bus is never within reach.
BUS_ADDRESS="unix:path=$RUN/no-bus"
if [ "$CASE" = widget ]; then
  env -i HOME="$RUN/home" XDG_RUNTIME_DIR="$RUN" PATH=/usr/bin \
    /usr/bin/python3 -B "$REPO/tests/fake_bus.py" "$RUN/notifications.jsonl" > "$RUN/bus-address" 2> "$RUN/bus.txt" &
  BUS_PID=$!
  trap 'kill "$BUS_PID" 2>/dev/null || true; wait "$BUS_PID" 2>/dev/null || true; rm -rf -- "$RUN"' EXIT
  tries=0
  until [ -s "$RUN/bus-address" ]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 100 ] || ! kill -0 "$BUS_PID" 2>/dev/null; then
      echo "harness: the test bus did not start" >&2; cat "$RUN/bus.txt" >&2; exit 1
    fi
    /usr/bin/sleep 0.1
  done
  BUS_ADDRESS=$(cat "$RUN/bus-address")
fi

env -i HOME="$RUN/home" XDG_RUNTIME_DIR="$RUN" XDG_DATA_HOME="$RUN/data" \
  DBUS_SESSION_BUS_ADDRESS="$BUS_ADDRESS" \
  XDG_CONFIG_HOME="$RUN/config" XDG_CACHE_HOME="$RUN/cache" XDG_STATE_HOME="$RUN/state" \
  PATH="$RUN/bin:/usr/bin" LANG=C.UTF-8 QT_QPA_PLATFORM=offscreen \
  QS_DISABLE_CRASH_HANDLER=1 QS_NO_RELOAD_POPUP=1 QS_DISABLE_FILE_WATCHER=1 \
  OW_REPO="$REPO" OW_RUN="$RUN" OW_ROOT="$RUN/root" \
  /usr/bin/timeout -k 2 60 /usr/bin/quickshell -p "$RUN/root/shell.qml" > "$RUN/out.txt" 2>&1 || true

grep -ao 'OW-CHECK .*' "$RUN/out.txt" || true
verdict=FAIL
if grep -aq 'OW-RESULT PASS' "$RUN/out.txt"; then verdict=PASS; fi

# A warning that names one of the plugin's own files is a failure even when
# every check passed: a binding that cannot be evaluated only says so there.
if grep -a ' WARN' "$RUN/out.txt" | grep -aq -e "$REPO/[A-Za-z]*\.qml" -e "$REPO/Tracker\.js"; then
  grep -a ' WARN' "$RUN/out.txt" | grep -a -e "$REPO/" >&2
  verdict=FAIL
fi

if [ "$CASE" = service ] && [ "$verdict" = PASS ]; then
  # The data directory must be private to the user, also after the case
  # has deleted it twice under the running service.
  mode=$(stat -c %a "$RUN/data/omawrapped")
  if [ "$mode" != 700 ]; then echo "harness: data directory mode is $mode, expected 700" >&2; verdict=FAIL; fi
  # So must what is in it: the days folder, and every day file, which the
  # shell makes readable by everyone and the service then closes.
  modes=$(stat -c %a "$RUN/data/omawrapped/days" "$RUN"/data/omawrapped/days/*.json | tr '\n' ' ')
  if [ "$modes" = "700 600 " ]; then
    echo "OW-CHECK ok   the days folder and the day file are private = $modes(expected 700 600)"
  else
    echo "harness: modes of the days folder and its file are $modes, expected 700 600" >&2; verdict=FAIL
  fi
  # The case ends with a second it never flushes. The file had about 1300 ms
  # before it (and a second of pause, which adds nothing); the shell closing
  # must have added the rest.
  written=$(jq -r '.active_ms' "$RUN"/data/omawrapped/days/*.json)
  if [ "$written" -ge 2100 ] && [ "$written" -le 2700 ]; then
    echo "OW-CHECK ok   written when the shell closed = $written (expected about 2300)"
  else
    echo "harness: $written ms on disk after the shell closed, expected about 2300" >&2; verdict=FAIL
  fi
fi

if [ "$CASE" = widget ]; then
  # Every user of a machine can read what a program was started with. The
  # case has drawn cards and asked for today, from a month of sample days:
  # no app of those days and no time may be among the arguments of anything
  # that was started for it.
  touch "$RUN/widget-said.txt" "$RUN/started.txt" "$RUN/menu-asked.txt"
  if grep -a -n -E -e '[0-9]+h [0-9][0-9]m' -e '[0-9]+m( |$)' -e 'Ghostty|Chromium|Zed|Slack|Obsidian' \
      "$RUN/widget-said.txt" "$RUN/started.txt" "$RUN/menu-asked.txt" >&2; then
    echo "harness: a program was started with what the user did among its arguments" >&2; verdict=FAIL
  else
    echo "OW-CHECK ok   no app and no time among the arguments of any program started"
  fi
fi

if [ "$verdict" != PASS ]; then
  grep -a -e 'OW-RESULT' -e ' WARN' -e 'ERROR' "$RUN/out.txt" | tail -n 20 >&2 || true
fi
echo "harness: $verdict $CASE"
[ "$verdict" = PASS ]

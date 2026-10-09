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

# A stand-in for Omarchy's notifier, so a case can read what would have been
# shown instead of showing it. It appends, so a second call is seen too.
cat > "$RUN/bin/omarchy-notification-send" <<'STUB'
#!/usr/bin/sh
printf '%s\n' "$@" >> "$OW_RUN/notification.txt"
STUB
chmod +x "$RUN/bin/omarchy-notification-send"

env -i HOME="$RUN/home" XDG_RUNTIME_DIR="$RUN" XDG_DATA_HOME="$RUN/data" \
  XDG_CONFIG_HOME="$RUN/config" XDG_CACHE_HOME="$RUN/cache" XDG_STATE_HOME="$RUN/state" \
  PATH="$RUN/bin:/usr/bin" LANG=C.UTF-8 QT_QPA_PLATFORM=offscreen \
  QS_DISABLE_CRASH_HANDLER=1 QS_NO_RELOAD_POPUP=1 QS_DISABLE_FILE_WATCHER=1 \
  OW_REPO="$REPO" OW_RUN="$RUN" \
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
  # The data directory must be private to the user.
  mode=$(stat -c %a "$RUN/data/omawrapped")
  if [ "$mode" != 700 ]; then echo "harness: data directory mode is $mode, expected 700" >&2; verdict=FAIL; fi
  # The case ends with a second it never flushes. The file had about 1000 ms
  # before it; the shell closing must have added the rest.
  written=$(jq -r '.active_ms' "$RUN"/data/omawrapped/days/*.json)
  if [ "$written" -ge 1800 ] && [ "$written" -le 2400 ]; then
    echo "OW-CHECK ok   written when the shell closed = $written (expected about 2000)"
  else
    echo "harness: $written ms on disk after the shell closed, expected about 2000" >&2; verdict=FAIL
  fi
fi

if [ "$verdict" != PASS ]; then
  grep -a -e 'OW-RESULT' -e ' WARN' -e 'ERROR' "$RUN/out.txt" | tail -n 20 >&2 || true
fi
echo "harness: $verdict $CASE"
[ "$verdict" = PASS ]

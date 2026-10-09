import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Wayland
import "Tracker.js" as Tracker

// The sampler: one instance per shell, created by the host while the plugin
// is enabled. It listens to what the shell already knows (which window has
// focus, whether the user is idle, whether the session is locked), keeps the
// time in memory for at most a minute, and then adds it to one small file
// per day under ~/.local/share/omawrapped/days/.
//
// Apart from creating its data folder, at start and again if that folder
// is ever deleted, it runs no program.
// It opens no window, sends no notification and makes no network request.
// Only the focused window's app id is read; its title is never looked at.
//
// The files are the truth and memory holds only what was not written yet:
// every flush reads the day back, adds to it and replaces it. That is what
// lets `omawrapped reset` delete the files under a running shell
// without the next flush putting a whole day back.
Item {
  id: root

  // ---- Injected by the host ----

  // The host's facade: null before it is injected and again once the plugin
  // is disabled. Only the bar configuration is read from it.
  property var shell: null

  readonly property string pluginId: "io.github.davidgudovic.omawrapped"

  // ---- Settings, from the plugin's entry in shell.json ----

  readonly property var entry: Tracker.entryFor(shell ? shell.barConfig : null, pluginId)
  readonly property int idleSeconds: boundedInt(entry.idleSeconds, 120, 30, 3600)
  // The user's own switch: nothing is counted while it is on. It is a
  // setting, so that it survives a restart and keeps the other settings,
  // which disabling the plugin would not.
  readonly property bool paused: entry.paused === true || entry.paused === "true"
  // A playing video or a call keeps the session from going idle. That is
  // screen time, unless the user says otherwise.
  readonly property bool countKeptAwake: entry.countKeptAwake !== false && entry.countKeptAwake !== "false"
  // The screensaver is a window like any other, and nobody is watching it.
  // The list is matched by key, so a class may be typed in any case and a
  // web app is matched by its site, whatever page it is on.
  readonly property var ignoredKeys: ["org.omarchy.screensaver"].concat(Tracker.parseList(entry.ignoreApps))
    .map(function(app) { return Tracker.ignoreKey(app) })

  // ---- What the session says right now ----
  //
  // Test seam: the harness assigns these three to play a session back, and
  // switches pollLock off because it has no compositor to ask.

  property string focusedApp: ToplevelManager.activeToplevel ? (ToplevelManager.activeToplevel.appId || "") : ""
  property bool userIdle: idleMonitor.isIdle
  property bool sessionLocked: Tracker.anyLocked(monitorBlockers())
  property bool pollLock: true

  // Nothing is counted while counting is paused, the user is idle, the
  // session is locked, or an ignored app has focus.
  readonly property bool away: isAway()

  // ---- For the bar widget ----

  // True once the data directory exists and today's file was read.
  readonly property bool ready: _ready
  // Today so far, including what is not on disk yet. Refreshed every tick.
  readonly property real todayMs: _today.active_ms

  // ---- Internals ----

  readonly property int tickMs: 15000
  // A flush every fourth tick. The margin is for a tick that comes a few
  // milliseconds early, which would otherwise put the flush off by a tick.
  readonly property int flushMs: 50000
  // A stretch longer than this means the tick did not run: suspend.
  readonly property int maxGapMs: tickMs * 3

  readonly property string dataDir: {
    var base = Quickshell.env("XDG_DATA_HOME")
    if (!base || String(base).charAt(0) !== "/") base = Quickshell.env("HOME") + "/.local/share"
    return base + "/omawrapped"
  }

  property var _state: Tracker.create()
  property bool _ready: false
  // True once the shell is taking the service down.
  property bool _closing: false
  // The day file as it was last read or written, to show today's total
  // without reading the disk on every tick.
  property var _stored: null
  property var _today: Tracker.emptyDay(Tracker.dateKey(Date.now()))
  property double _lastFlush: 0

  function boundedInt(value, fallback, min, max) {
    var n = parseInt(String(value), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  function monitorBlockers() {
    var monitors = Hyprland.monitors.values
    var out = []
    for (var i = 0; i < monitors.length; i++) {
      var info = monitors[i].lastIpcObject
      if (info) out.push(info.solitaryBlockedBy)
    }
    return out
  }

  function dayPath(date) {
    return dataDir + "/days/" + date + ".json"
  }

  // { ok, day }: day is null when there is no file yet. ok is false when
  // the file could not be read, or holds something that is not a day of
  // ours: it is then left alone, and the time stays in memory.
  function readDay(date) {
    var view = fileComponent.createObject(root, { path: dayPath(date) })
    var text = view.text()
    var outcome = view.outcome
    view.destroy()
    if (outcome === "loaded") return Tracker.readStored(text, date)
    return { ok: outcome === "missing", day: null }
  }

  // Asking for a folder as if it were a file fails either way, but only a
  // missing one fails as "not found".
  function folderExists() {
    var view = fileComponent.createObject(root, { path: dataDir })
    view.text()
    var missing = view.outcome === "missing"
    view.destroy()
    return !missing
  }

  function writeDay(day) {
    var view = fileComponent.createObject(root, { path: dayPath(day.date), preload: false })
    view.setText(Tracker.serialize(day))
    var saved = view.outcome === "saved"
    view.destroy()
    return saved
  }

  function isAway() {
    return paused || userIdle || sessionLocked || ignoredKeys.indexOf(Tracker.ignoreKey(focusedApp)) !== -1
  }

  // Closes the running stretch under the conditions it ran with and starts
  // the next one. Called on every change of those conditions and on a tick.
  // It asks isAway() rather than reading `away`, which may not have caught
  // up yet when two of the conditions change in the same moment.
  function observe() {
    Tracker.observe(_state, Date.now(), focusedApp, isAway(), maxGapMs)
  }

  function refreshToday() {
    var date = Tracker.dateKey(Date.now())
    var stored = _stored && _stored.date === date ? _stored : null
    var pending = Tracker.pendingDay(_state, date)
    _today = pending ? Tracker.mergeDay(stored, pending) : (stored || Tracker.emptyDay(date))
  }

  // Adds everything counted so far to the day files. A day that cannot be
  // read or written is kept in memory and tried again with the next flush.
  function flush() {
    if (!_ready) return
    observe()
    _lastFlush = Date.now()
    // The folder may be gone, deleted by `omawrapped reset` or by hand. A
    // write would make it again, but open to other users; `prepare` makes
    // it private, and what was counted waits in memory until it has.
    if (!folderExists()) {
      _ready = false
      _stored = null
      refreshToday()
      prepare.running = true
      return
    }
    var today = Tracker.dateKey(_lastFlush)
    var pending = Tracker.takePending(_state)
    var todayRead = false
    var made = false
    for (var date in pending) {
      var read = readDay(date)
      var merged = read.ok ? Tracker.mergeDay(read.day, pending[date]) : null
      if (merged !== null && writeDay(merged)) {
        if (read.day === null) made = true
        if (date === today) {
          _stored = merged
          todayRead = true
        }
      } else {
        Tracker.restorePending(_state, pending[date])
      }
    }
    // Without anything to write, today's file is still looked at, so that
    // the bar follows a reset or a file removed by hand.
    if (!todayRead) _stored = readDay(today).day
    refreshToday()
    if (made) makePrivate()
  }

  // The shell writes a new file readable by everyone and keeps the mode of
  // one it writes again. So a day file is closed to other users once, right
  // after it was made; until then the folder above it keeps them out.
  function makePrivate() {
    // A shell that is closing cannot wait for a program of its own.
    if (_closing) Quickshell.execDetached({ command: privateCommand, environment: privateEnvironment })
    else closer.running = true
  }

  function tick() {
    if (pollLock) Hyprland.refreshMonitors()
    observe()
    if (Date.now() - _lastFlush >= flushMs) flush()
    else refreshToday()
  }

  // Forgets what was not written yet. `omawrapped reset` calls this
  // before deleting the files, so nothing recorded before it survives.
  function discard() {
    // The running stretch is closed first, or it would be counted after.
    observe()
    Tracker.takePending(_state)
    _stored = null
    refreshToday()
  }

  function statusJson() {
    return JSON.stringify({
      counting: _ready && !away,
      paused: paused,
      locked: sessionLocked,
      idleSeconds: idleSeconds,
      countKeptAwake: countKeptAwake,
      ignoreApps: Tracker.parseList(entry.ignoreApps),
      todayMs: Math.round(todayMs),
      dataDir: dataDir
    })
  }

  // Locking and unlocking move the focus and wake the idle timer, so those
  // two are also the moments to ask about the lock. The tick asks as well,
  // in case a lock ever comes without either.
  function sessionChanged() {
    observe()
    if (pollLock) lockProbe.restart()
  }

  onFocusedAppChanged: sessionChanged()
  onUserIdleChanged: sessionChanged()
  onSessionLockedChanged: observe()
  onIgnoredKeysChanged: observe()
  onPausedChanged: observe()
  // Walking away is the moment before the screen locks, the machine sleeps
  // or the session ends: what was counted goes to disk now, not at the
  // next minute.
  onAwayChanged: if (away) flush()

  Component.onCompleted: {
    prepare.running = true
    // What the shell last heard about the lock may be hours old.
    if (pollLock) lockProbe.restart()
  }
  Component.onDestruction: {
    _closing = true
    flush()
  }

  IdleMonitor {
    id: idleMonitor
    enabled: true
    timeout: root.idleSeconds
    respectInhibitors: root.countKeptAwake
  }

  // Makes the data folder and its days folder if they are missing, and
  // closes them and every day file to other users: 700 and 600. The folder
  // is named in the program's environment, which only its owner can read,
  // not in its arguments, which everyone can. This is the only program the
  // service ever runs; the day files are written by the shell itself.
  readonly property var privateCommand: ["sh", "-c", "umask 077; mkdir -p \"$OW_DIR/days\" && "
    + "chmod 700 \"$OW_DIR\" \"$OW_DIR/days\"; "
    + "for f in \"$OW_DIR\"/days/*.json; do [ -f \"$f\" ] && [ ! -L \"$f\" ] && chmod 600 \"$f\"; done; true"]
  readonly property var privateEnvironment: ({ OW_DIR: root.dataDir })

  Process {
    id: prepare
    command: root.privateCommand
    environment: root.privateEnvironment
    onExited: {
      root._ready = true
      // Reads today's file, and writes whatever waited for the folder. If
      // the folder could not be made, the next flush tries again.
      if (root.folderExists()) root.flush()
    }
  }

  // The same once more, after a day file was made.
  Process {
    id: closer
    command: root.privateCommand
    environment: root.privateEnvironment
  }

  Timer {
    interval: root.tickMs
    repeat: true
    running: true
    onTriggered: root.tick()
  }

  // One question for a burst of changes: focus often moves several times
  // within a millisecond.
  Timer {
    id: lockProbe
    interval: 50
    onTriggered: Hyprland.refreshMonitors()
  }

  // The shell is closing: write what the last minute collected.
  Connections {
    target: Qt.application
    function onAboutToQuit() { root.flush() }
  }

  // One file view per read or write. A view that is pointed at a second
  // path keeps the text of the first when the second does not exist.
  Component {
    id: fileComponent

    FileView {
      property string outcome: "pending"

      blockLoading: true
      blockWrites: true
      atomicWrites: true
      printErrors: false
      watchChanges: false
      onLoaded: outcome = "loaded"
      onLoadFailed: function(error) { outcome = error === FileViewError.FileNotFound ? "missing" : "failed" }
      onSaved: outcome = "saved"
      onSaveFailed: outcome = "failed"
    }
  }

  IpcHandler {
    target: root.pluginId

    function flush(): string {
      root.flush()
      return "ok"
    }

    function discard(): string {
      root.discard()
      return "ok"
    }

    function status(): string {
      return root.statusJson()
    }
  }
}

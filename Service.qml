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
// Apart from creating its data folder once at start, it runs no program.
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
  // A playing video or a call keeps the session from going idle. That is
  // screen time, unless the user says otherwise.
  readonly property bool countKeptAwake: entry.countKeptAwake !== false && entry.countKeptAwake !== "false"
  // The screensaver is a window like any other, and nobody is watching it.
  readonly property var ignoredApps: ["org.omarchy.screensaver"].concat(Tracker.parseList(entry.ignoreApps))

  // ---- What the session says right now ----
  //
  // Test seam: the harness assigns these three to play a session back, and
  // switches pollLock off because it has no compositor to ask.

  property string focusedApp: ToplevelManager.activeToplevel ? (ToplevelManager.activeToplevel.appId || "") : ""
  property bool userIdle: idleMonitor.isIdle
  property bool sessionLocked: Tracker.anyLocked(monitorBlockers())
  property bool pollLock: true

  // Nothing is counted while the user is idle, the session is locked, or
  // an ignored app has focus.
  readonly property bool away: isAway()

  // ---- For the bar widget ----

  // True once the data directory exists and today's file was read.
  readonly property bool ready: _ready
  // Today so far, including what is not on disk yet. Refreshed every tick.
  readonly property real todayMs: _today.active_ms

  // ---- Internals ----

  readonly property int tickMs: 15000
  readonly property int flushMs: 60000
  // A stretch longer than this means the tick did not run: suspend.
  readonly property int maxGapMs: tickMs * 3

  readonly property string dataDir: {
    var base = Quickshell.env("XDG_DATA_HOME")
    if (!base || String(base).charAt(0) !== "/") base = Quickshell.env("HOME") + "/.local/share"
    return base + "/omawrapped"
  }

  property var _state: Tracker.create()
  property bool _ready: false
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

  // { ok, day }: day is null for a file that is missing or not ours; ok is
  // false only when the file is there and could not be read.
  function readDay(date) {
    var view = fileComponent.createObject(root, { path: dayPath(date) })
    var text = view.text()
    var outcome = view.outcome
    view.destroy()
    if (outcome === "loaded") return { ok: true, day: Tracker.parseDay(text, date) }
    return { ok: outcome === "missing", day: null }
  }

  function writeDay(day) {
    var view = fileComponent.createObject(root, { path: dayPath(day.date), preload: false })
    view.setText(Tracker.serialize(day))
    var saved = view.outcome === "saved"
    view.destroy()
    return saved
  }

  function isAway() {
    return userIdle || sessionLocked || ignoredApps.indexOf(focusedApp) !== -1
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
    var today = Tracker.dateKey(_lastFlush)
    var pending = Tracker.takePending(_state)
    var todayRead = false
    for (var date in pending) {
      var read = readDay(date)
      var merged = read.ok ? Tracker.mergeDay(read.day, pending[date]) : null
      if (merged !== null && writeDay(merged)) {
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
  }

  function tick() {
    if (pollLock) Hyprland.refreshMonitors()
    observe()
    if (Date.now() - _lastFlush >= flushMs) flush()
    else refreshToday()
  }

  // Forgets what was not written yet. `omawrapped reset` calls this
  // after deleting the files, so nothing recorded before it survives.
  function discard() {
    // The running stretch is closed first, or it would be counted after.
    observe()
    Tracker.takePending(_state)
    _stored = null
    refreshToday()
  }

  function statusJson() {
    return JSON.stringify({
      ready: _ready,
      counting: _ready && !away,
      idle: userIdle,
      locked: sessionLocked,
      idleSeconds: idleSeconds,
      countKeptAwake: countKeptAwake,
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
  onIgnoredAppsChanged: observe()

  Component.onCompleted: prepare.running = true
  Component.onDestruction: flush()

  IdleMonitor {
    id: idleMonitor
    enabled: true
    timeout: root.idleSeconds
    respectInhibitors: root.countKeptAwake
  }

  // The data directory is private to the user. This is the only program
  // the service ever runs; the day files are written by the shell itself.
  Process {
    id: prepare
    command: ["mkdir", "-p", "-m", "700", root.dataDir]
    onExited: {
      root._stored = root.readDay(Tracker.dateKey(Date.now())).day
      root._lastFlush = Date.now()
      root._ready = true
      root.observe()
      root.refreshToday()
    }
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

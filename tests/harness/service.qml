import QtQuick
import Quickshell
import Quickshell.Io

// Plays a scripted session through the real Service.qml and checks the day
// file it writes. Run by tests/harness.sh, headless, with a private runtime
// and data directory.
ShellRoot {
  id: harness

  readonly property string repo: Quickshell.env("OW_REPO")
  readonly property string dataDir: Quickshell.env("XDG_DATA_HOME") + "/omawrapped"
  readonly property int toleranceMs: 200

  property var service: null
  property var failures: []
  property int step: 0
  property string today: ""

  function make() {
    var component = Qt.createComponent("file://" + repo + "/Service.qml")
    if (component.status !== Component.Ready) {
      console.log("OW-RESULT FAIL load: " + component.errorString())
      Qt.quit()
      return null
    }
    // Away until the script says otherwise, so that nothing is counted
    // between the service coming up and the first step. The settings arrive
    // the way the host hands them over: in the bar layout on its facade.
    return component.createObject(null, {
      shell: { barConfig: { layout: { left: [], right: [{ id: "other.plugin" }, {
        id: "io.github.davidgudovic.omawrapped", idleSeconds: 45, ignoreApps: "secret, other" }] } } },
      pollLock: false, focusedApp: "", userIdle: true, sessionLocked: false
    })
  }

  function readDay() {
    var view = fileComponent.createObject(harness, { path: dataDir + "/days/" + today + ".json" })
    var text = view.text()
    view.destroy()
    try { return JSON.parse(text) } catch (error) { return null }
  }

  function near(label, actual, expected) {
    var ok = typeof actual === "number" && Math.abs(actual - expected) <= toleranceMs
    console.log("OW-CHECK " + (ok ? "ok  " : "FAIL") + " " + label + " = " + actual + " (expected " + expected + " ±" + toleranceMs + ")")
    if (!ok) failures.push(label)
  }

  function equal(label, actual, expected) {
    var ok = actual === expected
    console.log("OW-CHECK " + (ok ? "ok  " : "FAIL") + " " + label + " = " + JSON.stringify(actual) + " (expected " + JSON.stringify(expected) + ")")
    if (!ok) failures.push(label)
  }

  // [delay before the step in ms, what it does]
  readonly property var script: [
    [300, function() {
      equal("service ready", service.ready, true)
      equal("nothing counted while away", service.todayMs, 0)
      equal("idleSeconds from the bar layout", service.idleSeconds, 45)
      equal("a kept-awake session counts unless switched off", service.countKeptAwake, true)
      equal("ignored apps", service.ignoredApps.join(","), "org.omarchy.screensaver,secret,other")
      today = Qt.formatDate(new Date(), "yyyy-MM-dd")
      service.focusedApp = "alpha"
      service.userIdle = false
    }],
    [2000, function() { service.focusedApp = "beta" }],
    [1000, function() { service.userIdle = true }],
    [1500, function() { service.userIdle = false }],
    [1000, function() { service.sessionLocked = true }],
    [1000, function() { service.sessionLocked = false; service.focusedApp = "org.omarchy.screensaver" }],
    [1000, function() { service.focusedApp = "secret" }],
    [1000, function() { service.focusedApp = "" }],
    [1000, function() { service.focusedApp = "alpha" }],
    [1500, function() {
      service.flush()
      var day = readDay()
      equal("day file written", day !== null, true)
      if (day === null) return
      equal("version", day.version, 1)
      equal("date", day.date, today)
      near("alpha ms", day.apps_ms.alpha, 3500)
      near("beta ms", day.apps_ms.beta, 2000)
      equal("screensaver not recorded", day.apps_ms["org.omarchy.screensaver"], undefined)
      equal("ignored app not recorded", day.apps_ms.secret, undefined)
      equal("apps recorded", Object.keys(day.apps_ms).length, 2)
      near("active ms", day.active_ms, 6500)
      equal("switches", day.switches, 2)
      var hours = 0
      for (var h = 0; h < 24; h++) hours += day.hours_ms[h]
      equal("hours add up to active", hours, day.active_ms)
      near("todayMs for the widget", service.todayMs, 6500)
    }],
    // The shell reloads plugins by destroying the service: nothing may be lost.
    [1000, function() { service.destroy() }],
    [300, function() {
      var day = readDay()
      near("alpha ms after destroy", day ? day.apps_ms.alpha : null, 4500)
      service = make()
    }],
    [300, function() {
      near("a new service reads today back", service.todayMs, 7500)
      service.focusedApp = "gamma"
      service.userIdle = false
    }],
    // `omawrapped reset` under a running shell: the files go, then the
    // service is told to discard over IPC, as the command does. That it
    // answers at all also shows the new service took the IPC target over
    // from the one destroyed above.
    [1000, function() { wipe.running = true }],
    [300, function() { ipc.call("discard") }],
    [700, function() {
      equal("discard over IPC answered", ipc.answer, "ok")
      equal("todayMs after discard", service.todayMs, 0)
      ipc.call("status")
    }],
    [700, function() {
      var status = {}
      try { status = JSON.parse(ipc.answer) } catch (error) {}
      equal("status over IPC", status.ready === true && status.counting === true, true)
    }],
    [100, function() {
      service.flush()
      var day = readDay()
      equal("only gamma after reset", day ? Object.keys(day.apps_ms).join(",") : null, "gamma")
      // 700 + 700 + 100 ms have passed since the discard.
      near("gamma ms after reset", day ? day.apps_ms.gamma : null, 1500)
      near("active ms after reset", day ? day.active_ms : null, 1500)
    }],
    // Files removed by hand, without discard: the next flush writes only
    // what was not on disk yet, never the day it remembers.
    [200, function() { wipe.running = true }],
    [800, function() {
      service.flush()
      var day = readDay()
      near("active ms after files vanished", day ? day.active_ms : null, 1000)
      near("todayMs follows the disk", service.todayMs, 1000)
      var status = JSON.parse(service.statusJson())
      equal("status counting", status.counting, true)
      equal("status dataDir", status.dataDir, dataDir)
    }],
    // One more second that nobody flushes: the shell closing must write it.
    // tests/harness.sh reads the file once this process is gone.
    [1000, function() {}]
  ]

  function next() {
    if (step >= script.length) {
      console.log("OW-RESULT " + (failures.length === 0 ? "PASS" : "FAIL " + failures.join("; ")))
      Qt.quit()
      return
    }
    sequencer.interval = script[step][0]
    sequencer.start()
  }

  Timer {
    id: sequencer
    onTriggered: {
      harness.script[harness.step][1]()
      harness.step += 1
      harness.next()
    }
  }

  Process {
    id: wipe
    command: ["rm", "-rf", harness.dataDir + "/days"]
  }

  // Calls the service the way `omarchy-shell <plugin id> <method>` does.
  Process {
    id: ipc
    property string answer: ""
    function call(method) {
      answer = ""
      command = ["quickshell", "ipc", "-p", Quickshell.env("OW_ROOT") + "/shell.qml", "call", "--",
        "io.github.davidgudovic.omawrapped", method]
      running = true
    }
    stdout: StdioCollector { onStreamFinished: ipc.answer = text.trim() }
  }

  Component {
    id: fileComponent
    FileView { blockLoading: true; printErrors: false; watchChanges: false }
  }

  Component.onCompleted: {
    service = make()
    if (service) next()
  }
}

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
  property real beforePause: 0
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
    // between the service coming up and the first step.
    return component.createObject(null, {
      shell: shellWith({}), pollLock: false, focusedApp: "", userIdle: true, sessionLocked: false
    })
  }

  // The settings as the host hands them over: in the bar layout on its
  // facade. A change of settings is a new facade with a new layout.
  function shellWith(more) {
    var entry = { id: "io.github.davidgudovic.omawrapped", idleSeconds: 45,
      ignoreApps: "Secret, other, chrome-private.example.com__-Default" }
    for (var key in more) entry[key] = more[key]
    return { barConfig: { layout: { left: [], right: [{ id: "other.plugin" }, entry] } } }
  }

  function readText() {
    var view = fileComponent.createObject(harness, { path: dataDir + "/days/" + today + ".json" })
    var text = view.text()
    view.destroy()
    return text
  }

  function readDay() {
    try { return JSON.parse(readText()) } catch (error) { return null }
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
      equal("ignored apps, as the keys they are matched by", service.ignoredKeys.join(","),
        "org.omarchy.screensaver,secret,other,web:private.example.com")
      today = Qt.formatDate(new Date(), "yyyy-MM-dd")
      service.focusedApp = "alpha"
      service.userIdle = false
    }],
    // A web app: its window class holds the site, a path and the profile.
    [2000, function() { service.focusedApp = "chrome-beta.example.com__docs_d_SECRETDOC_edit-Work" }],
    [1000, function() { service.userIdle = true }],
    [1500, function() { service.userIdle = false }],
    [1000, function() { service.sessionLocked = true }],
    [1000, function() { service.sessionLocked = false; service.focusedApp = "org.omarchy.screensaver" }],
    // Ignored apps: one typed in another case, one a web app on another page.
    [500, function() { service.focusedApp = "secret" }],
    [500, function() { service.focusedApp = "chrome-private.example.com__inbox-Default" }],
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
      near("the web app, under its site", day.apps_ms["web:beta.example.com"], 2000)
      var text = readText()
      equal("no path, profile or ignored site in the file",
        /SECRETDOC|Work|docs_d|private\.example|inbox/.test(text), false)
      equal("screensaver not recorded", day.apps_ms["org.omarchy.screensaver"], undefined)
      equal("ignored apps not recorded", Object.keys(day.apps_ms).sort().join(","), "alpha,web:beta.example.com")
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
    // `omawrapped reset` under a running shell: the service is told to
    // discard over IPC, then the files go, as the command does. That it
    // answers at all also shows the new service took the IPC target over
    // from the one destroyed above.
    [1000, function() { ipc.call("discard") }],
    [500, function() {
      equal("discard over IPC answered", ipc.answer, "ok")
      equal("todayMs after discard", service.todayMs, 0)
      wipe.running = true
    }],
    [300, function() { ipc.call("status") }],
    [600, function() {
      var status = {}
      try { status = JSON.parse(ipc.answer) } catch (error) {}
      equal("status over IPC", status.counting === true && status.dataDir === dataDir, true)
      equal("status lists what the user ignores", (status.ignoreApps || []).join(","),
        "Secret,other,chrome-private.example.com__-Default")
      // The folder is gone: this flush writes nothing and has it made again.
      service.flush()
      equal("nothing is written into a folder that is gone", readDay(), null)
    }],
    [400, function() {
      service.flush()
      var day = readDay()
      equal("only gamma after reset", day ? Object.keys(day.apps_ms).join(",") : null, "gamma")
      // 500 + 300 + 600 + 400 ms have passed since the discard.
      near("gamma ms after reset", day ? day.apps_ms.gamma : null, 1800)
      near("active ms after reset", day ? day.active_ms : null, 1800)
    }],
    // The folder removed by hand, without discard: the next flushes write
    // only what was not on disk yet, never the day the service remembers.
    [200, function() { wipe.running = true }],
    [800, function() { service.flush() }],
    [300, function() {
      service.flush()
      var day = readDay()
      near("active ms after the folder vanished", day ? day.active_ms : null, 1300)
      near("todayMs follows the disk", service.todayMs, 1300)
      equal("not paused unless the setting says so", JSON.parse(service.statusJson()).paused, false)
      // The user pauses: the setting arrives, and counting stops with it.
      harness.beforePause = day ? day.active_ms : 0
      service.shell = shellWith({ paused: true })
    }],
    [1000, function() {
      service.flush()
      var day = readDay()
      var status = JSON.parse(service.statusJson())
      equal("the setting pauses the service", service.paused, true)
      equal("status says paused, and not counting", status.paused === true && status.counting === false, true)
      equal("nothing is counted while paused", day ? day.active_ms : null, harness.beforePause)
      // "true" is what `omarchy bar set` stores when it is not told the value is JSON.
      service.shell = shellWith({ paused: "true" })
      equal("the word true pauses as well", service.paused, true)
      service.shell = shellWith({})
      equal("and without the setting it counts again", JSON.parse(service.statusJson()).counting, true)
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
    // The whole folder, as `omawrapped reset` and the README's removal do.
    command: ["rm", "-rf", harness.dataDir]
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

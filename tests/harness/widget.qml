import QtQuick
import Quickshell
import Quickshell.Io

// Loads the real BarWidget.qml against the shell's own Ui and Commons kit,
// with a stand-in bar and service, and checks what it would show. Run by
// tests/harness.sh, headless: nothing is drawn on a screen.
ShellRoot {
  id: harness

  readonly property string repo: Quickshell.env("OW_REPO")
  readonly property string pluginId: "io.github.davidgudovic.omawrapped"
  readonly property string glyph: "\udb85\udd4d"

  property var widget: null
  property var failures: []
  property int step: 0

  // What Service.qml offers the widget.
  QtObject {
    id: fakeService
    property bool ready: false
    property real todayMs: 0
  }

  // What the bar offers a widget and its buttons.
  QtObject {
    id: fakeBar
    property bool vertical: false
    property int barSize: 26
    property string fontFamily: "monospace"
    property color barForeground: "#bebebe"
    property color urgent: "#d35f5f"
    property bool foregroundAnimationEnabled: false
    property bool serviceLoaded: false
    property string tooltip: ""
    readonly property var shell: ({ serviceFor: function(id) { return fakeBar.serviceLoaded && id === harness.pluginId ? fakeService : null } })
    function showTooltip(target, text) { tooltip = text }
    function hideTooltip(target) { tooltip = "" }
    function registerClickTarget(target) {}
    function unregisterClickTarget(target) {}
  }

  function equal(label, actual, expected) {
    var ok = actual === expected
    console.log("OW-CHECK " + (ok ? "ok  " : "FAIL") + " " + label + " = " + JSON.stringify(actual) + " (expected " + JSON.stringify(expected) + ")")
    if (!ok) failures.push(label)
  }

  function truthy(label, actual) {
    console.log("OW-CHECK " + (actual ? "ok  " : "FAIL") + " " + label)
    if (!actual) failures.push(label)
  }

  function make(properties) {
    var component = Qt.createComponent("file://" + repo + "/BarWidget.qml")
    if (component.status !== Component.Ready) {
      console.log("OW-RESULT FAIL load: " + component.errorString())
      Qt.quit()
      return null
    }
    return component.createObject(null, properties)
  }

  readonly property var script: [
    [100, function() {
      equal("no service yet: icon alone", widget.children.length > 0 && widget.today, "")
      equal("no service yet: tooltip", widget.tooltip, "OmaWrapped is starting…")
      equal("command", widget.command, repo + "/bin/omawrapped")
      fakeBar.serviceLoaded = true
      fakeBar.serviceLoadedChanged()
      // The bar swaps the facade when services change; the widget follows.
      widget.bar = null
      widget.bar = fakeBar
      fakeService.todayMs = 2 * 3600000 + 5 * 60000 + 30000
      fakeService.ready = true
    }],
    [100, function() {
      equal("today", widget.today, "2h 05m")
      equal("time shown", widget.showTime, true)
      equal("tooltip", widget.tooltip,
        "2h 05m of screen time today · click: week card · right: month card · middle: copy or show it")
      equal("the icon is the chart box", widget.glyph, glyph)
      truthy("has a width", widget.implicitWidth > 40)
      equal("as tall as the bar", widget.implicitHeight, 26)
      harness.wideWidth = widget.implicitWidth
      widget.settings = { id: pluginId, display: "icon" }
    }],
    [100, function() {
      equal("icon only when asked", widget.showTime, false)
      truthy("icon is narrower than icon and time", widget.implicitWidth > 0 && widget.implicitWidth < harness.wideWidth)
      widget.settings = { id: pluginId }
      fakeBar.vertical = true
    }],
    [100, function() {
      equal("icon only on a vertical bar", widget.showTime, false)
      fakeBar.vertical = false
      // Nothing is recorded in the harness, so the command fails: the
      // widget must come back from busy without an error of its own.
      widget.handlePress(Qt.LeftButton)
      equal("busy while the card is drawn", widget.busy, true)
      equal("busy tooltip", widget.tooltip, "Drawing your card…")
      // A second click while busy starts nothing: one notification below.
      widget.handlePress(Qt.RightButton)
    }],
    [4000, function() {
      equal("not busy once the command is done", widget.busy, false)
      var sent = notifications()
      equal("the failure is notified once", sent.length, 1)
      equal("under the app's name, with its icon", (sent[0] || []).slice(0, 5).join("|"),
        "--app-name|OmaWrapped|-g|" + glyph + "|OmaWrapped")
      truthy("and says why: " + (sent[0] || [])[5],
        /^Nothing was recorded for the last 7 days \(.+\)\.$/.test((sent[0] || [])[5] || ""))
      equal("nothing after the reason", (sent[0] || []).length, 6)
      // Middle click: Omarchy's menu, here dismissed without a choice.
      widget.handlePress(Qt.MiddleButton)
      equal("the menu does not dim the widget", widget.busy, false)
    }],
    [2500, function() {
      equal("the menu was asked for under the plugin's name", lines("menu-asked.txt")[0], "OmaWrapped")
      equal("it offers copying the card", lines("menu-asked.txt").join("|").indexOf("\tCopy card") !== -1, true)
      equal("and showing it in its folder", lines("menu-asked.txt").join("|").indexOf("\tShow in folder") !== -1, true)
      equal("a dismissed menu is not an error", notifications().length, 1)
      // Now something is chosen, but there is no card to copy yet.
      choice.setText("Copy card\n")
      widget.handlePress(Qt.MiddleButton)
    }],
    [2500, function() {
      var sent = notifications()
      equal("a choice that fails is reported", sent.length, 2)
      truthy("with the reason: " + (sent[1] || [])[5], /^There is no card yet\./.test((sent[1] || [])[5] || ""))
      equal("no desktop program was started", lines("started.txt").join(""), "")
      // From here on there is something to draw: a month of sample days.
      sample.running = true
    }],
    [1500, function() { widget.handlePress(Qt.LeftButton) }],
    [5000, function() {
      equal("the card is done", widget.busy, false)
      var sent = notifications()
      var said = sent[2] || []
      var card = said[said.length - 1] || ""
      equal("a drawn card is announced", sent.length, 3)
      equal("as copied, with the card as its picture", said.slice(4, 7).join("|"), "--image|" + card + "|Card copied")
      truthy("the card is in the Pictures folder: " + card, /\/home\/Pictures\/omawrapped-\d{4}-\d\d-\d\d\.png$/.test(card))
      equal("a click on the notification shows it in its folder", said.slice(-4).join("|"),
        "--exec|" + repo + "/bin/omawrapped|show|" + card)
      var started = lines("started.txt")
      equal("the picture went to the clipboard", started.indexOf("wl-copy --type image/png") !== -1, true)
      equal("and the card was opened", started.indexOf("xdg-open " + card) !== -1, true)
      harness.card = card
      choice.setText("Show in folder\n")
      widget.handlePress(Qt.MiddleButton)
    }],
    [2500, function() {
      equal("the menu shows the card in its folder",
        lines("started.txt").indexOf("uwsm-app -- nautilus --select " + harness.card) !== -1, true)
      equal("without another notification", notifications().length, 3)
      widget.destroy()
    }]
  ]

  property string card: ""

  // tests/make_sample.py, writing into this run's data folder.
  Process {
    id: sample
    command: ["/usr/bin/python3", "-B", harness.repo + "/tests/make_sample.py", Quickshell.env("OW_RUN")]
  }

  // The lines of a file the stand-ins in tests/harness.sh write.
  function lines(name) {
    var view = fileComponent.createObject(harness, { path: Quickshell.env("OW_RUN") + "/" + name })
    var text = view.text()
    view.destroy()
    return text === "" ? [] : text.replace(/\n$/, "").split("\n")
  }

  // Every notification sent so far, each as its list of arguments.
  function notifications() {
    var calls = []
    var call = []
    var all = lines("notification.txt")
    for (var i = 0; i < all.length; i++) {
      if (all[i] === "==") { calls.push(call); call = [] }
      else call.push(all[i])
    }
    return calls
  }

  FileView {
    id: choice
    path: Quickshell.env("OW_RUN") + "/menu-choice"
    blockWrites: true
    printErrors: false
  }

  Component {
    id: fileComponent
    FileView { blockLoading: true; printErrors: false; watchChanges: false }
  }

  property real wideWidth: 0

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

  Component.onCompleted: {
    widget = make({ bar: fakeBar, moduleName: pluginId, settings: { id: pluginId } })
    if (widget) next()
  }
}

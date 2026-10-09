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
  readonly property string glyph: ""

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
      equal("tooltip", widget.tooltip, "2h 05m of screen time today · click: card of the last 7 days · right click: last 30 days")
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
      // tests/harness.sh puts a stand-in omarchy-notification-send first in
      // PATH; it writes its arguments, one per line, to this file.
      var view = fileComponent.createObject(harness, { path: Quickshell.env("OW_RUN") + "/notification.txt" })
      var sent = view.text().split("\n")
      view.destroy()
      equal("failure is notified once, under the app's name", sent.slice(0, 5).join("|"),
        "--app-name|OmaWrapped|-g|" + glyph + "|OmaWrapped could not make the card")
      truthy("the notification says why: " + sent[5], /^Nothing was recorded for the last 7 days \(.+\)\.$/.test(sent[5] || ""))
      equal("nothing after the reason", sent.slice(6).join(""), "")
      widget.destroy()
    }]
  ]

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

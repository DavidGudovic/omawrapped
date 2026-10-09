import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import "Tracker.js" as Tracker

// The bar widget: today's screen time, and a click away from the card.
//
// A copy exists per monitor and is rebuilt with the bar, so it keeps
// nothing. The counting happens in Service.qml; this only shows its total
// and runs the `omawrapped` command when asked:
//
//   left click    the card of the last 7 days
//   right click   the card of the last 30 days
//   middle click  Omarchy's menu, to copy the last card or show its folder
//
// A card is opened, put on the clipboard as a picture and announced by the
// command itself. Only what goes wrong is reported from here.
BarWidget {
  id: root
  moduleName: "io.github.davidgudovic.omawrapped"

  // Null while the service is still loading and under a replacement bar.
  readonly property var service: (bar && bar.shell && typeof bar.shell.serviceFor === "function")
    ? (bar.shell.serviceFor(root.moduleName) || null) : null
  readonly property bool live: service !== null && service.ready === true

  readonly property bool showTime: setting("display", "time") !== "icon" && !vertical
  // Material Design "chart box", from the same icon set as the bar's own
  // icons: a card with a chart in it. U+F154D, written as its two halves.
  readonly property string glyph: "󱕍"
  readonly property string today: live ? Tracker.formatDuration(service.todayMs) : ""
  readonly property string command: decodeURIComponent(String(Qt.resolvedUrl("bin/omawrapped")).replace(/^file:\/\//, ""))
  readonly property bool busy: card.pending

  readonly property string tooltip: {
    if (busy) return "Drawing your card…"
    if (!live) return "OmaWrapped is starting…"
    return today + " of screen time today · click: week card · right: month card · middle: copy or show it"
  }

  function report(problem) {
    Quickshell.execDetached(["omarchy-notification-send", "--app-name", "OmaWrapped", "-g", root.glyph,
      "OmaWrapped", problem])
  }

  function handlePress(button) {
    if (button === Qt.LeftButton) card.start(["card", "--week", "--open", "--copy", "image", "--notify"])
    else if (button === Qt.RightButton) card.start(["card", "--month", "--open", "--copy", "image", "--notify"])
    else if (button === Qt.MiddleButton) menu.start(["menu"])
  }

  implicitWidth: showTime ? label.implicitWidth : icon.implicitWidth
  implicitHeight: showTime ? label.implicitHeight : icon.implicitHeight

  // One run of the command. `pending` is true from the click until the
  // command is over: Process.running turns true only once it has started,
  // which is too late to stop a second click.
  component Command: Process {
    property bool pending: false

    function start(args) {
      if (pending) return
      pending = true
      command = [root.command].concat(args)
      running = true
    }

    stderr: StdioCollector { id: errors; waitForEnd: true }
    // The first line the command printed says why it failed.
    onExited: function(exitCode) {
      pending = false
      var said = String(errors.text || "").trim().split("\n")[0]
      if (exitCode !== 0) root.report(said || "Run `omawrapped` in a terminal to see why.")
    }
    // A command that cannot be started never exits; it only stops running.
    onRunningChanged: if (!running && pending) {
      pending = false
      root.report("The omawrapped command could not be started.")
    }
  }

  Command { id: card }
  // The menu stays open until something is picked, so it runs on its own
  // and does not dim the widget.
  Command { id: menu }

  WidgetButton {
    id: label
    anchors.fill: parent
    visible: root.showTime
    bar: root.bar
    text: root.today === "" ? root.glyph : root.glyph + " " + root.today
    dimmed: root.busy
    tooltipText: root.tooltip
    onPressed: function(button) { root.handlePress(button) }
  }

  BarIconButton {
    id: icon
    anchors.fill: parent
    visible: !root.showTime
    bar: root.bar
    text: root.glyph
    dimmed: root.busy
    tooltipText: root.tooltip
    onPressed: function(button) { root.handlePress(button) }
  }
}

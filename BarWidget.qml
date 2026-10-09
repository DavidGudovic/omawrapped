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
//   middle click  Omarchy's menu: today so far, the last card, a pause
//
// A card is opened, put on the clipboard as a picture and announced by the
// command itself, and so is the reason when it fails. Nothing the command
// printed is passed on from here: a notification sent from the widget goes
// through a program's arguments, which every user of the machine can read,
// so it only ever says one of two fixed sentences.
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
  // The user paused the counting, in the menu or in the settings.
  readonly property bool paused: live && service.paused === true
  // What stands beside the icon: nothing while the service is starting.
  readonly property string caption: !live ? "" : (paused ? "paused" : today)
  readonly property string command: decodeURIComponent(String(Qt.resolvedUrl("bin/omawrapped")).replace(/^file:\/\//, ""))
  readonly property bool busy: card.pending

  readonly property string tooltip: {
    if (busy) return "Drawing your card…"
    if (!live) return "OmaWrapped is starting…"
    if (paused) return "Counting is paused at " + today + " today · middle click: resume, and more"
    return today + " of screen time today · click: week card · right: month card · middle: more"
  }

  // What the widget itself can say, word for word.
  readonly property string notFinished: "It could not finish. Run `omawrapped status` in a terminal to see why."
  readonly property string notStarted: "The omawrapped command could not be started."
  // The command's exit status when it has said on the desktop why it failed.
  readonly property int saidItself: 3

  function report(sentence) {
    Quickshell.execDetached(["omarchy-notification-send", "--app-name", "OmaWrapped", "-g", root.glyph,
      "OmaWrapped", sentence])
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

    // A command that failed has normally said why itself. This is for the
    // one that could not: no notification service, or a broken install.
    onExited: function(exitCode) {
      pending = false
      if (exitCode !== 0 && exitCode !== root.saidItself) root.report(root.notFinished)
    }
    // A command that cannot be started never exits; it only stops running.
    onRunningChanged: if (!running && pending) {
      pending = false
      root.report(root.notStarted)
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
    text: root.caption === "" ? root.glyph : root.glyph + " " + root.caption
    dimmed: root.busy || root.paused
    tooltipText: root.tooltip
    onPressed: function(button) { root.handlePress(button) }
  }

  BarIconButton {
    id: icon
    anchors.fill: parent
    visible: !root.showTime
    bar: root.bar
    text: root.glyph
    dimmed: root.busy || root.paused
    tooltipText: root.tooltip
    onPressed: function(button) { root.handlePress(button) }
  }
}

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import "Tracker.js" as Tracker

// The bar widget: today's screen time, and a click away from the card.
//
// A copy exists per monitor and is rebuilt with the bar, so it keeps
// nothing. The counting happens in Service.qml; this only shows its total
// and runs `omawrapped card` when asked: left click for the week, right
// click for the month.
BarWidget {
  id: root
  moduleName: "io.github.davidgudovic.omawrapped"

  // Null while the service is still loading and under a replacement bar.
  readonly property var service: (bar && bar.shell && typeof bar.shell.serviceFor === "function")
    ? (bar.shell.serviceFor(root.moduleName) || null) : null
  readonly property bool live: service !== null && service.ready === true

  readonly property bool showTime: setting("display", "time") !== "icon" && !vertical
  readonly property string glyph: "\uf06b"
  readonly property string today: live ? Tracker.formatDuration(service.todayMs) : ""
  readonly property string command: decodeURIComponent(String(Qt.resolvedUrl("bin/omawrapped")).replace(/^file:\/\//, ""))
  // True from the click until the command is over. Process.running turns
  // true only once the command has started, which is too late to stop a
  // second click.
  property bool busy: false

  readonly property string tooltip: {
    if (busy) return "Drawing your card…"
    if (!live) return "OmaWrapped is starting…"
    return today + " of screen time today · click: card of the last 7 days · right click: last 30 days"
  }

  function makeCard(period) {
    if (busy) return
    busy = true
    card.command = [root.command, "card", period, "--open"]
    card.running = true
  }

  // The card opens in the image viewer when it is done, so only a failure
  // is worth a notification.
  function finish(error) {
    busy = false
    if (error) Quickshell.execDetached(["omarchy-notification-send", "--app-name", "OmaWrapped", "-g", root.glyph,
      "OmaWrapped could not make the card", error])
  }

  function handlePress(button) {
    if (button === Qt.LeftButton) makeCard("--week")
    else if (button === Qt.RightButton) makeCard("--month")
  }

  implicitWidth: showTime ? label.implicitWidth : icon.implicitWidth
  implicitHeight: showTime ? label.implicitHeight : icon.implicitHeight

  Process {
    id: card
    stderr: StdioCollector { id: cardErrors; waitForEnd: true }
    // The first line the command printed says why it failed.
    onExited: function(exitCode) {
      var said = String(cardErrors.text || "").trim().split("\n")[0]
      root.finish(exitCode === 0 ? "" : (said || "Run `omawrapped card` in a terminal to see why."))
    }
    // A command that cannot be started never exits; it only stops running.
    onRunningChanged: if (!running && root.busy) root.finish("The omawrapped command could not be started.")
  }

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

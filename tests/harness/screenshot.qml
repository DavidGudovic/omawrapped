import QtQuick
import QtQuick.Window
import Quickshell
import qs.Commons

// Draws the real BarWidget.qml on a strip the colour of the bar and saves
// the picture. Run by tests/screenshots.sh, headless, to make
// docs/bar-widget.png. What is in the picture is the widget's own code and
// the shell's own Ui kit in the theme the script set up; the only stand-ins
// are the bar it sits in and a service that reports 3h 07m.
ShellRoot {
  id: shot

  readonly property string repo: Quickshell.env("OW_REPO")
  readonly property string out: Quickshell.env("OW_OUT")
  readonly property string pluginId: "io.github.davidgudovic.omawrapped"
  readonly property int padding: Style.space(10)

  QtObject {
    id: fakeService
    property bool ready: true
    property bool paused: false
    property real todayMs: 3 * 3600000 + 7 * 60000
  }

  // What the bar offers a widget: its colours, its font, its size.
  QtObject {
    id: fakeBar
    property bool vertical: false
    property int barSize: Style.bar.sizeHorizontal
    // Omarchy's default font, which is what `monospace` means on a stock install.
    property string fontFamily: "JetBrainsMono Nerd Font"
    property color barForeground: Color.bar.text
    property color urgent: Color.bar.active
    property bool foregroundAnimationEnabled: false
    readonly property var shell: ({ serviceFor: function(id) { return id === shot.pluginId ? fakeService : null } })
    function showTooltip(target, text) {}
    function hideTooltip(target) {}
    function registerClickTarget(target) {}
    function unregisterClickTarget(target) {}
  }

  Window {
    visible: true
    width: stage.width
    height: stage.height
    color: Color.bar.background

    Rectangle {
      id: stage
      width: (holder.children.length > 0 ? holder.children[0].implicitWidth : 0) + shot.padding * 2
      height: fakeBar.barSize
      color: Color.bar.background

      Item {
        id: holder
        x: shot.padding
        width: children.length > 0 ? children[0].implicitWidth : 0
        height: parent.height
      }
    }
  }

  Component.onCompleted: {
    var component = Qt.createComponent("file://" + repo + "/BarWidget.qml")
    if (component.status !== Component.Ready) {
      console.log("OW-SHOT FAIL load: " + component.errorString())
      Qt.quit()
      return
    }
    var widget = component.createObject(holder, { bar: fakeBar, moduleName: pluginId, settings: { id: pluginId } })
    widget.width = Qt.binding(function() { return widget.implicitWidth })
    widget.height = Qt.binding(function() { return holder.height })
    grab.start()
  }

  Timer {
    id: grab
    interval: 600
    onTriggered: stage.grabToImage(function(result) {
      console.log("OW-SHOT " + (result.saveToFile(shot.out) ? "saved " : "FAIL could not save ") + shot.out)
      Qt.quit()
    })
  }
}

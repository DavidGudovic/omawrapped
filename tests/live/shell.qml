import QtQuick
import Quickshell
import Quickshell.Io

// Runs the real Service.qml against the live session for a while and
// prints what it saw and what it recorded. Started by tests/live.sh as a
// second, windowless Quickshell with a throwaway data directory: it draws
// nothing, takes no focus and does not touch the installed plugin's data.
ShellRoot {
  id: live

  readonly property string repo: Quickshell.env("OW_REPO")
  readonly property int seconds: parseInt(Quickshell.env("OW_SECONDS") || "300", 10)
  readonly property string pluginId: "io.github.davidgudovic.omawrapped"
  // The shortest idle time the plugin accepts, so a short run can see one.
  readonly property var fakeShell: ({ barConfig: { layout: { right: [{ id: pluginId, idleSeconds: 30 }] } } })

  property var service: null

  function stamp() {
    return Qt.formatTime(new Date(), "HH:mm:ss")
  }

  function note(what) {
    console.log("OW-LIVE " + stamp() + " " + what)
  }

  Connections {
    target: live.service
    function onFocusedAppChanged() { live.note("focus   " + (live.service.focusedApp || "(no window)")) }
    function onUserIdleChanged() { live.note("idle    " + live.service.userIdle) }
    function onSessionLockedChanged() { live.note("locked  " + live.service.sessionLocked) }
  }

  Timer {
    interval: live.seconds * 1000
    running: true
    onTriggered: {
      live.service.flush()
      var path = live.service.dayPath(Qt.formatDate(new Date(), "yyyy-MM-dd"))
      var view = fileComponent.createObject(live, { path: path })
      live.note("recorded " + view.text().trim())
      view.destroy()
      live.note("status  " + live.service.statusJson())
      Qt.quit()
    }
  }

  Component {
    id: fileComponent
    FileView { blockLoading: true; printErrors: false; watchChanges: false }
  }

  Component.onCompleted: {
    var component = Qt.createComponent("file://" + repo + "/Service.qml")
    if (component.status !== Component.Ready) {
      console.log("OW-LIVE load failed: " + component.errorString())
      Qt.quit()
      return
    }
    service = component.createObject(null, { shell: fakeShell })
    note("started for " + seconds + "s; away after " + service.idleSeconds + "s without input; focus "
      + (service.focusedApp || "(no window)"))
  }
}

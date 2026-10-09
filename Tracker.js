.pragma library

// The sampler's accounting. Nothing here touches the session or the disk:
// Service.qml feeds it "this app is focused / the user is away" together
// with the time, and takes back per-day counters to merge into the files.
//
// A day is the only thing that is ever stored:
//
//   { version, date, active_ms, hours_ms[24], apps_ms{app: ms}, switches }
//
// active_ms is the time the session was in use, apps_ms the part of it that
// had a focused window, hours_ms the same time by local hour of day. Window
// titles never reach this file, so they cannot end up in a counter.

var VERSION = 1
var MAX_APPS_PER_DAY = 256
var MAX_APP_LENGTH = 96
// How long an app must hold focus before moving to it counts as a switch.
// Focus passes over windows all the time without anyone switching: when a
// workspace changes, a window closes, or the screensaver starts.
var SWITCH_DWELL_MS = 1000

function emptyDay(date) {
  var hours = []
  for (var i = 0; i < 24; i++) hours.push(0)
  return { version: VERSION, date: date, active_ms: 0, hours_ms: hours, apps_ms: {}, switches: 0 }
}

function pad2(n) {
  return n < 10 ? "0" + n : String(n)
}

// Local calendar date of a timestamp, as the day files are named.
function dateKey(ms) {
  var d = new Date(ms)
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
}

// First local hour boundary after ms. When clocks go back, the repeated
// hour counts as one long hour; when they go forward, the missing hour is
// skipped. The fallback only guards against a Date that does not advance.
function nextHourStart(ms) {
  var d = new Date(ms)
  var next = new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours() + 1, 0, 0, 0).getTime()
  return next > ms ? next : ms + 3600000
}

// An app id as it is stored: trimmed, bounded, and never a name that
// would land on Object.prototype instead of in the counters.
function cleanApp(app) {
  var id = String(app === undefined || app === null ? "" : app).replace(/[\x00-\x1f\x7f]/g, "").trim()
  if (id.length > MAX_APP_LENGTH) id = id.substring(0, MAX_APP_LENGTH)
  return id === "__proto__" ? "" : id
}

// What an app is counted under. A browser's app window is named
// chrome-<host>__<path>-<Profile>; only the host is kept, as web:<host>,
// because the path can be a document's address and the profile a name.
function appKey(app) {
  var id = cleanApp(app)
  var web = /^[a-z]+-([^_\/]+)__.*-[^-]+$/.exec(id)
  return web && web[1].indexOf(".") !== -1 ? "web:" + web[1].toLowerCase() : id
}

function ownNumber(object, key) {
  var value = Object.prototype.hasOwnProperty.call(object, key) ? object[key] : 0
  return typeof value === "number" && isFinite(value) && value > 0 ? value : 0
}

function dayFor(days, date) {
  if (!Object.prototype.hasOwnProperty.call(days, date)) days[date] = emptyDay(date)
  return days[date]
}

function addAppTime(day, app, ms) {
  if (!app) return
  var known = Object.prototype.hasOwnProperty.call(day.apps_ms, app)
  // Past the cap the time still counts as active, just not under a name.
  if (!known && Object.keys(day.apps_ms).length >= MAX_APPS_PER_DAY) return
  day.apps_ms[app] = (known ? day.apps_ms[app] : 0) + ms
}

// Adds the stretch [startMs, endMs) to days, split at local hour boundaries
// so that each piece lands in the right day and hour.
function addInterval(days, startMs, endMs, app) {
  var from = startMs
  while (from < endMs) {
    var to = Math.min(endMs, nextHourStart(from))
    var day = dayFor(days, dateKey(from))
    var ms = to - from
    day.active_ms += ms
    day.hours_ms[new Date(from).getHours()] += ms
    addAppTime(day, app, ms)
    from = to
  }
}

// cursor is where the open stretch began; app and counting describe it.
// For the switch counter: runApp has had focus for runMs of counted time
// in a row, and lastApp is the app the user was last settled in.
function create() {
  return { days: {}, cursor: null, app: "", counting: false, lastApp: "", runApp: "", runMs: 0 }
}

// Counts a switch once the app of the stretch just closed has held focus
// for SWITCH_DWELL_MS and is not the app the user was settled in before.
function noteRun(state, nowMs, elapsed) {
  if (!state.app) return
  if (state.runApp !== state.app) {
    state.runApp = state.app
    state.runMs = 0
  }
  state.runMs += elapsed
  if (state.runMs < SWITCH_DWELL_MS || state.runApp === state.lastApp) return
  if (state.lastApp) dayFor(state.days, dateKey(nowMs)).switches += 1
  state.lastApp = state.runApp
}

// Closes the open stretch at nowMs under the conditions it was opened with,
// then opens the next one with the conditions given. A stretch longer than
// maxGapMs is dropped whole: the timer that calls this did not run, so the
// machine was suspended or stalled, and nobody was looking at it.
function observe(state, nowMs, app, away, maxGapMs) {
  if (state.cursor !== null && state.counting) {
    var elapsed = nowMs - state.cursor
    if (elapsed > 0 && elapsed <= maxGapMs) {
      addInterval(state.days, state.cursor, nowMs, state.app)
      noteRun(state, nowMs, elapsed)
    }
  }
  state.cursor = nowMs
  state.app = appKey(app)
  state.counting = !away
}

function isEmptyDay(day) {
  return day.active_ms === 0 && day.switches === 0
}

// Hands over everything counted since the last call, by date, and starts
// again from nothing. The open stretch stays open.
function takePending(state) {
  var out = {}
  for (var date in state.days) {
    if (!isEmptyDay(state.days[date])) out[date] = state.days[date]
  }
  state.days = {}
  return out
}

// Puts a day back that could not be written, so the next flush retries it.
function restorePending(state, day) {
  state.days[day.date] = mergeDay(Object.prototype.hasOwnProperty.call(state.days, day.date)
    ? state.days[day.date] : null, day)
}

function pendingDay(state, date) {
  return Object.prototype.hasOwnProperty.call(state.days, date) ? state.days[date] : null
}

// The stored day as an object, or null when the text is not a day file of
// this version for this date. Unknown keys are dropped and every counter is
// checked, so one damaged file cannot poison the sums.
function parseDay(text, date) {
  var raw
  try {
    raw = JSON.parse(text)
  } catch (error) {
    return null
  }
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return null
  if (raw.version !== VERSION || raw.date !== date) return null
  var day = emptyDay(date)
  day.active_ms = ownNumber(raw, "active_ms")
  day.switches = Math.floor(ownNumber(raw, "switches"))
  if (Array.isArray(raw.hours_ms)) {
    for (var h = 0; h < 24; h++) day.hours_ms[h] = ownNumber(raw.hours_ms, h)
  }
  var apps = raw.apps_ms
  if (apps !== null && typeof apps === "object" && !Array.isArray(apps)) {
    for (var app in apps) {
      var id = cleanApp(app)
      var ms = ownNumber(apps, app)
      if (id && ms > 0) addAppTime(day, id, ms)
    }
  }
  return day
}

// What the text of a day file allows the next flush to do:
// { ok: true, day } to merge into it (day is null for an empty file), or
// { ok: false } to leave it alone. A file that is not a day of this version
// is somebody's data all the same: a later version's after a downgrade, or
// one edited by hand. It is never overwritten.
function readStored(text, date) {
  if (String(text).trim() === "") return { ok: true, day: null }
  var day = parseDay(text, date)
  return { ok: day !== null, day: day }
}

// stored + delta as a new day. stored may be null (no file yet).
function mergeDay(stored, delta) {
  var day = emptyDay(delta.date)
  var parts = stored ? [stored, delta] : [delta]
  for (var p = 0; p < parts.length; p++) {
    var part = parts[p]
    day.active_ms += part.active_ms
    day.switches += part.switches
    for (var h = 0; h < 24; h++) day.hours_ms[h] += part.hours_ms[h]
    for (var app in part.apps_ms) addAppTime(day, app, part.apps_ms[app])
  }
  return day
}

function serialize(day) {
  return JSON.stringify(day) + "\n"
}

// "3h 07m", "42m", "0m": what the bar shows.
function formatDuration(ms) {
  var value = Number(ms)
  var minutes = Math.floor((isFinite(value) && value > 0 ? value : 0) / 60000)
  var hours = Math.floor(minutes / 60)
  var rest = minutes % 60
  return hours > 0 ? hours + "h " + pad2(rest) + "m" : rest + "m"
}

// The key an app is matched under in an ignore list: the key it is counted
// under, in lower case, so that the class `hyprctl clients` shows can be
// typed in any case and a web app is matched by its site.
function ignoreKey(app) {
  return appKey(app).toLowerCase()
}

// "a, b ,c" -> ["a", "b", "c"]; anything else that is not a list -> [].
function parseList(value) {
  var parts = Array.isArray(value) ? value : String(value === undefined || value === null ? "" : value).split(",")
  var out = []
  for (var i = 0; i < parts.length; i++) {
    var item = cleanApp(parts[i])
    if (item && out.indexOf(item) === -1) out.push(item)
  }
  return out
}

// The plugin's entry in the bar layout, or {} while it has none. Services
// are not handed their settings, only the bar configuration they live in.
// That configuration reaches a plugin with its lists in a form that is not
// an array, so it is first taken through JSON, which makes them arrays.
function entryFor(barConfig, id) {
  var config
  try {
    config = JSON.parse(JSON.stringify(barConfig))
  } catch (error) {
    return {}
  }
  var layout = config && typeof config === "object" ? config.layout : null
  if (!layout || typeof layout !== "object") return {}
  var sections = ["left", "center", "right"]
  for (var s = 0; s < sections.length; s++) {
    var list = layout[sections[s]]
    if (!Array.isArray(list)) continue
    for (var i = 0; i < list.length; i++) {
      if (list[i] && typeof list[i] === "object" && list[i].id === id) return list[i]
    }
  }
  return {}
}

// True when Hyprland reports a session lock on any monitor. The lock is
// one of the reasons a monitor cannot go solitary, which is the only place
// the compositor says so. Each argument entry is one monitor's
// solitaryBlockedBy; the shell hands these over as list-like objects that
// are not arrays, so they are walked by index.
function anyLocked(blockerLists) {
  for (var i = 0; i < blockerLists.length; i++) {
    var blockers = blockerLists[i]
    if (!blockers || typeof blockers === "string" || typeof blockers.length !== "number") continue
    for (var j = 0; j < blockers.length; j++) {
      if (blockers[j] === "LOCK") return true
    }
  }
  return false
}

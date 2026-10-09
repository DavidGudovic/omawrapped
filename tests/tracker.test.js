process.env.TZ = "Europe/Belgrade"

const { describe, test } = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")

// Tracker.js is a QML library, not a Node module: drop the QML-only lines,
// run the rest in a fresh context and hand back its top-level names.
function loadTracker() {
  const file = path.join(__dirname, "..", "Tracker.js")
  const code = fs.readFileSync(file, "utf8")
    .split(/\r?\n/)
    .filter((line) => !line.startsWith(".pragma") && !line.startsWith(".import"))
    .join("\n")
  const context = vm.createContext({ Date, JSON, Math, Object, Array, String, isFinite, parseInt })
  vm.runInContext(code, context, { filename: file })
  const names = []
  for (const match of code.matchAll(/^function\s+([A-Za-z_$][\w$]*)\s*\(/gm)) names.push(match[1])
  for (const match of code.matchAll(/^var\s+([A-Z][A-Z0-9_]*)\s*=/gm)) names.push(match[1])
  const api = {}
  for (const name of names) {
    assert.notEqual(context[name], undefined, "Tracker.js did not define " + name)
    api[name] = context[name]
  }
  return { tracker: api, context }
}

const { tracker, context } = loadTracker()

const SEC = 1000
const MIN = 60 * SEC
const HOUR = 60 * MIN
const GAP = 60 * SEC
const DATE = "2026-05-14"

// Local wall-clock time; the month is 1-12 here.
function at(year, month, day, hour = 0, minute = 0, second = 0, ms = 0) {
  return new Date(year, month - 1, day, hour, minute, second, ms).getTime()
}

const BASE = at(2026, 5, 14, 10, 0, 0)

// Objects built inside the vm have another realm's prototypes.
function plain(value) {
  return JSON.parse(JSON.stringify(value))
}

function sum(values) {
  return values.reduce((total, value) => total + value, 0)
}

// 24 hour buckets, zero except for the given { hour: ms }.
function hoursWith(buckets) {
  const hours = new Array(24).fill(0)
  for (const hour of Object.keys(buckets)) hours[Number(hour)] = buckets[hour]
  return hours
}

function makeDay(date, fields) {
  const day = tracker.emptyDay(date)
  day.active_ms = fields.active || 0
  day.hours_ms = hoursWith(fields.hours || {})
  day.apps_ms = Object.assign({}, fields.apps || {})
  day.switches = fields.switches || 0
  return day
}

// steps are [offset from BASE in ms, app, away]
function run(steps, gap = GAP) {
  const state = tracker.create()
  for (const [offset, app, away] of steps) tracker.observe(state, BASE + offset, app, away === true, gap)
  return state
}

function pending(state, date = DATE) {
  return tracker.pendingDay(state, date)
}

function switchCount(state) {
  return sum(Object.values(state.days).map((day) => day.switches))
}

// A day file as text; an undefined override leaves that key out.
function dayText(overrides = {}) {
  const base = {
    version: tracker.VERSION,
    date: DATE,
    active_ms: 0,
    hours_ms: hoursWith({}),
    apps_ms: {},
    switches: 0,
  }
  return JSON.stringify(Object.assign(base, overrides))
}

describe("environment", () => {
  test("runs in Europe/Belgrade and the loader exposes the library", () => {
    assert.equal(new Date(2026, 0, 15, 12).getTimezoneOffset(), -60)
    assert.equal(new Date(2026, 6, 15, 12).getTimezoneOffset(), -120)
    for (const name of ["VERSION", "MAX_APPS_PER_DAY", "MAX_APP_LENGTH", "SWITCH_DWELL_MS"]) {
      assert.equal(typeof tracker[name], "number", name)
    }
    for (const name of ["emptyDay", "dateKey", "nextHourStart", "cleanApp", "addInterval", "create", "observe",
      "takePending", "restorePending", "pendingDay", "parseDay", "mergeDay", "serialize", "formatDuration",
      "parseList", "entryFor", "anyLocked"]) {
      assert.equal(typeof tracker[name], "function", name)
    }
  })
})

describe("dateKey and nextHourStart", () => {
  test("dateKey is the local calendar date, zero padded", () => {
    assert.equal(tracker.dateKey(at(2026, 5, 14, 10, 30)), "2026-05-14")
    assert.equal(tracker.dateKey(at(2026, 1, 5, 12)), "2026-01-05")
    assert.equal(tracker.dateKey(at(2026, 12, 31, 12)), "2026-12-31")
    // 22:30 UTC the day before: local time decides, not UTC
    assert.equal(tracker.dateKey(at(2026, 5, 14, 0, 30)), "2026-05-14")
  })

  test("dateKey just before midnight is still the old day", () => {
    assert.equal(tracker.dateKey(at(2026, 5, 14, 23, 59, 59, 999)), "2026-05-14")
    assert.equal(tracker.dateKey(at(2026, 5, 15, 0, 0, 0, 0)), "2026-05-15")
  })

  test("nextHourStart is the following full hour", () => {
    assert.equal(tracker.nextHourStart(at(2026, 5, 14, 10, 30)), at(2026, 5, 14, 11, 0))
    assert.equal(tracker.nextHourStart(at(2026, 5, 14, 10, 59, 59, 999)), at(2026, 5, 14, 11, 0))
    assert.equal(tracker.nextHourStart(at(2026, 5, 14, 10, 0, 0, 1)), at(2026, 5, 14, 11, 0))
  })

  test("nextHourStart just before midnight is midnight", () => {
    assert.equal(tracker.nextHourStart(at(2026, 5, 14, 23, 59, 59)), at(2026, 5, 15, 0, 0))
    assert.equal(tracker.nextHourStart(at(2026, 12, 31, 23, 30)), at(2027, 1, 1, 0, 0))
  })

  test("nextHourStart on an exact boundary is one hour later, strictly greater", () => {
    const boundary = at(2026, 5, 14, 10, 0)
    assert.equal(tracker.nextHourStart(boundary), at(2026, 5, 14, 11, 0))
    assert.equal(tracker.nextHourStart(boundary) - boundary, HOUR)
    const midnight = at(2026, 5, 15, 0, 0)
    assert.equal(tracker.nextHourStart(midnight), at(2026, 5, 15, 1, 0))
  })
})

describe("addInterval", () => {
  test("an interval inside one hour lands in one bucket", () => {
    const days = {}
    tracker.addInterval(days, at(2026, 5, 14, 10, 10), at(2026, 5, 14, 10, 40), "firefox")
    assert.deepEqual(Object.keys(days), [DATE])
    const day = days[DATE]
    assert.equal(day.date, DATE)
    assert.equal(day.active_ms, 30 * MIN)
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 10: 30 * MIN }))
    assert.deepEqual(plain(day.apps_ms), { firefox: 30 * MIN })
    assert.equal(day.switches, 0)
  })

  test("an interval across an hour boundary is split between two buckets", () => {
    const days = {}
    tracker.addInterval(days, at(2026, 5, 14, 10, 50), at(2026, 5, 14, 11, 10), "firefox")
    assert.deepEqual(Object.keys(days), [DATE])
    const day = days[DATE]
    assert.equal(day.active_ms, 20 * MIN)
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 10: 10 * MIN, 11: 10 * MIN }))
    assert.deepEqual(plain(day.apps_ms), { firefox: 20 * MIN })
  })

  test("an interval across midnight is split into two day objects", () => {
    const days = {}
    tracker.addInterval(days, at(2026, 5, 14, 23, 50), at(2026, 5, 15, 0, 10), "firefox")
    assert.deepEqual(Object.keys(days).sort(), ["2026-05-14", "2026-05-15"])
    const first = days["2026-05-14"]
    const second = days["2026-05-15"]
    assert.equal(first.date, "2026-05-14")
    assert.equal(second.date, "2026-05-15")
    assert.equal(first.active_ms, 10 * MIN)
    assert.equal(second.active_ms, 10 * MIN)
    assert.deepEqual(plain(first.hours_ms), hoursWith({ 23: 10 * MIN }))
    assert.deepEqual(plain(second.hours_ms), hoursWith({ 0: 10 * MIN }))
    assert.deepEqual(plain(first.apps_ms), { firefox: 10 * MIN })
    assert.deepEqual(plain(second.apps_ms), { firefox: 10 * MIN })
  })

  test("a span of several hours fills every bucket it touches", () => {
    const days = {}
    tracker.addInterval(days, at(2026, 5, 14, 8, 15), at(2026, 5, 14, 12, 45), "kitty")
    assert.deepEqual(Object.keys(days), [DATE])
    const day = days[DATE]
    assert.equal(day.active_ms, 4 * HOUR + 30 * MIN)
    assert.deepEqual(plain(day.hours_ms), hoursWith({
      8: 45 * MIN, 9: HOUR, 10: HOUR, 11: HOUR, 12: 45 * MIN,
    }))
    assert.deepEqual(plain(day.apps_ms), { kitty: 4 * HOUR + 30 * MIN })
  })

  test("intervals add up in the same day", () => {
    const days = {}
    tracker.addInterval(days, at(2026, 5, 14, 10, 0), at(2026, 5, 14, 10, 10), "a")
    tracker.addInterval(days, at(2026, 5, 14, 10, 10), at(2026, 5, 14, 10, 30), "b")
    tracker.addInterval(days, at(2026, 5, 14, 10, 30), at(2026, 5, 14, 10, 45), "a")
    const day = days[DATE]
    assert.equal(day.active_ms, 45 * MIN)
    assert.deepEqual(plain(day.apps_ms), { a: 25 * MIN, b: 20 * MIN })
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 10: 45 * MIN }))
  })

  test("zero-length and reversed intervals add nothing and create no day", () => {
    const empty = {}
    const t = at(2026, 5, 14, 10, 30)
    tracker.addInterval(empty, t, t, "a")
    tracker.addInterval(empty, t + MIN, t, "a")
    tracker.addInterval(empty, at(2026, 5, 15, 0, 10), at(2026, 5, 14, 23, 50), "a")
    assert.deepEqual(Object.keys(empty), [])

    const days = {}
    tracker.addInterval(days, at(2026, 5, 14, 10, 0), at(2026, 5, 14, 10, 10), "a")
    const before = plain(days)
    tracker.addInterval(days, t, t, "a")
    tracker.addInterval(days, t + MIN, t, "b")
    tracker.addInterval(days, at(2026, 5, 15, 0, 10), at(2026, 5, 14, 23, 50), "c")
    assert.deepEqual(plain(days), before)
  })

  test("an empty app id counts as active time but gets no apps_ms key", () => {
    const days = {}
    tracker.addInterval(days, at(2026, 5, 14, 10, 10), at(2026, 5, 14, 10, 40), "")
    const day = days[DATE]
    assert.equal(day.active_ms, 30 * MIN)
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 10: 30 * MIN }))
    assert.deepEqual(Object.keys(day.apps_ms), [])
  })
})

describe("daylight saving time", () => {
  test("clocks going back on 2026-10-25: the repeated hour is one long hour 2", () => {
    const start = at(2026, 10, 25, 1, 30)
    const end = at(2026, 10, 25, 3, 30)
    assert.equal(end - start, 3 * HOUR, "02:00-03:00 happens twice, so three real hours pass")
    const days = {}
    tracker.addInterval(days, start, end, "firefox")
    assert.deepEqual(Object.keys(days), ["2026-10-25"])
    const day = days["2026-10-25"]
    assert.equal(day.active_ms, end - start)
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 1: 30 * MIN, 2: 2 * HOUR, 3: 30 * MIN }))
    assert.deepEqual(plain(day.apps_ms), { firefox: 3 * HOUR })
  })

  test("an interval inside the repeated hour stays in hour 2 across the second 02:00", () => {
    // 00:30 UTC is 02:30 CEST, 01:30 UTC is 02:30 CET: both read 02:30
    const start = Date.UTC(2026, 9, 25, 0, 30)
    const end = Date.UTC(2026, 9, 25, 1, 30)
    const days = {}
    tracker.addInterval(days, start, end, "firefox")
    assert.deepEqual(Object.keys(days), ["2026-10-25"])
    const day = days["2026-10-25"]
    assert.equal(day.active_ms, HOUR)
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 2: HOUR }))
  })

  test("a whole 25-hour day keeps its true length", () => {
    const days = {}
    tracker.addInterval(days, at(2026, 10, 25, 0, 0), at(2026, 10, 26, 0, 0), "firefox")
    assert.deepEqual(Object.keys(days), ["2026-10-25"])
    const day = days["2026-10-25"]
    assert.equal(day.active_ms, 25 * HOUR)
    assert.equal(sum(day.hours_ms), 25 * HOUR)
    const expected = {}
    for (let hour = 0; hour < 24; hour++) expected[hour] = hour === 2 ? 2 * HOUR : HOUR
    assert.deepEqual(plain(day.hours_ms), hoursWith(expected))
  })

  test("clocks going forward on 2026-03-29: 01:30 to 03:30 is one real hour, hour 2 stays empty", () => {
    const start = at(2026, 3, 29, 1, 30)
    const end = at(2026, 3, 29, 3, 30)
    assert.equal(end - start, HOUR, "02:00-03:00 does not exist")
    const days = {}
    tracker.addInterval(days, start, end, "firefox")
    assert.deepEqual(Object.keys(days), ["2026-03-29"])
    const day = days["2026-03-29"]
    assert.equal(day.active_ms, HOUR)
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 1: 30 * MIN, 3: 30 * MIN }))
    assert.equal(day.hours_ms[2], 0)
  })

  test("a whole 23-hour day keeps its true length", () => {
    const days = {}
    tracker.addInterval(days, at(2026, 3, 29, 0, 0), at(2026, 3, 30, 0, 0), "firefox")
    const day = days["2026-03-29"]
    assert.equal(day.active_ms, 23 * HOUR)
    assert.equal(sum(day.hours_ms), 23 * HOUR)
    const expected = {}
    for (let hour = 0; hour < 24; hour++) expected[hour] = hour === 2 ? 0 : HOUR
    assert.deepEqual(plain(day.hours_ms), hoursWith(expected))
  })

  test("nextHourStart steps over the missing hour and ends the repeated one once", () => {
    assert.equal(tracker.nextHourStart(at(2026, 3, 29, 1, 30)), at(2026, 3, 29, 3, 0))
    assert.equal(tracker.nextHourStart(at(2026, 3, 29, 3, 0)), at(2026, 3, 29, 4, 0))
    // the first 02:30 (CEST): the whole repeated hour counts as one
    const firstTwoThirty = Date.UTC(2026, 9, 25, 0, 30)
    assert.equal(tracker.nextHourStart(firstTwoThirty), at(2026, 10, 25, 3, 0))
    assert.equal(tracker.nextHourStart(at(2026, 10, 25, 1, 30)), firstTwoThirty - 30 * MIN)
  })
})

describe("observe", () => {
  test("counts app time, skips away time and counts switches", () => {
    const state = run([
      [0, "a"],
      [10 * SEC, "a"],
      [20 * SEC, "b"],
      [30 * SEC, "b", true],
      [40 * SEC, "b", true],
      [50 * SEC, "b"],
      [60 * SEC, "c"],
      [70 * SEC, "c"],
    ])
    const day = pending(state)
    assert.equal(day.active_ms, 50 * SEC)
    assert.deepEqual(plain(day.apps_ms), { a: 20 * SEC, b: 20 * SEC, c: 10 * SEC })
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 10: 50 * SEC }))
    assert.equal(day.switches, 2)
  })

  test("a stretch is credited to the app it was opened with", () => {
    const state = run([[0, "a"], [10 * SEC, "b"], [25 * SEC, "b"]])
    assert.deepEqual(plain(pending(state).apps_ms), { a: 10 * SEC, b: 15 * SEC })
  })

  test("time while away is not counted", () => {
    const state = run([
      [0, "a"],
      [10 * SEC, "a", true],
      [40 * SEC, "a", true],
      [50 * SEC, "a"],
      [60 * SEC, "a"],
    ])
    const day = pending(state)
    // [0,10) was opened as present, [10,50) as away, [50,60) as present
    assert.equal(day.active_ms, 20 * SEC)
    assert.deepEqual(plain(day.apps_ms), { a: 20 * SEC })
  })

  test("an empty app counts as active time without a name", () => {
    const state = run([[0, ""], [10 * SEC, ""], [20 * SEC, "a"], [30 * SEC, "a"]])
    const day = pending(state)
    assert.equal(day.active_ms, 30 * SEC)
    assert.deepEqual(plain(day.apps_ms), { a: 10 * SEC })
    assert.equal(day.switches, 0)
  })

  test("a gap longer than maxGapMs is dropped whole, one of exactly maxGapMs is kept", () => {
    const gap = 45 * SEC
    const state = run([
      [0, "a"],
      [45 * SEC, "a"],
      [90 * SEC + 1, "a"],
      [100 * SEC, "a"],
    ], gap)
    const day = pending(state)
    assert.equal(day.active_ms, 45 * SEC + (100 * SEC - (90 * SEC + 1)))
    assert.deepEqual(plain(day.apps_ms), { a: day.active_ms })
  })

  test("a dropped gap that spans hours and days creates no day", () => {
    const gap = 45 * SEC
    const state = tracker.create()
    tracker.observe(state, at(2026, 5, 14, 23, 0), "a", false, gap)
    tracker.observe(state, at(2026, 5, 15, 7, 0), "a", false, gap)
    assert.deepEqual(Object.keys(state.days), [])
    tracker.observe(state, at(2026, 5, 15, 7, 0, 10), "a", false, gap)
    assert.deepEqual(Object.keys(state.days), ["2026-05-15"])
    const day = pending(state, "2026-05-15")
    assert.equal(day.active_ms, 10 * SEC)
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 7: 10 * SEC }))
  })

  test("a clock that goes backwards adds nothing", () => {
    const state = run([[10 * SEC, "a"], [0, "a"]])
    assert.deepEqual(Object.keys(state.days), [])
    // time is measured from the earlier reading from then on
    tracker.observe(state, BASE + 5 * SEC, "a", false, GAP)
    assert.equal(pending(state).active_ms, 5 * SEC)
  })

  test("two readings at the same instant add nothing", () => {
    const state = run([[0, "a"], [0, "a"]])
    assert.deepEqual(Object.keys(state.days), [])
  })

  test("the first app seen is not a switch", () => {
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "a"]])), 0)
    assert.equal(switchCount(run([[0, ""], [10 * SEC, "a"]])), 0)
  })

  test("switches counts a change between two different named apps", () => {
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "b"], [20 * SEC, "b"]])), 1)
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "b"], [20 * SEC, "a"], [30 * SEC, "a"]])), 2)
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "a"], [20 * SEC, "a"]])), 0)
  })

  test("a switch counts once the new app has held focus for the dwell time", () => {
    const dwell = tracker.SWITCH_DWELL_MS
    assert.equal(dwell, 1000)
    // not yet: the stretch in b is still open
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "b"]])), 0)
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "b"], [10 * SEC + dwell - 1, "b"]])), 0)
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "b"], [10 * SEC + dwell, "b"]])), 1)
    // reached over two stretches in a row, as when a tick falls in between
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "b"], [10 * SEC + 400, "b"], [10 * SEC + 1100, "b"]])), 1)
  })

  test("focus that only passes over an app is not a switch", () => {
    const state = run([[0, "a"], [10 * SEC, "b"], [10 * SEC + 300, "a"], [20 * SEC, "a"]])
    assert.equal(switchCount(state), 0)
    // the time it had focus is still its time
    assert.deepEqual(plain(pending(state).apps_ms), { a: 10 * SEC + 9700, b: 300 })
    // a -> b (in passing) -> c is one switch, to c
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "b"], [10 * SEC + 500, "c"], [20 * SEC, "c"]])), 1)
  })

  test("two short visits with another app in between do not add up to a switch", () => {
    const state = run([[0, "a"], [10 * SEC, "b"], [10 * SEC + 600, "c"], [10 * SEC + 700, "b"],
      [10 * SEC + 1300, "a"], [20 * SEC, "a"]])
    assert.equal(switchCount(state), 0)
  })

  test("the screensaver starting does not count switches", () => {
    // What a live session shows when the screensaver starts: the user has
    // been away, focus hops over two windows within milliseconds, and the
    // screensaver (ignored, so away) takes over.
    const state = run([
      [0, "claude"],
      [30 * SEC, "claude", true],
      [103 * SEC, "", true],
      [103 * SEC + 1, "slack", true],
      [103 * SEC + 2, "slack"],
      [103 * SEC + 10, ""],
      [103 * SEC + 11, "org.omarchy.screensaver", true],
      [103 * SEC + 12, "", true],
      [103 * SEC + 13, "claude"],
      [103 * SEC + 20, ""],
      [103 * SEC + 21, "org.omarchy.screensaver", true],
      [133 * SEC, "org.omarchy.screensaver", true],
    ], 5 * MIN)
    const day = pending(state)
    assert.equal(day.switches, 0)
    assert.equal(day.apps_ms.claude, 30 * SEC + 7)
    assert.equal(day.apps_ms.slack, 8)
  })

  test("going away and returning to the same app is not a switch", () => {
    const state = run([[0, "a"], [10 * SEC, "a", true], [20 * SEC, "a"], [30 * SEC, "a"]])
    assert.equal(switchCount(state), 0)
    assert.equal(pending(state).active_ms, 20 * SEC)
  })

  test("an empty app in between is not a switch when the same app comes back", () => {
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, ""], [20 * SEC, "a"]])), 0)
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, ""], [20 * SEC, ""], [30 * SEC, "a"]])), 0)
  })

  test("an empty app in between does not hide a real switch", () => {
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, ""], [20 * SEC, "b"], [30 * SEC, "b"]])), 1)
  })

  test("returning from away into a different app is a switch", () => {
    assert.equal(switchCount(run([[0, "a"], [10 * SEC, "a", true], [20 * SEC, "b"], [30 * SEC, "b"]])), 1)
  })

  test("a switch is recorded on the day it happens", () => {
    const state = tracker.create()
    tracker.observe(state, at(2026, 5, 14, 23, 59, 50), "a", false, GAP)
    tracker.observe(state, at(2026, 5, 15, 0, 0, 5), "b", false, GAP)
    tracker.observe(state, at(2026, 5, 15, 0, 0, 15), "b", false, GAP)
    const first = pending(state, "2026-05-14")
    const second = pending(state, "2026-05-15")
    assert.equal(first.active_ms, 10 * SEC)
    assert.deepEqual(plain(first.hours_ms), hoursWith({ 23: 10 * SEC }))
    assert.equal(first.switches, 0)
    assert.equal(second.active_ms, 15 * SEC)
    assert.deepEqual(plain(second.hours_ms), hoursWith({ 0: 15 * SEC }))
    assert.deepEqual(plain(second.apps_ms), { a: 5 * SEC, b: 10 * SEC })
    assert.equal(second.switches, 1)
  })

  test("app ids are cleaned before they are counted", () => {
    const state = run([
      [0, "  firefox "],
      [10 * SEC, "fire\x00fox\x07"],
      [20 * SEC, "\tfirefox\n"],
      [30 * SEC, "kitty"],
      [40 * SEC, "kitty"],
    ])
    const day = pending(state)
    assert.deepEqual(plain(day.apps_ms), { firefox: 30 * SEC, kitty: 10 * SEC })
    assert.equal(day.switches, 1, "only the final change to kitty is a switch")
  })

  test("an app called __proto__ counts as active time under no name", () => {
    const state = run([[0, "__proto__"], [10 * SEC, "__proto__"], [20 * SEC, " __proto__ "]])
    const day = pending(state)
    assert.equal(day.active_ms, 20 * SEC)
    assert.deepEqual(Object.keys(day.apps_ms), [])
    assert.equal(day.switches, 0)
    assert.equal(({}).polluted, undefined)
  })

  test("cleanApp trims, strips control characters and truncates", () => {
    assert.equal(tracker.cleanApp("  firefox  "), "firefox")
    assert.equal(tracker.cleanApp("\t firefox \n"), "firefox")
    assert.equal(tracker.cleanApp("fi\x00re\x07fo\x1bx\x7f"), "firefox")
    assert.equal(tracker.cleanApp("a\nb"), "ab")
    assert.equal(tracker.cleanApp("org.mozilla.firefox"), "org.mozilla.firefox")

    const long = "0123456789".repeat(20)
    assert.equal(tracker.cleanApp(long).length, tracker.MAX_APP_LENGTH)
    assert.equal(tracker.cleanApp(long), long.slice(0, tracker.MAX_APP_LENGTH))
    const exact = long.slice(0, tracker.MAX_APP_LENGTH)
    assert.equal(tracker.cleanApp(exact), exact)
    assert.equal(tracker.cleanApp(exact + "x"), exact)
  })

  test("cleanApp turns __proto__, null and undefined into an empty id", () => {
    assert.equal(tracker.cleanApp("__proto__"), "")
    assert.equal(tracker.cleanApp("  __proto__\t"), "")
    assert.equal(tracker.cleanApp("__pro\x00to__"), "")
    assert.equal(tracker.cleanApp(null), "")
    assert.equal(tracker.cleanApp(undefined), "")
    assert.equal(tracker.cleanApp(""), "")
    assert.equal(tracker.cleanApp("   "), "")
    assert.equal(tracker.cleanApp("__proto__x"), "__proto__x")
  })
})

describe("takePending, restorePending, pendingDay", () => {
  test("takePending hands over the counted days and leaves none pending", () => {
    const state = run([[0, "a"], [10 * SEC, "b"], [20 * SEC, "b"]])
    const taken = tracker.takePending(state)
    assert.deepEqual(Object.keys(taken), [DATE])
    assert.equal(taken[DATE].date, DATE)
    assert.equal(taken[DATE].active_ms, 20 * SEC)
    assert.deepEqual(plain(taken[DATE].apps_ms), { a: 10 * SEC, b: 10 * SEC })
    assert.equal(taken[DATE].switches, 1)
    assert.deepEqual(Object.keys(state.days), [])
    assert.equal(pending(state), null)
    assert.deepEqual(Object.keys(tracker.takePending(state)), [])
  })

  test("takePending returns every day that has something in it", () => {
    const state = tracker.create()
    tracker.addInterval(state.days, at(2026, 5, 14, 23, 50), at(2026, 5, 15, 0, 10), "a")
    const taken = tracker.takePending(state)
    assert.deepEqual(Object.keys(taken).sort(), ["2026-05-14", "2026-05-15"])
  })

  test("the open stretch keeps running after a take", () => {
    const state = run([[0, "a"], [10 * SEC, "a"]])
    tracker.takePending(state)
    // counted from the previous observe, not from the take
    tracker.observe(state, BASE + 25 * SEC, "a", false, GAP)
    const day = pending(state)
    assert.equal(day.active_ms, 15 * SEC)
    assert.deepEqual(plain(day.apps_ms), { a: 15 * SEC })
    assert.equal(day.switches, 0, "the last named app is still a")
  })

  test("an away stretch stays away across a take", () => {
    const state = run([[0, "a"], [10 * SEC, "a", true]])
    tracker.takePending(state)
    tracker.observe(state, BASE + 40 * SEC, "a", false, GAP)
    assert.equal(pending(state), null)
  })

  test("a day with nothing in it is not returned", () => {
    const state = tracker.create()
    state.days["2026-05-13"] = tracker.emptyDay("2026-05-13")
    tracker.addInterval(state.days, BASE, BASE + MIN, "a")
    assert.deepEqual(Object.keys(tracker.takePending(state)), [DATE])
    assert.deepEqual(Object.keys(state.days), [])

    state.days["2026-05-13"] = tracker.emptyDay("2026-05-13")
    assert.deepEqual(Object.keys(tracker.takePending(state)), [])
  })

  test("a day with only a switch in it is returned", () => {
    const state = run([[0, "a"], [10 * SEC, "b"], [20 * SEC, "b"]])
    state.days[DATE].active_ms = 0
    state.days[DATE].hours_ms = hoursWith({})
    state.days[DATE].apps_ms = {}
    const taken = tracker.takePending(state)
    assert.deepEqual(Object.keys(taken), [DATE])
    assert.equal(taken[DATE].switches, 1)
  })

  test("restorePending puts a day back", () => {
    const state = tracker.create()
    const day = makeDay(DATE, { active: 10 * MIN,hours: { 10: 10 * MIN }, apps: { a: 6 * MIN }, switches: 2 })
    tracker.restorePending(state, day)
    assert.deepEqual(plain(pending(state)), plain(day))
    assert.deepEqual(Object.keys(tracker.takePending(state)), [DATE])
  })

  test("restorePending merges with what was counted since", () => {
    const state = run([[0, "a"], [10 * SEC, "a"]])
    const taken = tracker.takePending(state)[DATE]
    tracker.observe(state, BASE + 25 * SEC, "a", false, GAP)
    tracker.observe(state, BASE + 30 * SEC, "b", false, GAP)
    tracker.observe(state, BASE + 40 * SEC, "b", false, GAP)
    tracker.restorePending(state, taken)
    const day = pending(state)
    assert.equal(day.active_ms, 40 * SEC)
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 10: 40 * SEC }))
    assert.deepEqual(plain(day.apps_ms), { a: 30 * SEC, b: 10 * SEC })
    assert.equal(day.switches, 1)
  })

  test("restorePending keeps other pending days apart", () => {
    const state = tracker.create()
    tracker.addInterval(state.days, at(2026, 5, 15, 9, 0), at(2026, 5, 15, 9, 5), "a")
    tracker.restorePending(state, makeDay(DATE, { active: MIN, hours: { 10: MIN }, apps: { a: MIN } }))
    assert.deepEqual(Object.keys(state.days).sort(), [DATE, "2026-05-15"])
    assert.equal(pending(state, "2026-05-15").active_ms, 5 * MIN)
    assert.equal(pending(state).active_ms, MIN)
  })

  test("pendingDay returns the pending day or null", () => {
    const state = run([[0, "a"], [10 * SEC, "a"]])
    assert.equal(pending(state), state.days[DATE])
    assert.equal(pending(state, "2026-05-15"), null)
    assert.equal(pending(tracker.create()), null)
  })

  test("pendingDay never finds members of Object.prototype", () => {
    const empty = tracker.create()
    const busy = run([[0, "a"], [10 * SEC, "a"]])
    for (const state of [empty, busy]) {
      for (const name of ["constructor", "toString", "hasOwnProperty", "valueOf", "__proto__"]) {
        assert.equal(tracker.pendingDay(state, name), null, name)
      }
    }
  })

  test("app names that match Object.prototype members are ordinary apps", () => {
    const names = ["constructor", "toString", "hasOwnProperty", "valueOf"]
    const days = {}
    names.forEach((name, i) => tracker.addInterval(days, BASE + i * MIN, BASE + (i + 1) * MIN, name))
    tracker.addInterval(days, BASE + 4 * MIN, BASE + 5 * MIN, "constructor")
    const expected = { constructor: 2 * MIN, toString: MIN, hasOwnProperty: MIN, valueOf: MIN }
    const day = days[DATE]
    assert.deepEqual(plain(day.apps_ms), expected)
    assert.equal(day.active_ms, 5 * MIN)

    assert.deepEqual(plain(tracker.mergeDay(null, day).apps_ms), expected)
    assert.deepEqual(plain(tracker.mergeDay(day, day).apps_ms), {
      constructor: 4 * MIN, toString: 2 * MIN, hasOwnProperty: 2 * MIN, valueOf: 2 * MIN,
    })
    assert.deepEqual(plain(tracker.parseDay(tracker.serialize(day), DATE).apps_ms), expected)
  })
})

describe("parseDay and serialize", () => {
  test("serialize then parseDay round-trips", () => {
    const days = {}
    tracker.addInterval(days, at(2026, 5, 14, 10, 50), at(2026, 5, 14, 11, 20), "firefox")
    tracker.addInterval(days, at(2026, 5, 14, 11, 20), at(2026, 5, 14, 11, 45), "kitty")
    tracker.addInterval(days, at(2026, 5, 14, 11, 45), at(2026, 5, 14, 12, 5), "")
    days[DATE].switches = 3
    const text = tracker.serialize(days[DATE])
    assert.ok(text.endsWith("\n"))
    const parsed = tracker.parseDay(text, DATE)
    assert.deepEqual(plain(parsed), plain(days[DATE]))
    assert.equal(tracker.serialize(parsed), text)
  })

  test("an empty day round-trips", () => {
    const day = tracker.emptyDay(DATE)
    assert.deepEqual(plain(tracker.parseDay(tracker.serialize(day), DATE)), plain(day))
  })

  test("a wrong version gives null", () => {
    assert.equal(tracker.parseDay(dayText({ version: tracker.VERSION + 1 }), DATE), null)
    assert.equal(tracker.parseDay(dayText({ version: 0 }), DATE), null)
    assert.equal(tracker.parseDay(dayText({ version: String(tracker.VERSION) }), DATE), null)
    assert.equal(tracker.parseDay(dayText({ version: undefined }), DATE), null)
  })

  test("a wrong date gives null", () => {
    assert.equal(tracker.parseDay(dayText({ date: "2026-05-15" }), DATE), null)
    assert.equal(tracker.parseDay(dayText({ date: undefined }), DATE), null)
    assert.equal(tracker.parseDay(dayText({ date: 20260514 }), DATE), null)
    assert.equal(tracker.parseDay(dayText(), "2026-05-15"), null)
  })

  test("text that is not a day object gives null", () => {
    const inputs = [
      "{not json", "", "{", "null", "[]", "[1, 2]", "[" + dayText() + "]",
      "\"just a string\"", "5", "true", undefined, null,
    ]
    for (const input of inputs) assert.equal(tracker.parseDay(input, DATE), null, String(input))
  })

  test("a valid minimal day parses with the date it was asked for", () => {
    const day = tracker.parseDay(dayText({ active_ms: 5000 }), DATE)
    assert.equal(day.date, DATE)
    assert.equal(day.version, tracker.VERSION)
    assert.equal(day.active_ms, 5000)
  })

  test("negative, NaN, string and missing counters read as 0", () => {
    // JSON.stringify writes NaN as null
    const bad = [-5, NaN, null, "7000", "NaN", true, {}, [], -0.5]
    for (const value of bad) {
      const day = tracker.parseDay(dayText({ active_ms: value, switches: value }), DATE)
      assert.equal(day.active_ms, 0, JSON.stringify(value))
      assert.equal(day.switches, 0, JSON.stringify(value))
    }
    const missing = tracker.parseDay(dayText({ active_ms: undefined, switches: undefined }), DATE)
    assert.equal(missing.active_ms, 0)
    assert.equal(missing.switches, 0)
    // 1e999 is valid JSON and reads as Infinity
    const infinite = tracker.parseDay("{\"version\":" + tracker.VERSION + ",\"date\":\"" + DATE + "\",\"active_ms\":1e999}", DATE)
    assert.equal(infinite.active_ms, 0)
  })

  test("good counters are kept and switches is whole", () => {
    const day = tracker.parseDay(dayText({ active_ms: 123456, switches: 7 }), DATE)
    assert.equal(day.active_ms, 123456)
    assert.equal(day.switches, 7)
    assert.equal(tracker.parseDay(dayText({ switches: 3.9 }), DATE).switches, 3)
  })

  test("bad hours_ms entries read as 0, good ones are kept", () => {
    const hours = [100, -1, "5", null, 200, NaN, {}, 300]
    const day = tracker.parseDay(dayText({ hours_ms: hours }), DATE)
    assert.deepEqual(plain(day.hours_ms), hoursWith({ 0: 100, 4: 200, 7: 300 }))
  })

  test("a short, missing or malformed hours_ms gives 24 numbers", () => {
    const short = tracker.parseDay(dayText({ hours_ms: [1000, 2000, 3000] }), DATE)
    assert.deepEqual(plain(short.hours_ms), hoursWith({ 0: 1000, 1: 2000, 2: 3000 }))
    const missing = tracker.parseDay(dayText({ hours_ms: undefined }), DATE)
    assert.deepEqual(plain(missing.hours_ms), hoursWith({}))
    const empty = tracker.parseDay(dayText({ hours_ms: [] }), DATE)
    assert.deepEqual(plain(empty.hours_ms), hoursWith({}))
    for (const value of [null, "abc", 5, { 0: 5000 }]) {
      const day = tracker.parseDay(dayText({ hours_ms: value }), DATE)
      assert.deepEqual(plain(day.hours_ms), hoursWith({}), JSON.stringify(value))
    }
    const long = tracker.parseDay(dayText({ hours_ms: new Array(30).fill(1000) }), DATE)
    assert.equal(long.hours_ms.length, 24)
    assert.equal(sum(long.hours_ms), 24 * 1000)
  })

  test("non-number and non-positive app values are dropped", () => {
    const apps = {
      good: 5000, zero: 0, negative: -1, text: "1000", nothing: null,
      object: { x: 1 }, yes: true, list: [5000], half: 0.5,
    }
    const day = tracker.parseDay(dayText({ apps_ms: apps }), DATE)
    assert.deepEqual(plain(day.apps_ms), { good: 5000, half: 0.5 })
  })

  test("an apps_ms that is not an object is ignored", () => {
    for (const value of [null, "abc", 5, [5000], undefined]) {
      const day = tracker.parseDay(dayText({ apps_ms: value }), DATE)
      assert.deepEqual(Object.keys(day.apps_ms), [], JSON.stringify(value))
    }
  })

  test("app ids from a file are cleaned and merged", () => {
    const apps = { x: 1000, " x ": 2000, "y\u0000": 500, "   ": 9000 }
    const day = tracker.parseDay(dayText({ apps_ms: apps }), DATE)
    assert.deepEqual(plain(day.apps_ms), { x: 3000, y: 500 })
  })

  test("unknown top-level keys are dropped", () => {
    const text = dayText({ title: "Secret window title", extra: { deep: 1 }, active_ms: 4000 })
    const day = tracker.parseDay(text, DATE)
    assert.deepEqual(Object.keys(day).sort(), ["active_ms", "apps_ms", "date", "hours_ms", "switches", "version"])
    assert.ok(!tracker.serialize(day).includes("Secret"))
    assert.equal(day.active_ms, 4000)
  })

  test("an apps_ms key of __proto__ does not pollute anything", () => {
    const head = "{\"version\":" + tracker.VERSION + ",\"date\":\"" + DATE + "\",\"active_ms\":5000,\"hours_ms\":[],"
    const nested = tracker.parseDay(
      head + "\"apps_ms\":{\"__proto__\":{\"polluted\":true},\"real\":3000},\"switches\":0}", DATE)
    const number = tracker.parseDay(
      head + "\"apps_ms\":{\"__proto__\":9000,\"real\":3000},\"switches\":0}", DATE)
    for (const day of [nested, number]) {
      assert.deepEqual(Object.keys(day.apps_ms), ["real"])
      assert.equal(day.apps_ms.real, 3000)
      assert.equal(day.apps_ms.polluted, undefined)
      assert.ok(!tracker.serialize(day).includes("__proto__"))
    }
    assert.equal(({}).polluted, undefined)
    assert.equal(vm.runInContext("({}).polluted", context), undefined)
    assert.equal(Object.prototype.polluted, undefined)
  })

  test("a top-level __proto__ key is dropped", () => {
    const text = "{\"__proto__\":{\"polluted\":true},\"version\":" + tracker.VERSION + ",\"date\":\"" + DATE
      + "\",\"active_ms\":5000,\"hours_ms\":[],\"apps_ms\":{},\"switches\":0}"
    const day = tracker.parseDay(text, DATE)
    assert.equal(day.active_ms, 5000)
    assert.ok(!Object.keys(day).includes("__proto__"))
    assert.equal(day.polluted, undefined)
    assert.equal(({}).polluted, undefined)
  })
})

describe("mergeDay", () => {
  const stored = () => makeDay(DATE, {
    active: 100 * SEC, hours: { 9: 40 * SEC, 10: 60 * SEC }, apps: { a: 50 * SEC, b: 50 * SEC }, switches: 2,
  })
  const delta = () => makeDay(DATE, {
    active: 70 * SEC, hours: { 10: 30 * SEC, 11: 40 * SEC }, apps: { b: 10 * SEC, c: 30 * SEC }, switches: 1,
  })

  test("sums every counter", () => {
    const merged = tracker.mergeDay(stored(), delta())
    assert.equal(merged.version, tracker.VERSION)
    assert.equal(merged.date, DATE)
    assert.equal(merged.active_ms, 170 * SEC)
    assert.deepEqual(plain(merged.hours_ms), hoursWith({ 9: 40 * SEC, 10: 90 * SEC, 11: 40 * SEC }))
    assert.deepEqual(plain(merged.apps_ms), { a: 50 * SEC, b: 60 * SEC, c: 30 * SEC })
    assert.equal(merged.switches, 3)
  })

  test("a null stored day returns a copy of the delta", () => {
    const original = delta()
    for (const none of [null, undefined]) {
      const merged = tracker.mergeDay(none, original)
      assert.deepEqual(plain(merged), plain(original))
      assert.notEqual(merged, original)
      assert.notEqual(merged.hours_ms, original.hours_ms)
      assert.notEqual(merged.apps_ms, original.apps_ms)
      merged.hours_ms[10] = 1
      merged.apps_ms.b = 1
      merged.active_ms = 1
      assert.deepEqual(plain(original), plain(delta()))
    }
  })

  test("the inputs are not mutated", () => {
    const a = stored()
    const b = delta()
    const merged = tracker.mergeDay(a, b)
    assert.deepEqual(plain(a), plain(stored()))
    assert.deepEqual(plain(b), plain(delta()))
    merged.hours_ms[9] = 0
    merged.apps_ms.a = 0
    assert.deepEqual(plain(a), plain(stored()))
  })

  test("the date comes from the delta", () => {
    const other = makeDay("2026-05-15", { active: SEC, hours: { 0: SEC }, apps: { a: SEC } })
    assert.equal(tracker.mergeDay(null, other).date, "2026-05-15")
  })

  test("the app cap holds in addInterval: extra apps still count as active time", () => {
    const days = {}
    const cap = tracker.MAX_APPS_PER_DAY
    for (let i = 0; i < cap; i++) tracker.addInterval(days, BASE + i * SEC, BASE + (i + 1) * SEC, "app" + i)
    const full = days[DATE]
    assert.equal(Object.keys(full.apps_ms).length, cap)
    assert.equal(full.active_ms, cap * SEC)

    tracker.addInterval(days, BASE + cap * SEC, BASE + (cap + 5) * SEC, "overflow")
    assert.equal(Object.keys(full.apps_ms).length, cap)
    assert.ok(!Object.prototype.hasOwnProperty.call(full.apps_ms, "overflow"))
    assert.equal(full.active_ms, (cap + 5) * SEC)
    assert.equal(sum(full.hours_ms), (cap + 5) * SEC)

    // an app that already has a key keeps accumulating
    tracker.addInterval(days, BASE + (cap + 5) * SEC, BASE + (cap + 8) * SEC, "app0")
    assert.equal(full.apps_ms.app0, SEC + 3 * SEC)
    assert.equal(full.active_ms, (cap + 8) * SEC)
  })

  test("the app cap holds in mergeDay", () => {
    const cap = tracker.MAX_APPS_PER_DAY
    const apps = {}
    for (let i = 0; i < cap; i++) apps["app" + i] = SEC
    const full = makeDay(DATE, { active: cap * SEC, hours: { 10: cap * SEC }, apps })
    const more = makeDay(DATE, { active: 3 * SEC, hours: { 10: 3 * SEC }, apps: { app0: SEC, extra: 2 * SEC } })
    const merged = tracker.mergeDay(full, more)
    assert.equal(Object.keys(merged.apps_ms).length, cap)
    assert.equal(merged.apps_ms.app0, 2 * SEC)
    assert.ok(!Object.prototype.hasOwnProperty.call(merged.apps_ms, "extra"))
    assert.equal(merged.active_ms, cap * SEC + 3 * SEC)
  })
})

describe("formatDuration", () => {
  test("formats minutes and hours", () => {
    assert.equal(tracker.formatDuration(0), "0m")
    assert.equal(tracker.formatDuration(59999), "0m")
    assert.equal(tracker.formatDuration(60000), "1m")
    assert.equal(tracker.formatDuration(119999), "1m")
    assert.equal(tracker.formatDuration(59 * MIN), "59m")
    assert.equal(tracker.formatDuration(HOUR - 1), "59m")
    assert.equal(tracker.formatDuration(HOUR), "1h 00m")
    assert.equal(tracker.formatDuration(3 * HOUR + 7 * MIN), "3h 07m")
    assert.equal(tracker.formatDuration(3 * HOUR + 7 * MIN + 59 * SEC), "3h 07m")
    assert.equal(tracker.formatDuration(10 * HOUR + 42 * MIN), "10h 42m")
    assert.equal(tracker.formatDuration(25 * HOUR), "25h 00m")
  })

  test("a negative duration is 0m", () => {
    assert.equal(tracker.formatDuration(-1), "0m")
    assert.equal(tracker.formatDuration(-HOUR), "0m")
  })

  test("anything that is not a time is 0m", () => {
    assert.equal(tracker.formatDuration(NaN), "0m")
    assert.equal(tracker.formatDuration(undefined), "0m")
    assert.equal(tracker.formatDuration(null), "0m")
    assert.equal(tracker.formatDuration(Infinity), "0m")
  })
})

describe("parseList", () => {
  test("splits on commas and trims", () => {
    assert.deepEqual(plain(tracker.parseList("a, b ,c")), ["a", "b", "c"])
    assert.deepEqual(plain(tracker.parseList("single")), ["single"])
  })

  test("removes duplicates and drops empty items", () => {
    assert.deepEqual(plain(tracker.parseList("a,b,a, b ,a")), ["a", "b"])
    assert.deepEqual(plain(tracker.parseList("a,,b, ,,")), ["a", "b"])
    assert.deepEqual(plain(tracker.parseList(",,,")), [])
  })

  test("accepts an array", () => {
    assert.deepEqual(plain(tracker.parseList([" a ", "b", "a", "", null, undefined, "c"])), ["a", "b", "c"])
  })

  test("anything that is not a list gives an empty list", () => {
    assert.deepEqual(plain(tracker.parseList(null)), [])
    assert.deepEqual(plain(tracker.parseList(undefined)), [])
    assert.deepEqual(plain(tracker.parseList("")), [])
    assert.deepEqual(plain(tracker.parseList([])), [])
    assert.deepEqual(plain(tracker.parseList("   ")), [])
  })

  test("items are cleaned like app ids", () => {
    assert.deepEqual(plain(tracker.parseList("a,__proto__,b")), ["a", "b"])
  })
})

describe("entryFor", () => {
  const config = () => ({
    layout: {
      left: [{ id: "clock" }],
      center: [{ id: "workspaces" }, { id: "omawrapped", ignoreApps: "x" }],
      right: [{ id: "battery" }],
    },
  })

  test("finds the entry in left, center or right", () => {
    const left = { layout: { left: [{ id: "clock" }, { id: "me", where: "left" }], center: [], right: [] } }
    const center = { layout: { left: [], center: [{ id: "me", where: "center" }], right: [] } }
    const right = { layout: { left: [], center: [], right: [{ id: "x" }, { id: "me", where: "right" }] } }
    assert.deepEqual(plain(tracker.entryFor(left, "me")), left.layout.left[1])
    assert.deepEqual(plain(tracker.entryFor(center, "me")), center.layout.center[0])
    assert.deepEqual(plain(tracker.entryFor(right, "me")), right.layout.right[1])
    const full = config()
    assert.deepEqual(plain(tracker.entryFor(full, "omawrapped")), full.layout.center[1])
    assert.equal(tracker.entryFor(full, "omawrapped").ignoreApps, "x")
  })

  test("the first match wins, left before center before right", () => {
    const both = { layout: { left: [], center: [{ id: "me", n: 1 }], right: [{ id: "me", n: 2 }] } }
    assert.equal(tracker.entryFor(both, "me").n, 1)
  })

  test("returns {} when the id is absent", () => {
    const result = tracker.entryFor(config(), "missing")
    assert.deepEqual(plain(result), {})
    assert.deepEqual(Object.keys(result), [])
  })

  test("returns {} when the config is not an object", () => {
    for (const value of [null, undefined, "layout", 5, true, false, 0, ""]) {
      assert.deepEqual(plain(tracker.entryFor(value, "me")), {}, String(value))
    }
  })

  test("returns {} when layout is missing or not an object", () => {
    for (const layout of [undefined, null, "left", 5, true]) {
      assert.deepEqual(plain(tracker.entryFor({ layout }, "me")), {}, String(layout))
    }
    assert.deepEqual(plain(tracker.entryFor({}, "me")), {})
    assert.deepEqual(plain(tracker.entryFor({ layout: {} }, "me")), {})
  })

  test("skips sections that are not arrays", () => {
    const odd = {
      layout: {
        left: "me",
        center: { id: "me", where: "object" },
        right: [{ id: "me", where: "right" }],
      },
    }
    assert.deepEqual(plain(tracker.entryFor(odd, "me")), odd.layout.right[0])
    const nothing = { layout: { left: "me", center: { id: "me" }, right: 5 } }
    assert.deepEqual(plain(tracker.entryFor(nothing, "me")), {})
    // list-like but not an array
    const arrayLike = { layout: { left: { 0: { id: "me" }, length: 1 }, center: [], right: [] } }
    assert.deepEqual(plain(tracker.entryFor(arrayLike, "me")), {})
  })

  test("a configuration that cannot be read as JSON gives {}", () => {
    const loop = { layout: { left: [], center: [], right: [{ id: "me" }] } }
    loop.self = loop
    assert.deepEqual(plain(tracker.entryFor(loop, "me")), {})
  })

  test("skips items that are not objects", () => {
    const wanted = { id: "me", real: true }
    const list = { layout: { left: [null, undefined, 5, "me", true, 0, wanted], center: [], right: [] } }
    assert.deepEqual(plain(tracker.entryFor(list, "me")), wanted)
    const only = { layout: { left: [null, "me", 5], center: [], right: [] } }
    assert.deepEqual(plain(tracker.entryFor(only, "me")), {})
  })
})

describe("anyLocked", () => {
  test("is true when any list contains LOCK", () => {
    assert.equal(tracker.anyLocked([["LOCK"]]), true)
    assert.equal(tracker.anyLocked([["WINDOWED", "LOCK"]]), true)
    assert.equal(tracker.anyLocked([["WINDOWED"], [], ["OTHER", "LOCK", "MORE"]]), true)
  })

  test("walks array-like objects by index", () => {
    assert.equal(tracker.anyLocked([{ 0: "WINDOWED", 1: "LOCK", length: 2 }]), true)
    assert.equal(tracker.anyLocked([{ 0: "WINDOWED", length: 1 }, { 0: "LOCK", length: 1 }]), true)
    assert.equal(tracker.anyLocked({ 0: ["LOCK"], length: 1 }), true)
    assert.equal(tracker.anyLocked([{ 0: "WINDOWED", 1: "OTHER", length: 2 }]), false)
    assert.equal(tracker.anyLocked([{ length: 0 }]), false)
  })

  test("is false for an empty input and for lists without LOCK", () => {
    assert.equal(tracker.anyLocked([]), false)
    assert.equal(tracker.anyLocked({ length: 0 }), false)
    assert.equal(tracker.anyLocked([[]]), false)
    assert.equal(tracker.anyLocked([["WINDOWED", "OTHER"], []]), false)
    assert.equal(tracker.anyLocked([["lock"]]), false)
  })

  test("ignores undefined and null entries", () => {
    assert.equal(tracker.anyLocked([undefined, null]), false)
    assert.equal(tracker.anyLocked([undefined, ["LOCK"], null]), true)
  })

  test("ignores string entries on purpose", () => {
    assert.equal(tracker.anyLocked(["LOCK"]), false)
    assert.equal(tracker.anyLocked(["LOCK", "WINDOWED"]), false)
    assert.equal(tracker.anyLocked(["LOCK", ["LOCK"]]), true)
  })
})

// A small seeded generator (mulberry32), so the run is the same every time.
function seededRandom(seed) {
  let a = seed >>> 0
  return function () {
    a = (a + 0x6d2b79f5) >>> 0
    let t = Math.imul(a ^ (a >>> 15), a | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

describe("invariants under random input", () => {
  test("hours add up to active time and every counter is a whole non-negative number", () => {
    const gap = 45 * SEC
    const apps = ["", "alpha", "beta", "gamma", "  alpha  ", "__proto__", null, "constructor", "delta\x01"]
    const scenarios = [
      { name: "ordinary days", start: at(2026, 5, 14, 20, 0), through: at(2026, 5, 15, 4), seed: 1 },
      { name: "clocks go back", start: at(2026, 10, 24, 22, 0), through: at(2026, 10, 25, 4), seed: 2 },
      { name: "clocks go forward", start: at(2026, 3, 28, 22, 0), through: at(2026, 3, 29, 4), seed: 3 },
    ]
    for (const scenario of scenarios) {
      const random = seededRandom(scenario.seed)
      const state = tracker.create()
      const taken = []
      let now = scenario.start
      let cursor = null
      let counting = false
      let expected = 0
      let dropped = 0
      for (let step = 0; step < 400; step++) {
        // log-uniform step from 1 ms to 20 minutes, so some steps pass maxGapMs
        now += Math.ceil(Math.exp(random() * Math.log(20 * MIN)))
        const app = apps[Math.floor(random() * apps.length)]
        const away = random() < 0.25
        if (cursor !== null && counting) {
          if (now - cursor <= gap) expected += now - cursor
          else dropped++
        }
        tracker.observe(state, now, app, away, gap)
        cursor = now
        counting = !away
        if (step % 100 === 99) taken.push(tracker.takePending(state))
      }
      taken.push(tracker.takePending(state))

      let counted = 0
      const dates = new Set()
      for (const batch of taken) {
        for (const day of Object.values(batch)) {
          const label = scenario.name + " " + day.date
          dates.add(day.date)
          assert.match(day.date, /^\d{4}-\d{2}-\d{2}$/, label)
          assert.equal(day.hours_ms.length, 24, label)
          assert.equal(sum(day.hours_ms), day.active_ms, label + ": hours_ms must add up to active_ms")
          const named = sum(Object.values(day.apps_ms))
          assert.ok(named <= day.active_ms, label + ": apps_ms " + named + " > active_ms " + day.active_ms)
          const values = [day.active_ms, day.switches, ...day.hours_ms, ...Object.values(day.apps_ms)]
          for (const value of values) {
            assert.ok(Number.isInteger(value) && value >= 0, label + ": " + value)
          }
          counted += day.active_ms
        }
      }
      assert.equal(counted, expected, scenario.name + ": counted time must match the readings that were in range")
      // the run must exercise what it claims to
      assert.ok(now > scenario.through, scenario.name + ": the run ended before the interesting hours")
      assert.ok(dropped > 0,scenario.name + ": no gap was dropped")
      assert.ok(counted > 0, scenario.name + ": nothing was counted")
      assert.ok(dates.size >= 2, scenario.name + ": stayed within one day")
    }
  })
})

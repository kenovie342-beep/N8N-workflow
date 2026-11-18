// Cron next-fire computation with IANA timezones (no dependencies).
const NAMES = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12, sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };
const RANGES = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]];

function parseField(src, [lo, hi]) {
  const out = new Set();
  for (const part of src.toLowerCase().split(",")) {
    const m = part.match(/^(\*|[a-z0-9]+(?:-[a-z0-9]+)?)(?:\/(\d+))?$/);
    if (!m) throw new Error(`Invalid cron field: ${src}`);
    const step = m[2] ? Number(m[2]) : 1;
    if (step < 1) throw new Error("Invalid cron step");
    let a, b;
    if (m[1] === "*") [a, b] = [lo, hi];
    else {
      const [x, y] = m[1].split("-").map((v) => (v in NAMES ? NAMES[v] : /^\d+$/.test(v) ? Number(v) : NaN));
      a = x; b = y === undefined ? (m[2] ? hi : x) : y;
    }
    if (Number.isNaN(a) || Number.isNaN(b) || a < lo || b > hi || a > b) throw new Error(`Cron value out of range: ${part}`);
    for (let v = a; v <= b; v += step) out.add(v);
  }
  return out;
}

export function parseCron(expr) {
  const f = String(expr || "").trim().split(/\s+/);
  if (f.length !== 5) throw new Error("Cron needs 5 fields: minute hour day-of-month month day-of-week");
  const [mi, h, dom, mon, dow] = f.map((s, i) => parseField(s, RANGES[i]));
  if (dow.has(7)) { dow.delete(7); dow.add(0); }
  return { mi: [...mi].sort((a, b) => a - b), h: [...h].sort((a, b) => a - b), dom, mon, dow, domStar: f[2] === "*", dowStar: f[4] === "*" };
}

export function tzOffsetMs(tz, date) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    .formatToParts(date).map((x) => [x.type, x.value]));
  return Date.UTC(+p.year, p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(date.getTime() / 1000) * 1000;
}
export function assertTimezone(tz) {
  try { new Intl.DateTimeFormat("en-US", { timeZone: tz }); } catch { throw new Error(`Unknown timezone: ${tz}`); }
}
export function localToUtc(tz, y, mo, d, h, mi) {
  const guess = Date.UTC(y, mo - 1, d, h, mi);
  let utc = guess - tzOffsetMs(tz, new Date(guess));
  const off2 = tzOffsetMs(tz, new Date(utc));
  utc = guess - off2;
  return utc;
}
export function localParts(tz, ms) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
    .formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return { y: +p.year, mo: +p.month, d: +p.day, h: +p.hour, mi: +p.minute };
}

/** Next fire time (epoch ms) strictly after afterMs, evaluated in timezone tz. */
export function nextCron(expr, tz, afterMs) {
  assertTimezone(tz);
  const c = parseCron(expr);
  const base = localParts(tz, afterMs);
  for (let i = 0; i < 366 * 5; i++) {
    const day = new Date(Date.UTC(base.y, base.mo - 1, base.d + i));
    const y = day.getUTCFullYear(), mo = day.getUTCMonth() + 1, d = day.getUTCDate(), dw = day.getUTCDay();
    if (!c.mon.has(mo)) continue;
    const domOk = c.dom.has(d), dowOk = c.dow.has(dw);
    if (!(c.domStar || c.dowStar ? domOk && dowOk : domOk || dowOk)) continue;
    for (const h of c.h) for (const mi of c.mi) {
      const utc = localToUtc(tz, y, mo, d, h, mi);
      const back = localParts(tz, utc);
      if (utc > afterMs && back.h === h && back.mi === mi) return utc;   // skips DST-gap times
    }
  }
  throw new Error("Cron expression never fires");
}

/** Bridge/DB storage is always UTC ("YYYY-MM-DDTHH:MMZ") — the user sees everything in their own
 * browser timezone. Extracted out of clanConfigTable.ts (2026-08-23, plans/cwl-personal-hub.md
 * Phase 5e) since playerPrefs.ts's block II needs the exact same UTC->local conversion for its
 * read-only CWL-start-time column — a copy would drift, so this is the one place it lives. */

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** Snaps a "HH:MM" string to the nearest quarter hour. Real cwl_start_at values already only
 * ever land on a quarter-hour boundary — clanConfigTable.ts's own admin-facing time picker only
 * offers :00/:15/:30/:45 — so this is a no-op for any value that actually came from the bridge;
 * it only does real rounding while clanConfigTable.ts is constructing a NEW value from its own
 * free-form picker input. */
function snapToQuarterHour(hhmm: string): string {
  const [hStr, mStr] = hhmm.split(':')
  const h = Number(hStr) || 0
  const m = Number(mStr) || 0
  const snappedM = Math.round(m / 15) * 15
  const carry = snappedM === 60
  const finalH = (h + (carry ? 1 : 0)) % 24
  return `${pad(finalH)}:${pad(carry ? 0 : snappedM)}`
}

/** Converts a UTC "YYYY-MM-DDTHH:MMZ"-ish string to the viewer's local {date, time} parts, or
 * null if it doesn't parse. */
export function utcStringToLocalParts(utc: string): { date: string; time: string } | null {
  const parsed = new Date(utc)
  if (Number.isNaN(parsed.getTime())) return null
  return {
    date: `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`,
    time: snapToQuarterHour(`${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`),
  }
}

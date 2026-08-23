/** Shared CWL signup-status icon/label definitions (2026-08-23, plans/cwl-personal-hub.md
 * Phase 5e) — extracted out of enrollmentBoard.ts so the Manage Teams board and the new
 * playerPrefs.ts screen can never disagree about what each status looks like, especially the
 * new 'auto_confirmed' one.
 *
 * Localization split (Phase 6e): enrollmentBoard.ts is an admin/leader tool that stays
 * English-only for now (a large, separate conversion job with real regression surface, out of
 * scope here) and keeps calling statusLabel() with no translator, which falls back to the plain
 * STATUS_LABEL English strings below. playerPrefs.ts passes its own Translator (fetched from
 * GET /api/i18n) and gets the cwl.activity.status_* keys instead. Both read from the exact same
 * STATUS_ICON map either way — the icon never depends on language. */

import gcheckIconUrl from './assets/gcheck.svg'
import pendingIconUrl from './assets/pending.svg'
import redxIconUrl from './assets/redx.svg'
import autoConfirmedIconUrl from './assets/autoconfirmed.svg'
import type { Translator } from './i18n'

// The statuses a member's own DM response can produce, plus 'auto_confirmed' — seeded
// automatically by a standing opt-in preference at Start Enrollment, never written by an admin
// action or a member's own click (a real DM response always overwrites it with
// 'confirmed'/'declined'). Anything else (no signup row yet, or a legacy 'withdrawn' value) has
// no icon/label here at all.
export type VisibleStatus = 'pending' | 'confirmed' | 'declined' | 'auto_confirmed'

export const STATUS_ICON: Record<VisibleStatus, string> = {
  pending: pendingIconUrl,
  confirmed: gcheckIconUrl,
  declined: redxIconUrl,
  auto_confirmed: autoConfirmedIconUrl,
}

/** Plain English defaults — used directly by enrollmentBoard.ts (Phase 6e: not converted to
 * i18n) and as statusLabel()'s fallback when called with no translator. */
export const STATUS_LABEL: Record<VisibleStatus, string> = {
  pending: 'Pending',
  confirmed: 'Confirmed',
  declined: 'Declined',
  auto_confirmed: 'Auto-Confirmed',
}

const STATUS_LABEL_KEY: Record<VisibleStatus, string> = {
  pending: 'status_pending',
  confirmed: 'status_confirmed',
  declined: 'status_declined',
  auto_confirmed: 'status_auto_confirmed',
}

/** Localized label for one status. Pass a Translator (playerPrefs.ts, from cwl.activity.*) to
 * get it in the viewer's own language; omit it (enrollmentBoard.ts) to get the plain English
 * STATUS_LABEL text — this is what lets the admin board keep passing no translator at all. */
export function statusLabel(status: VisibleStatus, t?: Translator): string {
  return t ? t(STATUS_LABEL_KEY[status]) : STATUS_LABEL[status]
}

export function isVisibleStatus(status: string | null | undefined): status is VisibleStatus {
  return status === 'pending' || status === 'confirmed' || status === 'declined' || status === 'auto_confirmed'
}

/**
 * 2026-08-23, plans/cwl-personal-hub.md Phase 6d. The Activity has no server-side rendering of
 * its own, so it can't call qapbot/i18n.py's t() directly — instead it fetches a flat
 * {key: template} dict once at launch (GET /api/i18n, Phase 6c) and formats locally with this.
 *
 * Deliberately NOT a build-time-generated catalog and NOT a hand-maintained second copy of the
 * translations — see the plan's Phase 6a for why: only the bot can see a member's own language
 * preference, and a client-side catalog would need a Pages deploy for every string fix.
 */

export type Translator = (key: string, vars?: Record<string, string | number>) => string

/** Builds a translator over one namespace's flat {key: template} dict (as returned by
 * GET /api/i18n). Missing key -> returns the key itself, matching TranslationManager's own
 * last-resort behavior (qapbot/i18n.py) so a missing string is visibly wrong rather than blank.
 * A {placeholder} with no matching var is left intact rather than throwing, since a partial
 * render is more useful here than a crashed screen. */
export function createTranslator(strings: Record<string, string>): Translator {
  return (key: string, vars?: Record<string, string | number>): string => {
    const template = strings[key]
    if (template === undefined) return key
    if (!vars) return template
    return template.replace(/\{(\w+)\}/g, (match, name: string) =>
      Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match,
    )
  }
}

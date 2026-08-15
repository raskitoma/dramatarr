/** The settings page's section index.
 *
 * Kept as a named component after `PageToc` was generalized out of it: the settings screen and
 * its tests refer to this name, and a wrapper is cheaper than renaming a call site to gain
 * nothing. New pages should use `PageToc` directly.
 */

import { PageToc, type TocSection } from './PageToc'

export type { TocSection }

export function SettingsToc({ sections }: { sections: readonly TocSection[] }) {
  return <PageToc sections={sections} labelKey="admin.settings.tocLabel" />
}

export default SettingsToc

/** Index on the left, page on the right — the shell every long admin screen shares.
 *
 * The settings page grew this layout first and three more pages then needed the identical
 * grid, so it lives here instead of being copied into four stylesheets that would drift.
 *
 * Sections may be computed per render (the queue page's "running" section only exists while
 * something is running), so a caller can hand over a filtered list and the index will simply
 * not offer a link to a heading that is not on the page.
 */

import type { ReactNode } from 'react'

import { PageToc, type TocSection } from './PageToc'

import styles from './TocLayout.module.css'

export interface TocLayoutProps {
  sections: readonly TocSection[]
  /** i18n key naming the index. Defaults to the settings page's wording. */
  labelKey?: string
  children: ReactNode
}

export function TocLayout({ sections, labelKey, children }: TocLayoutProps) {
  // An index of one entry is furniture, not navigation: it takes a column of width and tells
  // the reader something the heading already told them.
  if (sections.length < 2) return <>{children}</>

  return (
    <div className={styles.layout}>
      <PageToc sections={sections} labelKey={labelKey} />
      <div className={styles.content}>{children}</div>
    </div>
  )
}

export default TocLayout

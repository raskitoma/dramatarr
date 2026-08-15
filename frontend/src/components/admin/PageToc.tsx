/** Section index for a long admin page.
 *
 * Generalized out of the settings screen once a second and third page needed the same thing.
 * The only per-page differences turned out to be the list of sections and the heading, so
 * those are props and nothing else is.
 *
 * The links are real anchors. Keyboard navigation, "open in new tab", the back button and
 * deep links like `/admin/settings#settings-metadata` all work because of that, not because
 * of the observer below — which decides only which entry is highlighted, and is allowed to
 * not exist.
 */

import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import styles from './PageToc.module.css'

export interface TocSection {
  id: string
  /** i18n key for the label. */
  labelKey: string
}

export interface PageTocProps {
  sections: readonly TocSection[]
  /** i18n key naming the index itself, for the landmark and the visible heading. */
  labelKey?: string
}

export function PageToc({ sections, labelKey = 'admin.settings.tocLabel' }: PageTocProps) {
  const { t } = useTranslation()
  const [active, setActive] = useState<string>(sections[0]?.id ?? '')

  useEffect(() => {
    // Highlighting is an enhancement, not the feature: the links are anchors and navigate
    // with or without this. Anywhere without an IntersectionObserver — jsdom, an older
    // browser — simply keeps the first entry marked rather than failing to render.
    if (typeof IntersectionObserver === 'undefined') return

    const elements = sections
      .map((section) => document.getElementById(section.id))
      .filter((element): element is HTMLElement => element !== null)
    if (elements.length === 0) return

    // The top offset keeps a section from counting as "current" while it is still behind the
    // sticky header; the generous bottom offset stops the last, short section from being
    // unreachable because it can never fill the viewport.
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        const first = visible[0]
        if (first) setActive(first.target.id)
      },
      { rootMargin: '-72px 0px -55% 0px', threshold: 0 },
    )

    for (const element of elements) observer.observe(element)
    return () => observer.disconnect()
  }, [sections])

  return (
    <nav className={styles.toc} aria-label={t(labelKey)}>
      <p className={styles.heading}>{t(labelKey)}</p>
      <ul className={styles.list}>
        {sections.map((section) => (
          <li key={section.id}>
            <a
              href={`#${section.id}`}
              className={styles.link}
              aria-current={active === section.id ? 'true' : undefined}
            >
              {t(section.labelKey)}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  )
}

export default PageToc

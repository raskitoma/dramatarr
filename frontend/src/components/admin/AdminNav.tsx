/** Sub-navigation for the admin area.
 *
 * The header only ever links to `/admin`; without this, `/admin/users` had no inbound link
 * anywhere in the app and `/admin/settings` was reachable only from the TMDB match dialog.
 * It renders once, in `App.tsx`'s admin wrapper, so every `/admin/*` route gets it and no
 * page has to remember to include it.
 *
 * `/admin` matches exactly — a prefix test would light it up on every child route — while the
 * others match by prefix so the metadata editor at `/admin/library/:id` still reads as
 * "Library".
 */

import { useTranslation } from 'react-i18next'
import { Link, useLocation } from 'wouter'

import styles from './AdminNav.module.css'

interface AdminNavItem {
  href: string
  labelKey: string
  /** `/admin` is a page in its own right, not the parent of the others. */
  exact?: boolean
}

const ITEMS: readonly AdminNavItem[] = [
  { href: '/admin', labelKey: 'admin.dashboard', exact: true },
  { href: '/admin/queue', labelKey: 'nav.queue' },
  { href: '/admin/library', labelKey: 'nav.library' },
  { href: '/admin/import', labelKey: 'nav.import' },
  { href: '/admin/requests', labelKey: 'nav.requests' },
  { href: '/admin/users', labelKey: 'nav.users' },
  { href: '/admin/settings', labelKey: 'nav.settings' },
]

function isActive(item: AdminNavItem, location: string): boolean {
  if (item.exact) return location === item.href
  return location === item.href || location.startsWith(`${item.href}/`)
}

export function AdminNav() {
  const { t } = useTranslation()
  const [location] = useLocation()

  return (
    <nav aria-label={t('nav.admin')} className={styles.nav}>
      {ITEMS.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          className={styles.link}
          aria-current={isActive(item, location) ? 'page' : undefined}
        >
          {t(item.labelKey)}
        </Link>
      ))}
    </nav>
  )
}

export default AdminNav

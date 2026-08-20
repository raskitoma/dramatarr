/** The application header: identity, navigation, search and the per-user controls.
 *
 * Below 720px the primary nav, the search field and the appearance controls collapse behind a
 * single disclosure button; the brand and the avatar menu stay put so the two things people
 * reach for most on a phone are always one tap away. The theme/locale controls are rendered
 * twice (header cluster and panel) and hidden with CSS rather than swapped at a breakpoint,
 * which keeps their state where it belongs — in the providers — and avoids a resize remount.
 */

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation } from 'wouter'

import { avatarUrlOf, type User } from '@/api/types'
import { useAuth } from '@/hooks/useAuth'

import { Avatar } from '@/components/Avatar'
import { HeaderSearch } from '@/components/HeaderSearch'
import { IconButton } from '@/components/IconButton'
import { LocaleSelect } from '@/components/LocaleSelect'
import { ThemeToggle } from '@/components/ThemeToggle'
import { useToast } from '@/components/Toast'
import { cx } from '@/components/cx'
import { CloseIcon, MenuIcon } from '@/components/icons'
import { useDismissOnOutside } from '@/hooks/useDismissOnOutside'

import styles from './AppHeader.module.css'

export interface NavItem {
  href: string
  labelKey: string
  /**
   * Route prefixes this item also owns, beyond its own `href`.
   *
   * Without these the highlight only ever appeared on the four destinations themselves, so it
   * went out the moment you opened anything — which is most of the time you are using the app.
   * A series page and an actor page are both places *in the catalogue*, so Explorar keeps the
   * light. That is a small claim about where you are rather than how you got here: you can
   * reach a series from home or from search too, and no single answer is right for all three.
   * Naming the catalogue is more useful than naming nothing.
   */
  owns?: readonly string[]
  /**
   * A nav item that is not one of the places you *browse*.
   *
   * Pedidos is a different kind of verb from Inicio and Explorar — it is where you ask for
   * something the house does not have — and the owner wanted that legible at a glance rather
   * than inferred from the word. Its own colour, not the accent, so it can never be confused
   * with the "you are here" treatment sitting right beside it.
   */
  tone?: 'requests'
}

export const NAV_ITEMS: readonly NavItem[] = [
  // Deliberately no `owns`. Home is the one route that would swallow every path under a prefix
  // test, so it matches exactly and nothing else.
  { href: '/', labelKey: 'nav.home' },
  { href: '/browse', labelKey: 'nav.browse', owns: ['/series', '/people'] },
  { href: '/search', labelKey: 'nav.search' },
  // Last, and deliberately after search: asking for something is what you do once looking for
  // it has failed, and putting it earlier would invite requests for things already on the NAS.
  { href: '/requests', labelKey: 'nav.requests', tone: 'requests' },
  // After Pedidos, because it is about what is already here rather than what is not. It earns
  // its place in the bar rather than hiding in the account menu: the whole point is checking it
  // *before* leaving, and something you have to go looking for gets checked on the plane.
  { href: '/downloads', labelKey: 'nav.downloads' },
]

/**
 * Whether this item is the one the current location belongs to.
 *
 * `/account`, `/watch/…` and the whole admin area answer false for every item, and that is
 * correct — none of them is reached from this menu, so lighting one would point at the wrong
 * door. (The player has no header at all.)
 */
export function ownsLocation(item: NavItem, location: string): boolean {
  if (location === item.href) return true
  // `startsWith` on the bare prefix would make `/serieskkk` match `/series`.
  return (item.owns ?? []).some(
    (prefix) => location === prefix || location.startsWith(`${prefix}/`),
  )
}

function NavLinks({ linkClassName }: { linkClassName: string | undefined }) {
  const { t } = useTranslation()
  const [location] = useLocation()

  return (
    <>
      {NAV_ITEMS.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          className={cx(linkClassName, item.tone === 'requests' && styles.navRequests)}
          aria-current={ownsLocation(item, location) ? 'page' : undefined}
        >
          {t(item.labelKey)}
        </Link>
      ))}
    </>
  )
}

function AvatarMenu({ user }: { user: User }) {
  const { t } = useTranslation()
  const { isAdmin, signOut } = useAuth()
  const toast = useToast()
  const [, navigate] = useLocation()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const menuId = useId()

  const close = useCallback(() => {
    setOpen(false)
  }, [])

  useDismissOnOutside(open, close, wrapRef)

  useEffect(() => {
    if (!open) return
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
  }, [open])

  function moveFocus(delta: number): void {
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [],
    )
    if (items.length === 0) return
    const current = items.findIndex((item) => item === document.activeElement)
    items[(current + delta + items.length) % items.length]?.focus()
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    switch (event.key) {
      case 'Escape':
        event.preventDefault()
        close()
        triggerRef.current?.focus()
        break
      case 'ArrowDown':
        event.preventDefault()
        moveFocus(1)
        break
      case 'ArrowUp':
        event.preventDefault()
        moveFocus(-1)
        break
      default:
        break
    }
  }

  async function handleSignOut(): Promise<void> {
    close()
    await signOut()
    toast.show(t('auth.signedOut'), 'info')
    navigate('/login', { replace: true })
  }

  return (
    <div className={styles.avatarWrap} ref={wrapRef} onKeyDown={handleKeyDown}>
      <button
        type="button"
        ref={triggerRef}
        className={styles.avatarButton}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => {
          setOpen((previous) => !previous)
        }}
      >
        {/* The picture and the colour are both user data; Avatar owns the circle so the
            header, the account screen and the users table cannot drift apart. */}
        <Avatar
          name={user.displayName}
          email={user.email}
          src={avatarUrlOf(user)}
          color={user.avatarColor}
          size="md"
        />
        <span className="sr-only">{t('nav.userMenu')}</span>
      </button>

      {open ? (
        <div id={menuId} role="menu" className={styles.menu} ref={menuRef}>
          <div className={styles.menuHeader}>
            <Avatar
              name={user.displayName}
              email={user.email}
              src={avatarUrlOf(user)}
              color={user.avatarColor}
              size="sm"
            />
            <span className={styles.menuIdentity}>
              <span className={styles.menuName}>{user.displayName}</span>
              <span className={styles.menuEmail}>{user.email}</span>
            </span>
          </div>
          <Link href="/account" role="menuitem" className={styles.menuItem}>
            {t('nav.account')}
          </Link>
          {isAdmin ? (
            <Link href="/admin" role="menuitem" className={styles.menuItem}>
              {t('nav.admin')}
            </Link>
          ) : null}
          <button
            type="button"
            role="menuitem"
            className={`${styles.menuItem} ${styles.menuDanger}`}
            onClick={() => {
              void handleSignOut()
            }}
          >
            {t('nav.logout')}
          </button>
        </div>
      ) : null}
    </div>
  )
}

export function AppHeader() {
  const { t } = useTranslation()
  const { user, config } = useAuth()
  const [location] = useLocation()
  const [panelOpen, setPanelOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const headerRef = useRef<HTMLElement | null>(null)
  const toggleWrapRef = useRef<HTMLSpanElement | null>(null)
  const panelId = useId()

  const closePanel = useCallback(() => {
    setPanelOpen(false)
  }, [])

  useDismissOnOutside(panelOpen, closePanel, headerRef)

  /**
   * The two popovers are mutually exclusive, structurally rather than by luck.
   *
   * On a phone they occupy the same strip of screen, so both open at once is not a cosmetic
   * problem — it is the state in which one Escape closes both and the user cannot tell what
   * they dismissed.
   */
  const handleSearchOpenChange = useCallback((next: boolean) => {
    setSearchOpen(next)
    if (next) setPanelOpen(false)
  }, [])

  // Any navigation dismisses both; otherwise they hang over the new page.
  useEffect(() => {
    setPanelOpen(false)
    setSearchOpen(false)
  }, [location])

  function handleHeaderKeyDown(event: KeyboardEvent<HTMLElement>): void {
    if (event.key !== 'Escape' || !panelOpen) return
    event.preventDefault()
    setPanelOpen(false)
    // Focus goes back to the control that opened the panel, not to the top of the document.
    toggleWrapRef.current?.querySelector('button')?.focus()
  }

  const siteName = config?.siteName ?? t('app.name')

  return (
    <header className={styles.header} ref={headerRef} onKeyDown={handleHeaderKeyDown}>
      {/* First focusable element on the page, per the contract. */}
      <a className="skip-link" href="#main">
        {t('nav.skipToContent')}
      </a>

      <div className={styles.inner}>
        <span className={styles.menuToggle} ref={toggleWrapRef}>
          {/* The label follows the state rather than staying "Menu": aria-expanded alone tells
              a screen-reader user the panel is open, but not that this button is what shuts it. */}
          <IconButton
            variant="ghost"
            label={panelOpen ? t('nav.closeMenu') : t('nav.menu')}
            aria-expanded={panelOpen}
            aria-controls={panelOpen ? panelId : undefined}
            onClick={() => {
              setPanelOpen((previous) => !previous)
              setSearchOpen(false)
            }}
          >
            {panelOpen ? <CloseIcon /> : <MenuIcon />}
          </IconButton>
        </span>

        <Link href="/" className={styles.brand}>
          {/* Decorative: the site name is right beside it, and announcing the mark as well
              would have a screen reader read the app's name twice on every page. */}
          <img className={styles.mark} src="/icon.svg" alt="" width={28} height={28} />
          <span className={styles.brandName}>{siteName}</span>
        </Link>

        <nav className={styles.nav} aria-label={t('nav.menu')}>
          <NavLinks linkClassName={styles.navLink} />
        </nav>

        <span className={styles.spacer} />

        {/* Immediately before the theme controls, which is where the owner asked for it. A
            sibling of `.controls` rather than a child, so it never inherits that cluster's
            `display: none` below the breakpoint — the magnifier is the one control that has to
            survive on a phone. */}
        <HeaderSearch open={searchOpen} onOpenChange={handleSearchOpenChange} />

        <div className={styles.controls}>
          <ThemeToggle />
          <LocaleSelect />
        </div>

        {user ? <AvatarMenu user={user} /> : null}
      </div>

      {panelOpen ? (
        <div id={panelId} className={styles.panel}>
          <nav className={styles.panelNav} aria-label={t('nav.menu')}>
            <NavLinks linkClassName={styles.navLink} />
          </nav>
          <div className={styles.panelControls}>
            <ThemeToggle />
            <LocaleSelect />
          </div>
        </div>
      ) : null}
    </header>
  )
}

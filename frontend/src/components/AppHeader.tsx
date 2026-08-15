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
  type FormEvent,
  type KeyboardEvent,
  type RefObject,
} from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useSearch } from 'wouter'

import { avatarUrlOf, type User } from '@/api/types'
import { useAuth } from '@/hooks/useAuth'

import { Avatar } from '@/components/Avatar'
import { IconButton } from '@/components/IconButton'
import { Input } from '@/components/Input'
import { LocaleSelect } from '@/components/LocaleSelect'
import { ThemeToggle } from '@/components/ThemeToggle'
import { useToast } from '@/components/Toast'
import { CloseIcon, MenuIcon, SearchIcon } from '@/components/icons'

import styles from './AppHeader.module.css'

interface NavItem {
  href: string
  labelKey: string
}

const NAV_ITEMS: readonly NavItem[] = [
  { href: '/', labelKey: 'nav.home' },
  { href: '/browse', labelKey: 'nav.browse' },
  { href: '/search', labelKey: 'nav.search' },
  // Last, and deliberately after search: asking for something is what you do once looking for
  // it has failed, and putting it earlier would invite requests for things already on the NAS.
  { href: '/requests', labelKey: 'nav.requests' },
]

/** Closes a popup when a pointer lands outside it, or when focus moves out of it. */
function useDismissOnOutside(
  open: boolean,
  close: () => void,
  container: RefObject<HTMLElement | null>,
): void {
  useEffect(() => {
    if (!open) return undefined

    function isOutside(target: EventTarget | null): boolean {
      const node = container.current
      return node !== null && target instanceof Node && !node.contains(target)
    }

    function handlePointerDown(event: PointerEvent): void {
      if (isOutside(event.target)) close()
    }

    function handleFocusIn(event: FocusEvent): void {
      if (isOutside(event.target)) close()
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('focusin', handleFocusIn)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('focusin', handleFocusIn)
    }
  }, [open, close, container])
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
          className={linkClassName}
          aria-current={location === item.href ? 'page' : undefined}
        >
          {t(item.labelKey)}
        </Link>
      ))}
    </>
  )
}

/** Search box that submits to /search?q=… and stays in step with the URL it navigates to. */
function HeaderSearch({ className }: { className: string | undefined }) {
  const { t } = useTranslation()
  const [location, navigate] = useLocation()
  const search = useSearch()
  const inputId = useId()

  const urlQuery = location === '/search' ? (new URLSearchParams(search).get('q') ?? '') : ''
  const [value, setValue] = useState(urlQuery)
  // Adjusting state during render rather than in an effect: landing on /search?q=x must show
  // `x` in the field on the same paint, with no intermediate frame holding the old value.
  const [lastUrlQuery, setLastUrlQuery] = useState(urlQuery)
  if (urlQuery !== lastUrlQuery) {
    setLastUrlQuery(urlQuery)
    setValue(urlQuery)
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    const query = value.trim()
    if (!query) return
    navigate(`/search?q=${encodeURIComponent(query)}`)
  }

  return (
    <form role="search" className={className} onSubmit={handleSubmit}>
      <label htmlFor={inputId} className="sr-only">
        {t('common.search')}
      </label>
      {/* Decoration, not a control: the field is already named by its label, and a magnifier
          that could be clicked but does nothing is worse than one that plainly cannot. */}
      <SearchIcon className={styles.searchIcon} aria-hidden="true" />
      <Input
        id={inputId}
        name="q"
        type="search"
        autoComplete="off"
        placeholder={t('search.placeholder')}
        value={value}
        onChange={(event) => {
          setValue(event.target.value)
        }}
      />
    </form>
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
  const headerRef = useRef<HTMLElement | null>(null)
  const toggleWrapRef = useRef<HTMLSpanElement | null>(null)
  const panelId = useId()

  const closePanel = useCallback(() => {
    setPanelOpen(false)
  }, [])

  useDismissOnOutside(panelOpen, closePanel, headerRef)

  // Any navigation dismisses the collapsed panel; otherwise it hangs over the new page.
  useEffect(() => {
    setPanelOpen(false)
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
            }}
          >
            {panelOpen ? <CloseIcon /> : <MenuIcon />}
          </IconButton>
        </span>

        <Link href="/" className={styles.brand}>
          {siteName}
        </Link>

        <nav className={styles.nav} aria-label={t('nav.menu')}>
          <NavLinks linkClassName={styles.navLink} />
        </nav>

        <HeaderSearch className={styles.search} />

        <span className={styles.spacer} />

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
          <HeaderSearch className={styles.panelSearch} />
          <div className={styles.panelControls}>
            <ThemeToggle />
            <LocaleSelect />
          </div>
        </div>
      ) : null}
    </header>
  )
}

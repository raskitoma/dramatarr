/** An actor, and everything the indexer has them in — `/people/:id`.
 *
 * The screen that did not exist and could not have. Vertical short drama is credited nowhere:
 * TMDB does not catalogue it, IMDb reaches about a fifth of it, and the platforms publish a
 * name with no way to search by it. DramaDex scrapes the credits, so "what else is this person
 * in" finally has an answer — across its whole catalogue, not just the shorts this household
 * happens to own.
 *
 * Which is why every credit says whether it is `inLibrary`. Without that the page is a list of
 * titles a member cannot do anything with; with it, it is "you already have four of these, and
 * here are eleven you could ask for". The credits already here become links; the rest become
 * something to request.
 *
 * Open to any signed-in member. This is browsing, not administration.
 */

import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'wouter'

import { adminKeys, dramadexPerson } from '@/api/admin'
import { Badge } from '@/components/Badge'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { Poster } from '@/components/Poster'
import { Spinner } from '@/components/Spinner'
import { ExternalLinkIcon } from '@/components/icons'
import { initialsOf } from '@/lib/format'

import styles from './PersonPage.module.css'

export function PersonPage() {
  const { t } = useTranslation()
  const params = useParams()
  const personId = Number.parseInt(String(params['id'] ?? ''), 10)

  const query = useQuery({
    queryKey: adminKeys.dramadexPerson(personId),
    queryFn: () => dramadexPerson(personId),
    enabled: Number.isInteger(personId) && personId > 0,
    staleTime: 5 * 60_000,
  })

  if (!Number.isInteger(personId) || personId <= 0) {
    return <EmptyState title={t('errors.NOT_FOUND')} />
  }
  if (query.isPending) {
    return (
      <div className={styles.centered}>
        <Spinner label={t('common.loading')} />
      </div>
    )
  }
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />
  }

  const person = query.data
  const owned = person.credits.filter((credit) => credit.inLibrary)
  const rest = person.credits.filter((credit) => !credit.inLibrary)

  return (
    <article className={styles.page}>
      <header className={styles.header}>
        <div className={styles.face} aria-hidden="true" data-tint={person.id % 6}>
          {initialsOf(person.name, '?')}
        </div>
        <h1 className={styles.name}>{person.name}</h1>
        <p className={styles.count}>
          {t('person.creditCount', { count: person.credits.length })}
          {owned.length > 0 ? ` · ${t('person.inLibraryCount', { count: owned.length })}` : ''}
        </p>
        {person.bio ? <p className={styles.bio}>{person.bio}</p> : null}
      </header>

      {person.credits.length === 0 ? <EmptyState title={t('person.noCredits')} /> : null}

      {/* What the household already has, first: it is the half a member can act on now. */}
      {owned.length > 0 ? (
        <section className={styles.block} aria-labelledby="person-owned">
          <h2 className={styles.heading} id="person-owned">
            {t('person.inLibrary')}
          </h2>
          <ul className={styles.grid}>
            {owned.map((credit) => (
              <li key={credit.id} className={styles.card}>
                <Poster src={credit.posterUrl} alt="" size="md" fluid />
                <p className={styles.cardTitle}>{credit.title}</p>
                {credit.year === null ? null : <p className={styles.cardMeta}>{credit.year}</p>}
                <Badge tone="success">{t('requests.inLibrary')}</Badge>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {rest.length > 0 ? (
        <section className={styles.block} aria-labelledby="person-rest">
          <h2 className={styles.heading} id="person-rest">
            {t('person.elsewhere')}
          </h2>
          <p className={styles.note}>{t('person.elsewhereHint')}</p>
          <ul className={styles.grid}>
            {rest.map((credit) => (
              <li key={credit.id} className={styles.card}>
                <Poster src={credit.posterUrl} alt="" size="md" fluid />
                <p className={styles.cardTitle}>{credit.title}</p>
                <p className={styles.cardMeta}>
                  {[credit.year, credit.studio].filter(Boolean).join(' · ')}
                </p>
                <div className={styles.cardActions}>
                  {/* Straight to the wishlist with the title filled in — the request screen
                      searches the same index, so it will find exactly this row. */}
                  <Link
                    href={`/requests?q=${encodeURIComponent(credit.title)}`}
                    className={styles.request}
                  >
                    {t('requests.ask')}
                  </Link>
                  {credit.watchUrl ? (
                    <a
                      className={styles.watch}
                      href={credit.watchUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      <ExternalLinkIcon size="1em" aria-hidden="true" />
                      {credit.studio ?? t('person.watch')}
                    </a>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </article>
  )
}

export default PersonPage

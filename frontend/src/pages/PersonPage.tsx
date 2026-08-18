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
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'wouter'

import { adminKeys, dramadexPerson } from '@/api/admin'
import { ApiRequestError } from '@/api/client'
import { useCreateRequest } from '@/api/requests'
import { Badge } from '@/components/Badge'
import { Button } from '@/components/Button'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { Poster } from '@/components/Poster'
import { Spinner } from '@/components/Spinner'
import { useToast } from '@/components/Toast'
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

  // Above every early return, and that is not style. These three used to sit below them, so the
  // first render — the loading one — returned before reaching them and the second called three
  // hooks more. React throws on the count changing, the boundary catches it, and every actor
  // page showed "Algo salió mal" instead of a person.
  const create = useCreateRequest()
  const toast = useToast()
  const [asking, setAsking] = useState<number | null>(null)

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
      {/* Portrait beside the text, at a size worth looking at rather than an avatar tucked in
          a corner. There is no photograph to put here — DramaDex has no person images at all —
          so what fills the frame is initials on the same tint the series page uses for this
          performer. The frame exists at portrait proportions regardless, because the day the
          indexer serves one this is where it goes and nothing around it has to move. */}
      <header className={styles.header}>
        <div className={styles.portrait} aria-hidden="true" data-tint={person.id % 6}>
          {initialsOf(person.name, '?')}
        </div>
        <div className={styles.headerText}>
          <h1 className={styles.name}>{person.name}</h1>
          <p className={styles.count}>
            {t('person.creditCount', { count: person.credits.length })}
            {owned.length > 0 ? ` · ${t('person.inLibraryCount', { count: owned.length })}` : ''}
          </p>
          {person.bio ? <p className={styles.bio}>{person.bio}</p> : null}
        </div>
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
                {/* The whole card is the link. Saying "we already have this" and then not
                    taking you to it was the one thing this section could get wrong, and it did:
                    `inLibrary` travelled without an address until `librarySlug` did too. */}
                <Link
                  href={credit.librarySlug ? `/series/${credit.librarySlug}` : '/browse'}
                  className={styles.ownedLink}
                >
                  <Poster src={credit.posterUrl} alt="" size="md" fluid />
                  <p className={styles.cardTitle}>{credit.title}</p>
                  {credit.year === null ? null : <p className={styles.cardMeta}>{credit.year}</p>}
                  <Badge tone="success">{t('requests.inLibrary')}</Badge>
                </Link>
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
                  {/* Asks, here, in one tap.
                      It used to be a link to `/requests?q=<title>` — and that page has never
                      read `?q=`, so it landed you on an empty wishlist search with nothing
                      requested. A control labelled "Pedir" that only navigates is worse than no
                      control: you leave believing you asked. Everything the endpoint needs is
                      already on this card. */}
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={create.isPending && asking === credit.id}
                    onClick={() => {
                      setAsking(credit.id)
                      create.mutate(
                        {
                          title: credit.title,
                          year: credit.year,
                          posterUrl: credit.posterUrl,
                          sourceUrl: credit.watchUrl,
                        },
                        {
                          onSuccess: (row) =>
                            toast.show(
                              row.alreadyExisted
                                ? t('requests.alreadyAsked', { title: credit.title })
                                : t('requests.asked', { title: credit.title }),
                              row.alreadyExisted ? 'info' : 'success',
                            ),
                          onError: (error) => {
                            // The one refusal that is good news, and it deserves its own
                            // sentence rather than the generic banner.
                            const code =
                              error instanceof ApiRequestError ? error.code : ''
                            if (code === 'REQUEST_ALREADY_IN_LIBRARY') {
                              toast.show(
                                t('requests.alreadyInLibrary', { title: credit.title }),
                                'info',
                              )
                              return
                            }
                            toast.show(
                              code
                                ? t(`errors.${code}`, { defaultValue: t('errors.generic') })
                                : t('errors.generic'),
                              'error',
                            )
                          },
                        },
                      )
                    }}
                  >
                    {t('requests.ask')}
                  </Button>
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

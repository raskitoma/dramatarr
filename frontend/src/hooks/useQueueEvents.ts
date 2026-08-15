/** Live queue feed over Server-Sent Events.
 *
 * A running scan emits a progress tick several times a second. Refetching the job list on each
 * one would put the API under more load than the scan itself, so progress events are written
 * straight into the TanStack Query cache with `setQueryData`; the bound progress bar re-renders
 * from cache and never issues a request. Only the events that change list *membership*
 * (created / started / finished) invalidate anything.
 *
 * `EventSource` retries on its own, but with a fixed one-second interval and no ceiling, which
 * hammers the server while it is restarting. So every error closes the socket and we schedule
 * our own reconnect with exponential backoff plus jitter, resetting the delay once a connection
 * opens successfully.
 */

import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { ADMIN_EVENTS_URL, adminKeys } from '@/api/admin'
import type { Job, JobDetail, Page, QueueState } from '@/api/types'

const BASE_DELAY_MS = 1_000
const MAX_DELAY_MS = 30_000

export interface QueueEventsResult {
  /** False while the stream is down, so the UI can surface `admin.queue.disconnected`. */
  connected: boolean
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** The payload is server-controlled JSON, so it is validated rather than cast blindly. */
function parseJob(raw: string): Job | null {
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return null
  }
  if (!isRecord(data)) return null
  if (typeof data.id !== 'string' || typeof data.status !== 'string') return null
  if (typeof data.type !== 'string') return null
  return data as unknown as Job
}

function parseQueueState(raw: string): QueueState | null {
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return null
  }
  if (!isRecord(data)) return null
  if (typeof data.paused !== 'boolean' || typeof data.running !== 'boolean') return null
  return data as unknown as QueueState
}

function eventPayload(event: Event): string | null {
  if (!(event instanceof MessageEvent)) return null
  const data: unknown = event.data
  return typeof data === 'string' ? data : null
}

/**
 * Write a job into every cache that already holds it. Nothing is inserted where the job is
 * absent: a job that does not belong to a cached page must not appear out of order, so
 * membership changes are left to the invalidation path.
 */
function applyJob(client: QueryClient, job: Job): void {
  client.setQueriesData<Page<Job>>({ queryKey: adminKeys.jobLists() }, (page) => {
    if (!page) return page
    let changed = false
    const items = page.items.map((item) => {
      if (item.id !== job.id) return item
      changed = true
      return job
    })
    return changed ? { ...page, items } : page
  })

  // Keep the already-loaded log lines; the event payload carries the job only.
  client.setQueryData<JobDetail>(adminKeys.job(job.id), (detail) =>
    detail ? { ...detail, ...job } : detail,
  )

  client.setQueryData<QueueState>(adminKeys.queue(), (state) => {
    if (!state || state.current === null || state.current.id !== job.id) return state
    return { ...state, current: job }
  })
}

export function useQueueEvents(enabled = true): QueueEventsResult {
  const client = useQueryClient()
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    if (!enabled) {
      setConnected(false)
      return
    }
    if (typeof EventSource === 'undefined') return

    let disposed = false
    let source: EventSource | null = null
    let timer: ReturnType<typeof setTimeout> | null = null
    let attempt = 0

    function refreshLists(): void {
      void client.invalidateQueries({ queryKey: adminKeys.jobLists() })
      void client.invalidateQueries({ queryKey: adminKeys.queue() })
    }

    function scheduleReconnect(): void {
      if (disposed) return
      const delay = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** attempt)
      attempt += 1
      // Jitter keeps several open tabs from reconnecting in lockstep.
      timer = setTimeout(connect, delay + Math.random() * delay * 0.25)
    }

    function connect(): void {
      if (disposed) return
      const stream = new EventSource(ADMIN_EVENTS_URL, { withCredentials: true })
      source = stream

      stream.addEventListener('open', () => {
        attempt = 0
        setConnected(true)
      })

      stream.addEventListener('error', () => {
        setConnected(false)
        stream.close()
        if (source === stream) source = null
        scheduleReconnect()
      })

      const onJob = (invalidate: boolean) => (event: Event) => {
        const raw = eventPayload(event)
        if (raw === null) return
        const job = parseJob(raw)
        if (!job) return
        applyJob(client, job)
        if (invalidate) refreshLists()
      }

      // A job appearing, starting or ending moves it between the three regions of the page,
      // so those three refresh the lists. Progress ticks only ever patch in place.
      stream.addEventListener('job.created', onJob(true))
      stream.addEventListener('job.started', onJob(true))
      stream.addEventListener('job.progress', onJob(false))
      stream.addEventListener('job.finished', (event) => {
        onJob(true)(event)
        void client.invalidateQueries({ queryKey: adminKeys.stats() })
      })

      stream.addEventListener('queue.state', (event) => {
        const raw = eventPayload(event)
        if (raw === null) return
        const state = parseQueueState(raw)
        if (!state) return
        client.setQueryData<QueueState>(adminKeys.queue(), state)
      })
    }

    connect()

    return () => {
      // Without this the browser keeps the socket open and every navigation leaks one more.
      disposed = true
      if (timer !== null) clearTimeout(timer)
      source?.close()
      source = null
    }
  }, [client, enabled])

  return { connected }
}

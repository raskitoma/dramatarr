/** React's view of the download ledger. */

import { useSyncExternalStore } from 'react'

import type { DownloadRow } from './db'
import { pendingCount, snapshot, subscribe } from './downloadManager'

const EMPTY: DownloadRow[] = []

export function useDownloads(): DownloadRow[] {
  return useSyncExternalStore(subscribe, snapshot, () => EMPTY)
}

export function useSeriesDownload(seriesId: string | undefined): DownloadRow | null {
  const rows = useDownloads()
  if (!seriesId) return null
  return rows.find((row) => row.seriesId === seriesId) ?? null
}

/** How many series are downloading or waiting. Drives the badge in the account menu, which is
 *  the only sign of a running download once you have left the series page. */
export function useDownloadCount(): number {
  return useSyncExternalStore(subscribe, pendingCount, () => 0)
}

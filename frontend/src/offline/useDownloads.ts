/** React's view of the download ledger. */

import { useSyncExternalStore } from 'react'

import type { DownloadRow } from './db'
import { snapshot, subscribe } from './downloadManager'

const EMPTY: DownloadRow[] = []

export function useDownloads(): DownloadRow[] {
  return useSyncExternalStore(subscribe, snapshot, () => EMPTY)
}

export function useSeriesDownload(seriesId: string | undefined): DownloadRow | null {
  const rows = useDownloads()
  if (!seriesId) return null
  return rows.find((row) => row.seriesId === seriesId) ?? null
}

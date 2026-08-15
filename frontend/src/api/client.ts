/** Typed fetch wrapper.
 *
 * The session is a httpOnly cookie, so every request must send credentials and no token is
 * ever handled in JS. Errors arrive as `{error: {code, message, details}}`; we surface the
 * stable `code` so the UI can localize it rather than printing a server string.
 */

import type { ApiError } from './types'

export class ApiRequestError extends Error {
  readonly code: string
  readonly status: number
  readonly details: Record<string, unknown>

  constructor(status: number, error: ApiError) {
    super(error.message)
    this.name = 'ApiRequestError'
    this.code = error.code
    this.status = status
    this.details = error.details ?? {}
  }

  /** True when the caller should bounce the user to /login. */
  get isUnauthorized(): boolean {
    return this.status === 401
  }
}

const BASE = '/api'

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  query?: Record<string, string | number | boolean | undefined | null>
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = `${BASE}${path}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

/**
 * True for a multipart body the browser must serialize itself.
 *
 * Setting `Content-Type` by hand on a FormData request is the classic way to break an upload:
 * the header would arrive without the `boundary` the browser picked, and the server would read
 * an unparseable body. So a FormData body travels raw, with no content type of ours.
 */
function isMultipart(body: unknown): body is FormData {
  return typeof FormData !== 'undefined' && body instanceof FormData
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, query, headers, ...rest } = options
  const multipart = isMultipart(body)

  const response = await fetch(buildUrl(path, query), {
    ...rest,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(body !== undefined && !multipart ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    },
    ...(body !== undefined ? { body: multipart ? body : JSON.stringify(body) } : {}),
  })

  if (response.status === 204) return undefined as T

  const isJson = response.headers.get('content-type')?.includes('application/json')

  if (!response.ok) {
    let payload: ApiError = {
      code: 'NETWORK_ERROR',
      message: response.statusText || 'Request failed',
      details: {},
    }
    if (isJson) {
      try {
        const parsed = (await response.json()) as { error?: ApiError }
        if (parsed.error) payload = parsed.error
      } catch {
        // Body was not the shape we expect; the generic payload above stands.
      }
    }
    throw new ApiRequestError(response.status, payload)
  }

  return isJson ? ((await response.json()) as T) : (undefined as T)
}

export const api = {
  get: <T>(path: string, query?: RequestOptions['query']) => request<T>(path, { method: 'GET', query }),
  post: <T>(path: string, body?: unknown, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'POST', body, query }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  /** Multipart POST — the one shape `post` cannot express, since JSON is its whole contract. */
  upload: <T>(path: string, form: FormData) => request<T>(path, { method: 'POST', body: form }),
}

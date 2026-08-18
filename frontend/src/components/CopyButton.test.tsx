// @vitest-environment jsdom
/**
 * Copying, in both the worlds this app is reached from.
 *
 * `navigator.clipboard` exists only in a secure context. The household reaches dramatarr at
 * `https://dramatarr.…` *and* directly at `http://<nas-ip>:8090` on the LAN, where the modern
 * API is undefined — so the deprecated path is not legacy politeness, it is the only thing
 * that works on half the ways in. A button that silently does nothing there would be
 * indistinguishable from one that worked.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { setupI18n } from '@/i18n'

import { CopyButton } from './CopyButton'

beforeAll(async () => {
  await setupI18n('es')
})

beforeEach(() => {
  Reflect.deleteProperty(navigator, 'clipboard')
  Reflect.deleteProperty(document, 'execCommand')
})

afterEach(cleanup)

function givenClipboard(writeText: ReturnType<typeof vi.fn>) {
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
}

function givenExecCommand(fn: ReturnType<typeof vi.fn>) {
  Object.defineProperty(document, 'execCommand', { value: fn, configurable: true })
}

describe('CopyButton', () => {
  it('uses the clipboard API when there is one', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    givenClipboard(writeText)
    render(<CopyButton value="Nero: Made Men" />)

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('Nero: Made Men'))
  })

  it('falls back over plain http, where the clipboard API does not exist', async () => {
    const exec = vi.fn().mockReturnValue(true)
    givenExecCommand(exec)
    render(<CopyButton value="Nero: Made Men" />)

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(exec).toHaveBeenCalledWith('copy'))
  })

  it('falls back when the clipboard API exists but refuses', async () => {
    /* A denied permissions policy, or a document that is not focused. */
    givenClipboard(vi.fn().mockRejectedValue(new Error('NotAllowedError')))
    const exec = vi.fn().mockReturnValue(true)
    givenExecCommand(exec)
    render(<CopyButton value="Nero" />)

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(exec).toHaveBeenCalledWith('copy'))
  })

  it('says it copied, on the button rather than in a toast', async () => {
    givenClipboard(vi.fn().mockResolvedValue(undefined))
    render(<CopyButton value="Nero" />)

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copiado'))
  })

  it('stays quiet when the copy did not happen', async () => {
    givenExecCommand(vi.fn().mockReturnValue(false))
    render(<CopyButton value="Nero" label="Copiar el título" />)

    fireEvent.click(screen.getByRole('button'))

    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.getByRole('button').getAttribute('aria-label')).toBe('Copiar el título')
  })

  it('does not trigger whatever it is sitting inside', () => {
    givenClipboard(vi.fn().mockResolvedValue(undefined))
    const onClick = vi.fn()
    render(
      <div onClick={onClick}>
        <CopyButton value="Nero" />
      </div>,
    )

    fireEvent.click(screen.getByRole('button'))

    expect(onClick).not.toHaveBeenCalled()
  })

  it('leaves nothing behind in the document', async () => {
    givenExecCommand(vi.fn().mockReturnValue(true))
    render(<CopyButton value="Nero" />)

    fireEvent.click(screen.getByRole('button'))

    await waitFor(() => expect(document.querySelectorAll('textarea')).toHaveLength(0))
  })
})

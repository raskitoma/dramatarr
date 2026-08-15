/** What counts as an uploadable image, shared by every upload the app offers.
 *
 * Two screens send images to the server — a member's profile picture and a series poster — and
 * they must agree on what the api container can actually decode. That is not a design choice
 * but a deployment fact: ffmpeg and ffprobe are the only image tooling in the image, so the
 * list below is the intersection of "every browser can produce it" and "ffmpeg can read it".
 *
 * A HEIC straight off an iPhone is deliberately absent, which is why the file inputs are typed
 * rather than `image/*`: having the file picker refuse it is kinder than uploading eight
 * megabytes to be told no.
 */

export const IMAGE_ACCEPT = 'image/png,image/jpeg,image/webp'

export const IMAGE_MIME_TYPES: readonly string[] = ['image/png', 'image/jpeg', 'image/webp']

/**
 * Client-side guard only; the server enforces its own limit and remains the authority.
 *
 * It is here so a 40 MB photo is refused instantly instead of after a progress bar that ends
 * in an error — the rejection is the same either way, the wait is not.
 */
export const IMAGE_MAX_BYTES = 8 * 1000 * 1000

export function isAcceptedImageType(file: File): boolean {
  return IMAGE_MIME_TYPES.includes(file.type)
}

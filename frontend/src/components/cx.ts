/** Tiny className joiner used by every component in the kit.
 *
 * `styles[variant]` is `string | undefined` under `noUncheckedIndexedAccess`, and conditional
 * classes are naturally `false | undefined`, so joining needs one type-safe helper rather than
 * a filter/join dance repeated in sixteen files.
 */

export type ClassValue = string | false | null | undefined

export function cx(...parts: ClassValue[]): string {
  return parts
    .filter((part): part is string => typeof part === 'string' && part.length > 0)
    .join(' ')
}

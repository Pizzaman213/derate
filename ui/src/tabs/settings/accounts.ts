// Admin account forms, as AdminsCard checks them. Pure, so
// `pricing.check.mjs` can load it without a DOM.

/** `PASSWORD_MIN_LEN` and the confirm field, checked on submit. Null = fine.
 *  The server checks the length again and its sentence wins when it refuses. */
export function passwordProblem(password: string, confirm: string, minLen: number): string | null {
  if (password.length < minLen) return `Use at least ${minLen} characters.`
  if (password !== confirm) return 'The two passwords do not match.'
  return null
}

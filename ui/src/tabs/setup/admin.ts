import { PASSWORD_MIN_LEN } from '../../api/types'

/** Why the first-admin form cannot be sent yet, or null when it can.
 *
 *  Only what the browser can know for certain: an empty name or email, a
 *  password shorter than `PASSWORD_MIN_LEN` (the server's own floor, mirrored
 *  from contracts/auth.py), and the two passwords disagreeing. Anything else
 *  the server refuses for -- an email's shape included -- is its sentence,
 *  rendered verbatim -- this never second-guesses it. */
export function adminFormProblem(
  username: string,
  email: string,
  password: string,
  again: string,
): string | null {
  if (!username.trim()) return 'Choose a username.'
  if (!email.trim()) return 'Enter an email address.'
  if (password.length < PASSWORD_MIN_LEN) {
    return `The password needs at least ${PASSWORD_MIN_LEN} characters (${password.length} so far).`
  }
  if (password !== again) return 'The two passwords do not match.'
  return null
}

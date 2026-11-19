# login

The sign-in screen, `/login`. `AppShell`'s auth gate draws it for any path while
an admin exists and this browser holds no `derate_session` cookie, and it is
drawn before the frame, like first run: with no session every panel the chrome
would mount answers 401 `session_required`. A successful `POST /api/auth/login`
re-reads `GET /api/auth/state` and the gate moves on to where the person was
going.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `LoginTab.tsx` | 146 | the form: username, password, the server's refusal rendered verbatim, and the lockout line |
| `lockout.ts` | 75 | a refused sign-in reduced to which refusal it was (`invalid_credentials`, `login_locked`) and how long until the next attempt can succeed. Pure |
| `login.css` | 8 | borrows `setup/setup.css`'s frame-less layout |
| `login.check.mjs` | 66 | hermetic: the wait is worded rounding *up* (61 s is never "1 min"), where it comes from (`retry_after_s`, `Retry-After`), and `setup/admin.ts`'s `adminFormProblem` |
| `state.check.mjs` | 64 | `// requires: coordinator`: the shape of `GET /api/auth/state` (`admin_exists`, `authenticated`, `username`) and the codes the login route answers |

## What the server decides

The lock is the server's: five failures inside 15 minutes for a username or
an ip lock it for 30 s, doubling per further failure up to an hour, answered
as 429 `login_locked`. This screen only words the wait it is given; it keeps
no count of its own.

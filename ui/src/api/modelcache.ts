import type { CachedModel } from './types'

const PREFIX = 'models--'

/** Encoded cache folder for a repository id.
 *
 *  Moved here out of `tabs/storage/ModelCacheCard.tsx` so there is one copy of
 *  the encoder. Mirrors `registry/modelcache.py`'s `folder_for`, whose
 *  docstring notes that this direction is the unambiguous one. */
export function folderFor(modelId: string): string {
  return `${PREFIX}${modelId.trim().replace(/\//g, '--')}`
}

/** True when this folder name can only have come from one repository id.
 *
 *  `registry/modelcache.py`'s `repo_from_folder` says of its own output:
 *  "Ambiguous by construction -- `models--a--b--c` could be `a/b--c` or
 *  `a--b/c` ... Nothing decides anything on this value; it is what a person
 *  reads." Handing a guessed id to the planner would be deciding on it.
 *
 *  Note what does NOT work as a test: re-encoding `repo_id` and comparing it
 *  to `folder`. Both sides split on the FIRST separator, so
 *  `folder_for(repo_from_folder(f)) === f` holds for every folder there is,
 *  ambiguous ones included -- it is a tautology, not a check. The real question
 *  is how many separators the folder has: one (or none, for an org-less id
 *  like `gpt2`) admits exactly one reading; more than one does not. */
export function repoIdIsUnambiguous(m: CachedModel): boolean {
  if (!m.folder.startsWith(PREFIX)) return false
  const rest = m.folder.slice(PREFIX.length)
  return rest.split('--').length <= 2
}

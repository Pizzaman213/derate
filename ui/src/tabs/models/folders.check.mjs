// `folderFor` against the hub's own cache layout.
//
//   node src/tabs/models/folders.check.mjs

import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './folders.ts')
const { check, done } = report()

check(M.folderFor('Qwen/Qwen3-8B') === 'models--Qwen--Qwen3-8B', 'Qwen/Qwen3-8B -> models--Qwen--Qwen3-8B')
check(M.folderFor('  unsloth/Qwen3-8B-GGUF ') === 'models--unsloth--Qwen3-8B-GGUF', 'surrounding space is trimmed')
check(M.folderFor('gpt2') === 'models--gpt2', 'an owner-less id has no separator to replace')

done()

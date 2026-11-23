// A change big enough to shape the codebase needs its design written down BEFORE the code,
// and reviewed by `design-critic` (agents/design-critic.md). The gate cannot judge the note;
// it only refuses a big change that has none: more than NET_LINES net new source lines, or
// growth in a file already past TIGHT of its budget.
import { git, base, changed, untracked, read } from './context.mjs'
import { budgetOf } from './budget.mjs'

const NET_LINES = 150
const TIGHT = 0.7
export const NOTES = '.omp/work/design-notes/'
const isSource = (p) => /^(control_plane\/.*\.py|ui\/src\/.*\.tsx?)$/.test(p) && !/(^|\/)(tests?)\//.test(p) && !p.endsWith('.check.mjs')

const growth = (p) => {
  if (untracked.includes(p)) return read(p).length
  const [added, removed] = git('diff', '--numstat', base, '--', p).split('\t')
  return Number(added || 0) - Number(removed || 0)
}

export const steps = [
  { name: 'a large change has a design note (.omp/work/design-notes/)', run: () => {
    if (changed.some((p) => p.startsWith(NOTES) && p.endsWith('.md'))) return []
    const grown = changed.filter(isSource).map((p) => [p, growth(p)]).filter(([, g]) => g > 0)
    const net = grown.reduce((s, [, g]) => s + g, 0)
    const tight = grown.filter(([p]) => read(p).length > TIGHT * budgetOf(p)).map(([p]) => p)
    const why = [net > NET_LINES && `${net} net new source lines (limit ${NET_LINES})`, tight.length && `growth in files past ${TIGHT * 100}% of budget: ${tight.join(', ')}`].filter(Boolean)
    return why.length ? [`${why.join('; ')}, and no note under ${NOTES}. Write one from .omp/design-note.md, get design-critic's verdict, then implement.`] : []
  } },
]

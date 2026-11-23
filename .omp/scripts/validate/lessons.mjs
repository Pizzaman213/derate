// lessons.md stays honest: a lesson claiming a gate step enforces it must name a step that exists.
import { existsSync, readFileSync } from 'node:fs'

const FILE = '.omp/lessons.md'

export const check = (stepNames) => {
  if (!existsSync(FILE)) return [`${FILE} is missing`]
  const rows = [...readFileSync(FILE, 'utf8').matchAll(/^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|$/gm)]
    .filter(([, lesson]) => lesson !== 'lesson' && !/^-+$/.test(lesson))
  return rows.filter(([, , , by]) => !/^(reviewer:|todo)/.test(by) && !stepNames.includes(by))
    .map(([, lesson, , by]) => `lesson "${lesson.slice(0, 50)}..." is enforced by "${by}", which is not a gate step`)
}

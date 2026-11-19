// Where everything is, relative to the repo root (the cwd omp is started in).
import { join, relative, resolve, isAbsolute, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = fileURLToPath(new URL('.', import.meta.url))
/** The repo root: this file is .omp/extensions/design-pack/lib/paths.ts. */
export const REPO = resolve(here, '..', '..', '..', '..')
export const PACK = join(REPO, '.omp')
export const DESIGN = join(PACK, 'design')
export const TOKENS_JSON = join(DESIGN, 'tokens', 'tokens.json')
export const PAIRS_JSON = join(DESIGN, 'tokens', 'pairs.json')
export const TOKENS_FLAT = join(DESIGN, 'tokens', 'tokens.flat.json')
export const ICONS_JSON = join(DESIGN, 'icons', 'icons.json')
export const ICONS_SVG_DIR = join(DESIGN, 'icons', 'svg')
export const REGISTRY_JSON = join(DESIGN, 'components', 'registry.json')
export const CHANGELOG = join(DESIGN, 'CHANGELOG.md')
export const AUDIT_MD = join(DESIGN, 'AUDIT.md')
export const GRAPH_JSON = join(PACK, 'graph', 'graph.json')
export const UI_SRC = join(REPO, 'ui', 'src')
export const UI_COMPONENTS = join(UI_SRC, 'components')

export const rel = (p: string): string => relative(REPO, p).split(sep).join('/')
export const abs = (p: string, cwd = REPO): string => (isAbsolute(p) ? p : resolve(cwd, p))

export const isUiSource = (p: string): boolean => /^ui\/src\/.*\.(tsx?|css)$/.test(rel(p)) && !/\.check\.mjs$/.test(p)
export const isTokenSource = (p: string): boolean => /^\.omp\/design\/tokens\/(tokens|pairs)\.json$/.test(rel(p))
export const isIconSource = (p: string): boolean => /^\.omp\/design\/icons\/(svg\/.*\.svg|icons\.json)$/.test(rel(p))
export const isRegistrySource = (p: string): boolean => rel(p) === '.omp/design/components/registry.json'
export const isSharedComponent = (p: string): boolean => /^ui\/src\/components\/[A-Z][A-Za-z0-9]*\.tsx$/.test(rel(p))
export const isGraphSource = (p: string): boolean => /^(control_plane|ui\/src|\.omp\/design)\//.test(rel(p))

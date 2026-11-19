// Every chart module registers itself here on import; index.ts imports them all.

import type { AnyChartModule } from './types'

const modules = new Map<string, AnyChartModule>()

/** Register a chart module (called by defineChart). Re-registering an id replaces it. */
export function register(m: AnyChartModule): void {
  modules.set(m.def.id, m)
}
/** The module for a chart id, or undefined. */
export function getModule(id: string): AnyChartModule | undefined {
  return modules.get(id)
}
/** All registered modules, in registration order. */
export function allModules(): AnyChartModule[] {
  return [...modules.values()]
}

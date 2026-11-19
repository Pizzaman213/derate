// Run one of the pack's node scripts from .omp/, through pi.exec so the
// extension never spawns a process of its own.
import type { ExtensionAPI } from '@oh-my-pi/pi-coding-agent'
import { PACK } from './paths.ts'

export type RunResult = { ok: boolean; code: number; out: string }

export async function runScript(pi: ExtensionAPI, script: string, args: string[] = [], opts: { timeout?: number; cwd?: string; signal?: AbortSignal } = {}): Promise<RunResult> {
  const r = await pi.exec('node', [script, ...args], { cwd: opts.cwd ?? PACK, timeout: opts.timeout ?? 600_000, signal: opts.signal })
  const out = `${r.stdout ?? ''}${r.stderr ? (r.stdout ? '\n' : '') + r.stderr : ''}`.trim()
  return { ok: r.code === 0, code: r.code, out }
}

export async function runNpm(pi: ExtensionAPI, scriptName: string, args: string[] = [], opts: { timeout?: number; signal?: AbortSignal } = {}): Promise<RunResult> {
  const r = await pi.exec('npm', ['run', '--silent', scriptName, ...(args.length ? ['--', ...args] : [])], { cwd: PACK, timeout: opts.timeout ?? 1_200_000, signal: opts.signal })
  const out = `${r.stdout ?? ''}${r.stderr ? (r.stdout ? '\n' : '') + r.stderr : ''}`.trim()
  return { ok: r.code === 0, code: r.code, out }
}

/** The lines of a verifier's output that name a failure or a file:line. */
export function failureLines(out: string, max = 40): string[] {
  return out.split('\n').filter((l) => /^FAIL|FAILED|^\S+:\d+:\d+ |Error:|✗/.test(l)).slice(0, max)
}

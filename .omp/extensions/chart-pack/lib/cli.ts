// Every command, tool and hook goes through the pack's one CLI (.omp/charts/scripts/cli.mjs),
// run with pi.exec from .omp/charts, so what the extension says is what the CLI says.
import type { ExtensionAPI } from '@oh-my-pi/pi-coding-agent'
import { CHARTS } from './paths.ts'

export type CliResult = { ok: boolean; code: number; out: string; killed: boolean }

export async function runCli(pi: ExtensionAPI, args: string[], opts: { timeout?: number; signal?: AbortSignal } = {}): Promise<CliResult> {
  const r = await pi.exec('node', ['scripts/cli.mjs', ...args], { cwd: CHARTS, timeout: opts.timeout ?? 600_000, signal: opts.signal })
  const out = `${r.stdout ?? ''}${r.stderr ? (r.stdout ? '\n' : '') + r.stderr : ''}`.trim()
  return { ok: r.code === 0 && !r.killed, code: r.code, out, killed: r.killed }
}

/** Run with --json and parse stdout; a CLI failure with a JSON body still parses. */
export async function runCliJson<T = Record<string, unknown>>(pi: ExtensionAPI, args: string[], opts: { timeout?: number; signal?: AbortSignal } = {}): Promise<{ ok: boolean; json: T | null; out: string }> {
  const r = await pi.exec('node', ['scripts/cli.mjs', ...args, '--json'], { cwd: CHARTS, timeout: opts.timeout ?? 300_000, signal: opts.signal })
  const stdout = r.stdout ?? ''
  const start = stdout.indexOf('{')
  let json: T | null = null
  try { json = start >= 0 ? (JSON.parse(stdout.slice(start)) as T) : null } catch { json = null }
  return { ok: r.code === 0 && !r.killed, json, out: `${stdout}${r.stderr ? `\n${r.stderr}` : ''}`.trim() }
}

/** Split a slash-command argument string, honouring "double" and 'single' quotes. */
export function splitArgs(s: string): string[] {
  const out: string[] = []
  for (const m of s.matchAll(/"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+)/g)) out.push(m[1] !== undefined ? m[1].replace(/\\(.)/g, '$1') : (m[2] ?? m[3]!))
  return out
}

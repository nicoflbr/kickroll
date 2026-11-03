// Pure functions for the status entries: file text in, RunStatus / tab order / tab labels out.
import type { Budget, JevUsage, ModelUsage, RunEntry, RunStatus, TaskStatus } from '../types'

// heartbeat files are rewritten every 5 s; a RUNNING entry without a beat this fresh is stale.
export const STALE_MS = 30_000
// More than this many entries: the rest sit behind one overflow tab.
export const MAX_TABS = 6

function parseBudget(raw: unknown): Budget | undefined {
  const b = raw as { used?: unknown; max?: unknown } | null
  if (!b || typeof b.used !== 'number' || typeof b.max !== 'number') {
    return undefined
  }
  return { used: b.used, max: b.max }
}

// One status file (schema version 1 or 2) -> RunStatus; anything that does not match is dropped.
export function parseRun(text: string): RunStatus | null {
  try {
    const raw = JSON.parse(text)
    if ((raw.version !== 1 && raw.version !== 2) || !Array.isArray(raw.tasks)) return null
    const budgets: Record<string, Budget> = {}
    for (const key of Object.keys(raw.budgets ?? {})) {
      const b = parseBudget(raw.budgets[key])
      if (b) budgets[key] = b
    }
    const usage: Record<string, ModelUsage> = {}
    for (const model of ['haiku', 'sonnet', 'opus']) {
      const u = raw.usage?.[model]
      usage[model] = {
        calls: Number(u?.calls ?? 0),
        tokens: Number(u?.tokens ?? 0),
        seconds: Number(u?.seconds ?? 0),
      }
    }
    // Older status files have no "jev" block: show zeros.
    const jev: JevUsage = {
      calls: Number(raw.jev?.calls ?? 0),
      errors: Number(raw.jev?.errors ?? 0),
      seconds: Number(raw.jev?.seconds ?? 0),
    }
    const tasks: TaskStatus[] = raw.tasks.map((t: Record<string, unknown>) => ({
      id: String(t.id ?? '?'),
      title: String(t.title ?? ''),
      kind: String(t.kind ?? ''),
      status: String(t.status ?? 'PENDING'),
      step: String(t.step ?? 'NONE'),
      attempt: Number(t.attempt ?? 1),
      maxAttempts: Number(t.max_attempts ?? 3),
      model: String(t.model ?? ''),
      dependsOn: Array.isArray(t.depends_on) ? t.depends_on.map(String) : [],
      failedCriteria: Number(t.failed_criteria ?? 0),
    }))
    return {
      runId: String(raw.run_id ?? ''),
      pid: Number(raw.pid ?? 0),
      repo: String(raw.repo ?? ''),
      session: String(raw.session ?? ''),
      status: String(raw.status ?? 'RUNNING'),
      phase: String(raw.phase ?? 'EXECUTE'),
      note: String(raw.note ?? ''),
      updated: String(raw.updated ?? ''),
      started: String(raw.started ?? ''),
      budgets,
      usage,
      jev,
      tasks,
    }
  } catch {
    return null
  }
}

// The process id in a heartbeat file name (pid-1234.beat.json), else undefined.
export function beatPid(name: string): number | undefined {
  const m = /^pid-(\d+)\.beat\.json$/.exec(name)
  return m ? Number(m[1]) : undefined
}

// An entry file (status/<key>.json, not a beat or temp file) -> its key, else undefined.
export function entryKey(name: string): string | undefined {
  if (!name.endsWith('.json') || name.endsWith('.beat.json')) return undefined
  return name.slice(0, -'.json'.length)
}

// Marks a RUNNING entry STALE when its process has no fresh beat. beats: pid -> mtime of its beat file.
export function withLiveness(
  key: string,
  run: RunStatus,
  beats: Map<number, number>,
  now: number,
  staleNote: string,
): RunEntry {
  const beat = beats.get(run.pid)
  const live = run.status === 'RUNNING' && beat !== undefined && now - beat < STALE_MS
  const stale = run.status === 'RUNNING' && !live
  return { ...run, key, live, ...(stale ? { status: 'STALE', note: staleNote } : {}) }
}

// Tab order: live runs first, then by last update, newest first; ties by key.
export function orderEntries(list: RunEntry[]): RunEntry[] {
  return [...list].sort((a, b) => {
    if (a.live !== b.live) return a.live ? -1 : 1
    if (a.updated !== b.updated) return a.updated < b.updated ? 1 : -1
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0
  })
}

// The entry shown: the selected one while it exists, else the first (the newest live run).
export function pick(list: RunEntry[], selected: string): RunEntry | null {
  return list.find(e => e.key === selected) ?? list[0] ?? null
}

// Visible tabs (the selected one always among them) and the entries behind the overflow tab.
export function tabsOf(list: RunEntry[], selected: string): { shown: RunEntry[]; hidden: RunEntry[] } {
  if (list.length <= MAX_TABS) return { shown: list, hidden: [] }
  const current = pick(list, selected)
  let shown = list.slice(0, MAX_TABS - 1)
  if (current && !shown.includes(current)) shown = [...shown.slice(0, -1), current]
  return { shown, hidden: list.filter(e => !shown.includes(e)) }
}

// Last folder of a repository path (either separator).
export function repoName(repo: string): string {
  const parts = repo.split(/[\\/]+/).filter(Boolean)
  return parts[parts.length - 1] ?? ''
}

// Tab label: repository and the short run id, or "Bypass" for a skill bypass.
export function tabLabel(e: RunEntry, bypass: string): string {
  const name = repoName(e.repo) || 'kickroll'
  if (e.status === 'BYPASSED') return `${name} · ${bypass}`
  const short = e.runId.split('-').pop() ?? e.runId
  return `${name} ${short}`
}

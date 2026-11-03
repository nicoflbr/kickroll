import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Budget, Decision, JevUsage, ModelUsage, RunStatus, TaskStatus } from '../types'
import { messages } from './i18n'
import { dashboard } from './svg'

const PANE = 'kickroll'
const COMMAND = 'kickroll-panel'
const POLL_MS = 1500
const KEEP = 200
// heartbeat.json is rewritten every 5 s; this many polls without a new beat (30 s) means the run is dead.
const STALE_POLLS = 20

const PHASES = ['PREFLIGHT', 'BYPASS', 'PLAN', 'APPROVAL', 'EXECUTE', 'DONE']
const STEPS = ['ROUTE', 'WORK', 'L0', 'L1', 'L3', 'COMMIT']
const MODEL_COLOR: Record<string, string> = {
  haiku: '#5DCAA5',
  sonnet: '#85B7EB',
  opus: '#AFA9EC',
}
const RUN_COLOR: Record<string, string> = {
  RUNNING: '#97C459',
  PAUSED: '#EF9F27',
  SUCCESS: '#5DCAA5',
  PARTIAL: '#EF9F27',
  FAILED: '#F09595',
  STALE: '#888780',
}
const TASK_GLYPH: Record<string, string> = {
  PENDING: '○',
  READY: '◌',
  RUNNING: '●',
  ACCEPTED: '✓',
  MERGED: '✔',
  BLOCKED: '■',
  FAILED: '✗',
  SKIPPED: '–',
  UNCLEAR: '?',
}
const TASK_COLOR: Record<string, string> = {
  RUNNING: '#97C459',
  ACCEPTED: '#5DCAA5',
  MERGED: '#5DCAA5',
  BLOCKED: '#EF9F27',
  FAILED: '#F09595',
  UNCLEAR: '#EF9F27',
}
const INK = '#1b1b1b'

// Change the shape tag when the fields of a decision or run change.
const decisions = atom(
  { plugin: 'kickroll-panel', key: 'decisions' } as const,
  [],
  { shape: 'decisions-1' },
)
const run = atom({ plugin: 'kickroll-panel', key: 'run' } as const, null, {
  shape: 'run-4',
})

let lastLogKey = ''
let lastRunRaw = ''
let lastBeatRaw = ''
let beatIdle = 0
let lastStale = false

// One log line -> decision; malformed lines are skipped.
function parseDecision(line: string): Decision | undefined {
  try {
    const raw = JSON.parse(line)
    if (typeof raw.model !== 'string') return undefined
    return {
      ts: String(raw.ts ?? ''),
      model: raw.model,
      task: String(raw.task ?? '-'),
      complexity: Number(raw.complexity ?? 0),
      deep: Number(raw.deep ?? 0),
      confidence: Number(raw.confidence ?? 0),
      latencyMs: Number(raw.latency_ms ?? 0),
      reason: String(raw.reason ?? ''),
    }
  } catch {
    return undefined
  }
}

function parseBudget(raw: unknown): Budget | undefined {
  const b = raw as { used?: unknown; max?: unknown } | null
  if (!b || typeof b.used !== 'number' || typeof b.max !== 'number') {
    return undefined
  }
  return { used: b.used, max: b.max }
}

// status.json -> RunStatus; anything that does not match the schema is dropped.
function parseRun(text: string): RunStatus | null {
  try {
    const raw = JSON.parse(text)
    if (raw.version !== 1 || !Array.isArray(raw.tasks)) return null
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

function beatPidOf(text: string): number {
  try {
    return Number(JSON.parse(text).pid ?? -1)
  } catch {
    return -1
  }
}

// Data directory as in the CLI: KICKROLL_HOME, else ~/.kickroll.
async function dataDir($: EngineInterface): Promise<string> {
  const custom = await $.env.get('KICKROLL_HOME')
  if (custom) return custom.replace(/\\/g, '/')
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? ''
  return `${home.replace(/\\/g, '/')}/.kickroll`
}

// Reads both files and writes the state only on change (redraws the readers).
async function refresh($: EngineInterface) {
  const dir = await dataDir($)
  let log = ''
  try {
    log = await $.fs.read(`${dir}/router_log.jsonl`)
  } catch {
    log = '' // log missing or over 4 MiB: the router part stays empty
  }
  const list = log
    .split('\n')
    .map(parseDecision)
    .filter((d): d is Decision => d !== undefined)
    .slice(-KEEP)
  const last = list[list.length - 1]
  const logKey = last ? `${list.length}:${last.ts}` : ''
  if (logKey !== lastLogKey) {
    lastLogKey = logKey
    await update($, decisions, () => list)
  }

  let raw = ''
  try {
    raw = await $.fs.read(`${dir}/status.json`)
  } catch {
    raw = ''
  }
  let beat = ''
  try {
    beat = await $.fs.read(`${dir}/heartbeat.json`)
  } catch {
    beat = ''
  }
  if (beat !== lastBeatRaw) {
    lastBeatRaw = beat
    beatIdle = 0
  } else {
    beatIdle++
  }
  const beatPid = beatPidOf(beat)

  // RUNNING only counts while the writing process still beats; otherwise the file is a leftover.
  const parsedNow = raw ? parseRun(raw) : null
  const stale =
    parsedNow?.status === 'RUNNING' &&
    (!beat || beatPid !== parsedNow.pid || beatIdle >= STALE_POLLS)
  if (raw !== lastRunRaw || stale !== lastStale) {
    lastRunRaw = raw
    lastStale = stale
    const T = messages(await $.env.get('KICKROLL_LANG'))
    const shown = parsedNow && stale ? { ...parsedNow, status: 'STALE', note: T.stale } : parsedNow
    await update($, run, () => shown)
  }

  const current = stale ? null : parsedNow
  const active = current?.tasks.find(t => t.status === 'RUNNING')
  if (current && current.status === 'RUNNING' && active) {
    $.ui.status(
      `kickroll ${current.runId}: ${active.id} ${active.step} ${active.attempt}/${active.maxAttempts}`,
    )
  } else if (last) {
    $.ui.status(`router: ${last.model} (${last.task})`)
  }
}

export const register: Register = on => {
  let timer: { cancel: () => void } | undefined

  on('session.start', async ($, e, next) => {
    const T = messages(await $.env.get('KICKROLL_LANG'))
    // Open first: a refused command must not hide the pane.
    void $.ui.open({ id: PANE, title: T.paneTitle })
    // `/kickroll` belongs to the kickroll plugin's skill, so the command has its own name.
    try {
      await $.command.register({
        name: COMMAND,
        description: T.commandDescription,
      })
    } catch (err) {
      $.ui.log(`kickroll-panel: /${COMMAND} not registered: ${String(err)}`)
    }
    timer?.cancel()
    timer = $.clock.every(POLL_MS, () => void refresh($))
    await refresh($)

    return next(e)
  })

  on('command.run', { command: COMMAND }, async $ => {
    const T = messages(await $.env.get('KICKROLL_LANG'))
    await $.ui.open({ id: PANE, title: T.paneTitle })

    return { text: T.paneOpened }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const ui = $.ui.resolve(e)
    const { Box, Text } = ui
    const list = await read($, decisions)
    const current = await read($, run)
    const rows = e.viewport?.rows ?? 30
    const lang = await $.env.get('KICKROLL_LANG')
    const T = messages(lang)

    // Desktop: own vector dashboard; other surfaces: the text view below.
    if ('Svg' in ui) {
      const { Svg } = ui
      // Pane width from its columns (measured: about 6 px per cell) minus the margin.
      const pane = e.viewport ? e.viewport.columns * 6 - 24 : 340
      const graphic = dashboard(current, list, pane, lang)
      const recent = list.slice(-6).reverse()

      return (
        <Box flexDirection="column" gap={1}>
          <Svg
            source={graphic.source}
            alt={graphic.alt}
            width={graphic.width}
            height={graphic.height}
          />
          {recent.map(d => (
            <Text mono size="caption" wrap="truncate-end">
              <Text dimColor>{d.ts.slice(11, 16)} </Text>
              <Text color={MODEL_COLOR[d.model]}>{d.model.padEnd(7)}</Text>
              <Text>{`${d.task.slice(0, 11).padEnd(11)} ${d.complexity.toFixed(1)}/${d.deep.toFixed(1)}/${d.confidence.toFixed(1)} `}</Text>
              <Text dimColor>{`${d.latencyMs}ms`}</Text>
            </Text>
          ))}
        </Box>
      )
    }

    const chip = (label: string, bg: string) => (
      <Text backgroundColor={bg} color={INK} bold>
        {` ${label} `}
      </Text>
    )
    const bar = (used: number, max: number, color: string) => {
      const width = 20
      const filled = max > 0 ? Math.min(width, Math.round((used / max) * width)) : 0
      return (
        <Text>
          <Text color={color}>{'█'.repeat(filled)}</Text>
          <Text dimColor>{'░'.repeat(width - filled)}</Text>
        </Text>
      )
    }

    const pipeline = (cur: RunStatus) => {
      const at = PHASES.indexOf(cur.phase)
      return (
        <Box flexDirection="row" gap={1}>
          {PHASES.map((name, i) =>
            i < at ? (
              <Text color="#5DCAA5">{`✓ ${name}`}</Text>
            ) : i === at ? (
              chip(name, RUN_COLOR[cur.status] ?? '#97C459')
            ) : (
              <Text dimColor>{name}</Text>
            ),
          )}
        </Box>
      )
    }

    const stepper = (task: TaskStatus) => {
      const at = STEPS.indexOf(task.step)
      const done = task.status === 'ACCEPTED' || task.status === 'MERGED'
      return (
        <Box flexDirection="row" gap={1}>
          {STEPS.map((name, i) => {
            const isDone = done || (at >= 0 && i < at)
            const isActive = !done && i === at
            const box = (
              <Box
                borderStyle="round"
                borderColor={
                  isActive ? '#97C459' : isDone ? '#5DCAA5' : undefined
                }
                borderDimColor={!isActive && !isDone}
                paddingX={1}
              >
                <Text
                  bold={isActive}
                  color={isActive ? '#97C459' : isDone ? '#5DCAA5' : undefined}
                  dimColor={!isActive && !isDone}
                >
                  {`${isDone ? '✓' : isActive ? '●' : '○'} ${T.steps[i]}`}
                </Text>
              </Box>
            )
            return box
          })}
        </Box>
      )
    }

    const taskRow = (t: TaskStatus) => (
      <Box flexDirection="row" gap={1}>
        <Text color={TASK_COLOR[t.status]} dimColor={t.status === 'PENDING'}>
          {TASK_GLYPH[t.status] ?? '?'}
        </Text>
        <Text bold={t.status === 'RUNNING'}>{t.id.padEnd(4)}</Text>
        <Text dimColor={t.status !== 'RUNNING'}>{t.title.slice(0, 34).padEnd(34)}</Text>
        {t.model ? (
          <Text color={MODEL_COLOR[t.model]}>{t.model.padEnd(6)}</Text>
        ) : (
          <Text dimColor>{'-'.padEnd(6)}</Text>
        )}
        <Text dimColor>
          {t.status === 'RUNNING'
            ? `${t.step.padEnd(6)} ${'↻'.repeat(Math.max(0, t.attempt - 1))}${t.attempt}/${t.maxAttempts}`
            : t.status.toLowerCase()}
        </Text>
      </Box>
    )

    const count = (m: string) => list.filter(d => d.model === m).length
    const avg = list.length
      ? Math.round(list.reduce((s, d) => s + d.latencyMs, 0) / list.length)
      : 0
    const routerRoom = Math.max(3, Math.min(8, rows - 26))

    const active = current?.tasks.find(t => t.status === 'RUNNING')
    const focus = active ?? current?.tasks[current.tasks.length - 1]

    return (
      <Box flexDirection="column" gap={1}>
        {current ? (
          <Box flexDirection="column" gap={1}>
            <Box flexDirection="row" gap={2}>
              <Text bold>{T.title}</Text>
              <Text dimColor>{T.run(current.runId)}</Text>
              {chip(current.status, RUN_COLOR[current.status] ?? '#97C459')}
              {current.note ? <Text dimColor>{current.note}</Text> : null}
            </Box>
            {pipeline(current)}
            {focus ? (
              <Box flexDirection="column" gap={1}>
                <Box flexDirection="row" gap={2}>
                  <Text bold>{`${focus.id} ${focus.title}`.slice(0, 50)}</Text>
                  {focus.model ? chip(focus.model, MODEL_COLOR[focus.model] ?? '#888780') : null}
                  <Text dimColor>
                    {T.attempt(focus.attempt, focus.maxAttempts)}
                    {focus.failedCriteria > 0 ? ` · ${T.criteriaOpen(focus.failedCriteria)}` : ''}
                  </Text>
                </Box>
                {stepper(focus)}
              </Box>
            ) : null}
            <Box flexDirection="column">
              {current.tasks.map(taskRow)}
            </Box>
            <Text dimColor={current.jev.calls === 0}>
              {`jev  ${T.jevRow(current.jev.calls, current.jev.errors, `${current.jev.seconds} s`)}`}
            </Text>
            <Box flexDirection="column">
              {Object.keys(current.budgets).map(key => {
                const b = current.budgets[key]
                const hot = b.max > 0 && b.used / b.max >= 0.8
                return (
                  <Box flexDirection="row" gap={1}>
                    <Text dimColor>{(T.budgets[key] ?? key).padEnd(8)}</Text>
                    {bar(b.used, b.max, hot ? '#EF9F27' : '#85B7EB')}
                    <Text dimColor>{`${b.used}/${b.max}`}</Text>
                  </Box>
                )
              })}
            </Box>
          </Box>
        ) : (
          <Box flexDirection="column">
            <Text bold>{T.title}</Text>
            <Text dimColor>{T.noRun}</Text>
          </Box>
        )}

        <Box flexDirection="column">
          <Text bold>
            {`${T.router(list.length)} · haiku ${count('haiku')} · sonnet ${count('sonnet')} · opus ${count('opus')} · Ø ${avg} ms`}
          </Text>
          {list.length === 0 && (
            <Text dimColor>{T.noDecisions}</Text>
          )}
          {list
            .slice(-routerRoom)
            .reverse()
            .map(d => (
              <Text>
                <Text dimColor>{d.ts.slice(11, 19)} </Text>
                <Text color={MODEL_COLOR[d.model]} bold>
                  {d.model.padEnd(7)}
                </Text>
                <Text>
                  {d.task.padEnd(16)} cx {d.complexity.toFixed(2)} deep{' '}
                  {d.deep.toFixed(2)} conf {d.confidence.toFixed(2)}{' '}
                </Text>
                <Text dimColor>{d.latencyMs} ms</Text>
              </Text>
            ))}
        </Box>
      </Box>
    )
  })
}

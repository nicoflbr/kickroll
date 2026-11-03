import { expect, test } from 'claude-code/testing'

import { orderEntries, parseRun, pick, tabLabel, tabsOf, withLiveness } from './entries'

const DIR = '/data'
// the kit hands hooks resolved paths (C:\data\status on Windows)
const norm = (path: string) => path.replace(/\\/g, '/').replace(/^[A-Za-z]:/, '')

function status(runId: string, over: Record<string, unknown> = {}): string {
  return JSON.stringify({
    version: 2,
    run_id: runId,
    repo: `C:\\repos\\${runId.startsWith('a') ? 'alpha' : 'beta'}`,
    session: 'local_s1',
    pid: 0,
    status: 'RUNNING',
    phase: 'EXECUTE',
    note: '',
    updated: '2026-11-03T20:00:00+00:00',
    started: '2026-11-03T19:50:00+00:00',
    budgets: { calls: { used: 1, max: 25 } },
    usage: {},
    jev: { calls: 0, errors: 0, seconds: 0 },
    tasks: [
      {
        id: 'T1',
        title: `task of ${runId}`,
        kind: 'code',
        status: 'RUNNING',
        step: 'WORK',
        attempt: 1,
        max_attempts: 3,
        model: 'sonnet',
        depends_on: [],
        failed_criteria: 0,
      },
    ],
    ...over,
  })
}

const PANE = {
  plugin: 'kickroll-panel',
  component: 'Pane',
  requestId: 'kickroll',
  props: {
    title: 'kickroll',
    isFocused: false,
    bodyColumns: 80,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

test('parseRun reads version 1 and 2 and drops other files', () => {
  expect(parseRun(status('a-1'))?.repo).toBe('C:\\repos\\alpha')
  const v1 = JSON.parse(status('a-1'))
  v1.version = 1
  delete v1.repo
  delete v1.session
  expect(parseRun(JSON.stringify(v1))?.repo).toBe('')
  expect(parseRun('{"version": 3, "tasks": []}')).toBe(null)
  expect(parseRun('not json')).toBe(null)
})

test('liveness, order, pick and tab labels', () => {
  const now = 1_000_000
  const beats = new Map([[11, now - 1000], [12, now - 60_000]])
  const live = withLiveness('a-1', { ...parseRun(status('a-1'))!, pid: 11 }, beats, now, 'stale')
  const dead = withLiveness('b-2', { ...parseRun(status('b-2'))!, pid: 12, updated: '2026-11-03T21:00:00+00:00' }, beats, now, 'stale')
  const done = withLiveness('b-3', parseRun(status('b-3', { status: 'SUCCESS', updated: '2026-11-03T22:00:00+00:00' }))!, beats, now, 'stale')
  expect([live.live, live.status]).toEqual([true, 'RUNNING'])
  expect([dead.live, dead.status, dead.note]).toEqual([false, 'STALE', 'stale'])
  const order = orderEntries([done, dead, live]).map(e => e.key)
  expect(order).toEqual(['a-1', 'b-3', 'b-2'])
  expect(pick(orderEntries([done, live]), 'gone')?.key).toBe('a-1')
  expect(pick(orderEntries([done, live]), 'b-3')?.key).toBe('b-3')
  expect(tabLabel(live, 'Bypass')).toBe('alpha 1')
  const bypass = withLiveness('bypass-x', parseRun(status('b-9', { status: 'BYPASSED' }))!, beats, now, 's')
  expect(tabLabel(bypass, 'Bypass')).toBe('beta · Bypass')
})

test('more than six entries: overflow keeps the selected tab visible', () => {
  const list = Array.from({ length: 9 }, (_, i) =>
    withLiveness(`r${i}`, parseRun(status(`r-${i}`, { status: 'SUCCESS' }))!, new Map(), 0, ''),
  )
  const { shown, hidden } = tabsOf(list, 'r8')
  // five tabs and the overflow tab
  expect(shown.length).toBe(5)
  expect(hidden.length).toBe(4)
  expect(shown.map(e => e.key)).toContain('r8')
})

test('one tab per status entry; pressing a tab switches the run shown', async ($, on) => {
  const now = Date.now()
  const files: Record<string, string> = {
    [`${DIR}/status/a-1.json`]: status('a-1', { pid: 11 }),
    [`${DIR}/status/b-2.json`]: status('b-2', { pid: 12, status: 'PARTIAL', phase: 'DONE' }),
  }
  on('env.get', (_, e) => ({ value: e.name === 'KICKROLL_HOME' ? DIR : undefined }))
  on('fs.list', (_, e) => {
    if (norm(e.path) !== `${DIR}/status`) throw new Error('ENOENT')
    return {
      value: [
        { name: 'a-1.json', kind: 'file', size: 1, mtimeMs: now - 2000, isLink: false },
        { name: 'b-2.json', kind: 'file', size: 1, mtimeMs: now - 1000, isLink: false },
        { name: 'pid-11.beat.json', kind: 'file', size: 1, mtimeMs: now, isLink: false },
        { name: 'a-1.json.11.1.tmp', kind: 'file', size: 1, mtimeMs: now, isLink: false },
      ],
    }
  })
  on('fs.read', (_, e) => {
    const text = files[norm(e.path)]
    if (text === undefined) throw new Error('ENOENT')
    return { value: text }
  })
  on('command.register', () => ({ value: { command: 'kickroll-panel' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ key: 'tab:a-1' })).toBeDefined()
    expect(await ui.find({ key: 'tab:b-2' })).toBeDefined()
    await ui.unmount()
  }

  // The desktop draws the run as one Svg: its source names the run shown; the live run comes first.
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const shown = async () => JSON.stringify(await ui.drawn())
  expect(await shown()).toContain('Run a-1')
  expect(await shown()).not.toContain('Run b-2')
  await ui.press({ key: 'tab:b-2' })
  expect(await shown()).toContain('Run b-2')
  expect(await shown()).not.toContain('Run a-1')
  expect((await ui.find({ key: 'tab:b-2' })) !== undefined).toBe(true)
  await ui.unmount()
})

test('a single entry draws no tab bar; no status folder falls back to status.json', async ($, on) => {
  on('env.get', (_, e) => ({ value: e.name === 'KICKROLL_HOME' ? DIR : undefined }))
  on('fs.list', () => {
    throw new Error('ENOENT')
  })
  on('fs.read', (_, e) => {
    if (norm(e.path) === `${DIR}/status.json`) {
      const v1 = JSON.parse(status('old-1', { status: 'SUCCESS', phase: 'DONE' }))
      v1.version = 1
      delete v1.repo
      delete v1.session
      return { value: JSON.stringify(v1) }
    }
    throw new Error('ENOENT')
  })
  on('command.register', () => ({ value: { command: 'kickroll-panel' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ key: 'tab:status' })).toBeUndefined()
  expect(JSON.stringify(await ui.drawn())).toContain('Run old-1')
  await ui.unmount()
})

import type { Decision, RunStatus, TaskStatus } from '../types'
import { messages } from './i18n'
import type { Messages } from './i18n'

// Reine Zeichenfunktionen: Daten rein, SVG-Text raus. Hochformat-Karte mit eigener
// dunkler Flaeche, damit sie in hellem und dunklem Theme gleich lesbar ist. Sie wird
// 1:1 (ohne Skalierung) gezeichnet, damit Schrift und Linien scharf bleiben.

const BG = '#1c1c1b'
const CARD = '#262624'
const CARD_HI = '#2f2f2c'
const LINE = '#3a3a37'
const MUTE = '#5f5e5a'
const TEXT = '#f1efe8'
const DIM = '#9c9a92'
const INK = '#1b1b1b'
const GREEN = '#97C459'
const TEAL = '#5DCAA5'
const AMBER = '#EF9F27'
const RED = '#F09595'
const BLUE = '#85B7EB'
const MODEL: Record<string, string> = {
  haiku: '#5DCAA5',
  sonnet: '#85B7EB',
  opus: '#AFA9EC',
}
const RUN: Record<string, string> = {
  RUNNING: GREEN,
  PAUSED: AMBER,
  SUCCESS: TEAL,
  PARTIAL: AMBER,
  FAILED: RED,
  STALE: MUTE,
  BYPASSED: BLUE,
}
const TASK: Record<string, string> = {
  RUNNING: GREEN,
  ACCEPTED: TEAL,
  MERGED: TEAL,
  BLOCKED: AMBER,
  UNCLEAR: AMBER,
  FAILED: RED,
  READY: BLUE,
}
const PHASES = ['PREFLIGHT', 'BYPASS', 'PLAN', 'APPROVAL', 'EXECUTE', 'DONE']
const STEPS = ['ROUTE', 'WORK', 'L0', 'L1', 'L3', 'COMMIT']
let T: Messages = messages(undefined) // active language, set by dashboard()
const BUDGET_KEYS = ['calls', 'opus', 'planner', 'minutes']
const FONT = "font-family=\"'Segoe UI', system-ui, -apple-system, sans-serif\""
const PAD = 20

// Status timestamps are UTC ISO strings; show them in the viewer's local time.
export function clock(ts: string, seconds: boolean): string {
  const t = new Date(ts)
  if (isNaN(t.getTime())) return ts.slice(11, seconds ? 19 : 16)
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(t.getHours())}:${pad(t.getMinutes())}`
  return seconds ? `${hm}:${pad(t.getSeconds())}` : hm
}

// A dead run (STALE) or a task the chat handled itself (BYPASSED): nothing live to show, only one summary line.
export function idleRun(run: RunStatus | null): boolean {
  return !!run && (run.status === 'STALE' || run.status === 'BYPASSED')
}

export function idleLine(run: RunStatus, M: Messages): string {
  if (run.status === 'BYPASSED') return run.note
  return M.staleLine(run.runId, M.phases[PHASES.indexOf(run.phase)] ?? run.phase)
}

function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function clip(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, Math.max(1, max - 1))}…` : s
}

function text(
  x: number,
  y: number,
  s: string,
  o: { size?: number; fill?: string; weight?: number; anchor?: string } = {},
): string {
  const { size = 12, fill = TEXT, weight = 400, anchor = 'start' } = o
  return `<text x="${x}" y="${y}" font-size="${size}" fill="${fill}" font-weight="${weight}" text-anchor="${anchor}" ${FONT}>${esc(s)}</text>`
}

function pill(x: number, y: number, w: number, label: string, fill: string): string {
  return (
    `<rect x="${x}" y="${y}" width="${w}" height="20" rx="10" fill="${fill}"/>` +
    text(x + w / 2, y + 14, label, { size: 11, fill: INK, weight: 600, anchor: 'middle' })
  )
}

function pulse(cx: number, cy: number, r: number, color: string): string {
  return `<circle cx="${cx}" cy="${cy}" r="${r + 5}" fill="none" stroke="${color}" stroke-width="2" stroke-opacity="0.35"/>`
}

function rule(w: number, y: number): string {
  return `<line x1="${PAD}" y1="${y + 0.5}" x2="${w - PAD}" y2="${y + 0.5}" stroke="${LINE}"/>`
}

function pipeline(w: number, run: RunStatus, y: number): string {
  const at = PHASES.indexOf(run.phase)
  const color = RUN[run.status] ?? GREEN
  const x = (i: number) => PAD + 22 + (i * (w - 2 * PAD - 44)) / (PHASES.length - 1)
  let out = ''
  for (let i = 0; i < PHASES.length - 1; i++) {
    out += `<line x1="${x(i)}" y1="${y}" x2="${x(i + 1)}" y2="${y}" stroke="${i < at ? TEAL : LINE}" stroke-width="3" stroke-linecap="round"/>`
  }
  PHASES.forEach((_, i) => {
    const cx = x(i)
    if (i < at || (i === at && run.status === 'SUCCESS')) {
      out += `<circle cx="${cx}" cy="${y}" r="9" fill="${TEAL}"/>`
      out += `<path d="M${cx - 4} ${y} l3 3 l5 -6" fill="none" stroke="${INK}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`
    } else if (i === at) {
      out += `<circle cx="${cx}" cy="${y}" r="9" fill="${color}"/>`
      if (run.status === 'RUNNING') out += pulse(cx, y, 9, color)
    } else {
      out += `<circle cx="${cx}" cy="${y}" r="8" fill="${BG}" stroke="${MUTE}" stroke-width="2"/>`
    }
    out += text(cx, y + 28, T.phases[i], {
      size: 11,
      fill: i === at ? TEXT : DIM,
      weight: i === at ? 600 : 400,
      anchor: 'middle',
    })
  })
  return out
}

const ROW = 44

// Senkrechter Stepper; der Ruecksprung-Pfeil laeuft rechts vom Pruefschritt zurueck zum Routing.
function loop(w: number, task: TaskStatus, y: number): string {
  const at = STEPS.indexOf(task.step)
  const finished = task.status === 'ACCEPTED' || task.status === 'MERGED'
  const cardW = Math.round(w - 2 * PAD - 28)
  const cx = PAD + 16
  const rowY = (i: number) => y + i * ROW
  const retry = task.attempt > 1
  const rc = retry ? AMBER : MUTE
  let out = ''

  // Rueckweg zuerst zeichnen, damit er unter den Karten liegt.
  const edge = PAD + cardW + 4
  const bottom = rowY(3) + 18
  const top = rowY(0) + 18
  out += `<path d="M${edge} ${bottom} C${edge + 30} ${bottom} ${edge + 30} ${top} ${edge + 4} ${top}" fill="none" stroke="${rc}" stroke-width="1.6" stroke-dasharray="5 4" stroke-linecap="round"/>`
  out += `<path d="M${edge + 10} ${top - 5} L${edge + 3} ${top} L${edge + 10} ${top + 5}" fill="none" stroke="${rc}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`

  STEPS.forEach((_, i) => {
    const ry = rowY(i)
    const done = finished || (at >= 0 && i < at)
    const active = !finished && i === at
    const color = active ? GREEN : done ? TEAL : DIM
    if (i < STEPS.length - 1) {
      out += `<line x1="${cx}" y1="${ry + 18 + 11}" x2="${cx}" y2="${ry + ROW + 18 - 11}" stroke="${done ? TEAL : MUTE}" stroke-width="2"${done ? '' : ' stroke-dasharray="3 3"'}/>`
    }
    out += `<rect x="${PAD + 36}" y="${ry}" width="${cardW - 32}" height="36" rx="9" fill="${active ? '#2d3a1a' : done ? '#1f3a32' : CARD}" stroke="${active ? GREEN : done ? TEAL : LINE}" stroke-width="${active ? 2 : 1}">`
    out += '</rect>'
    out += `<circle cx="${cx}" cy="${ry + 18}" r="11" fill="${done ? TEAL : active ? GREEN : BG}" stroke="${done || active ? 'none' : MUTE}" stroke-width="2"/>`
    if (done) out += `<path d="M${cx - 5} ${ry + 18} l3.5 3.5 l6 -7" fill="none" stroke="${INK}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`
    else if (active) out += pulse(cx, ry + 18, 11, GREEN) + `<circle cx="${cx}" cy="${ry + 18}" r="4" fill="${INK}"/>`
    else out += text(cx, ry + 22, String(i + 1), { size: 11, fill: DIM, anchor: 'middle' })
    out += text(PAD + 48, ry + 15, T.steps[i], { size: 13, fill: active ? GREEN : done ? TEAL : TEXT, weight: active ? 600 : 500 })
    out += text(PAD + 48, ry + 29, T.hints[i], { size: 11, fill: DIM })
  })

  out += text(w / 2, y + 6 * ROW + 8, retry ? T.retry(task.attempt, task.maxAttempts) : T.retryIdle, {
    size: 11,
    fill: retry ? AMBER : DIM,
    anchor: 'middle',
  })
  return out
}

const TASK_ROW = 56

function taskRow(w: number, t: TaskStatus, y: number): string {
  const running = t.status === 'RUNNING'
  const color = TASK[t.status] ?? MUTE
  const cardW = w - 2 * PAD + 8
  let out = `<rect x="${PAD - 4}" y="${y}" width="${cardW}" height="${TASK_ROW - 6}" rx="10" fill="${running ? CARD_HI : CARD}"/>`
  if (running) out += `<rect x="${PAD - 4}" y="${y}" width="4" height="${TASK_ROW - 6}" rx="2" fill="${GREEN}"/>`
  out += `<circle cx="${PAD + 14}" cy="${y + 18}" r="6" fill="${t.status === 'PENDING' ? 'none' : color}" stroke="${color}" stroke-width="2"/>`
  if (running) out += pulse(PAD + 14, y + 18, 6, GREEN)
  out += text(PAD + 30, y + 22, t.id, { size: 13, weight: 600 })
  out += text(PAD + 58, y + 22, clip(t.title, Math.floor((w - 2 * PAD - 58) / 6.6)), {
    size: 13,
    fill: running ? TEXT : DIM,
  })

  // Zweite Zeile: Modell, dann Schrittpunkte (laufend) bzw. Status.
  let x = PAD + 30
  if (t.model) {
    out += pill(x, y + 28, 58, t.model, MODEL[t.model] ?? '#888780')
    x += 68
  }
  if (running) {
    const at = STEPS.indexOf(t.step)
    STEPS.forEach((_, i) => {
      const fill = at >= 0 && i < at ? TEAL : i === at ? GREEN : LINE
      out += `<circle cx="${x + i * 14 + 4}" cy="${y + 38}" r="4.5" fill="${fill}"/>`
    })
    out += text(w - PAD - 6, y + 42, T.attempt(t.attempt, t.maxAttempts), { size: 11, fill: t.attempt > 1 ? AMBER : DIM, anchor: 'end' })
  } else {
    out += text(w - PAD - 6, y + 42, t.status.toLowerCase(), { size: 11, fill: color, anchor: 'end' })
  }
  return out
}

function budgets(w: number, run: RunStatus, y: number): { svg: string; height: number } {
  let out = ''
  let n = 0
  const track = w - 2 * PAD - 56 - 56
  for (const key of BUDGET_KEYS) {
    const label = T.budgets[key] ?? key
    const b = run.budgets[key]
    if (!b) continue
    const by = y + n * 26
    const ratio = b.max > 0 ? Math.min(1, b.used / b.max) : 0
    const color = ratio >= 1 ? RED : ratio >= 0.8 ? AMBER : BLUE
    out += text(PAD, by + 10, label, { size: 12, fill: DIM })
    out += `<rect x="${PAD + 56}" y="${by + 2}" width="${track}" height="8" rx="4" fill="${LINE}"/>`
    if (ratio > 0) out += `<rect x="${PAD + 56}" y="${by + 2}" width="${Math.max(8, track * ratio)}" height="8" rx="4" fill="${color}"/>`
    out += text(w - PAD, by + 10, `${b.used}/${b.max}`, { size: 12, fill: TEXT, anchor: 'end' })
    n++
  }
  return { svg: out, height: n * 26 }
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`
  return String(n)
}

function duration(seconds: number): string {
  return seconds >= 90 ? `${Math.round(seconds / 60)} min` : `${Math.round(seconds)} s`
}

// Actual usage of this run per model (from the CLI results), not the router's recommendations.
function usageBlock(w: number, run: RunStatus, y: number): { svg: string; height: number } {
  const models = ['haiku', 'sonnet', 'opus']
  const calls = models.map(m => run.usage?.[m]?.calls ?? 0)
  const total = calls.reduce((a, b) => a + b, 0)
  const width = w - 2 * PAD
  let out = text(PAD, y + 10, T.usage, { size: 12, fill: DIM })
  let x = PAD
  models.forEach((m, i) => {
    if (total === 0 || calls[i] === 0) return
    const seg = (calls[i] / total) * width
    out += `<rect x="${x}" y="${y + 18}" width="${Math.max(2, seg - 2)}" height="10" rx="5" fill="${MODEL[m]}"/>`
    x += seg
  })
  if (total === 0) out += `<rect x="${PAD}" y="${y + 18}" width="${width}" height="10" rx="5" fill="${LINE}"/>`
  models.forEach((m, i) => {
    const u = run.usage?.[m] ?? { calls: 0, tokens: 0, seconds: 0 }
    const ry = y + 50 + i * 22
    out += `<circle cx="${PAD + 5}" cy="${ry - 4}" r="4" fill="${MODEL[m]}"/>`
    out += text(PAD + 15, ry, m, { size: 12, weight: 600 })
    out += text(w - PAD, ry, T.usageRow(u.calls, compact(u.tokens), duration(u.seconds)), { size: 12, fill: u.calls > 0 ? TEXT : DIM, anchor: 'end' })
  })
  // Jev requests (routing, L2 check): not a Claude model, so no bar segment, only a row.
  const j = run.jev ?? { calls: 0, errors: 0, seconds: 0 }
  const jy = y + 50 + models.length * 22
  out += `<circle cx="${PAD + 5}" cy="${jy - 4}" r="4" fill="${j.errors > 0 ? '#EF9F27' : '#F0997B'}"/>`
  out += text(PAD + 15, jy, 'jev', { size: 12, weight: 600 })
  out += text(w - PAD, jy, T.jevRow(j.calls, j.errors, duration(j.seconds)), { size: 12, fill: j.calls > 0 ? TEXT : DIM, anchor: 'end' })
  return { svg: out, height: 50 + (models.length + 1) * 22 }
}

function routerBar(w: number, list: Decision[], y: number): string {
  const total = list.length
  const width = w - 2 * PAD
  const models = ['haiku', 'sonnet', 'opus']
  let out = text(PAD, y + 10, T.router(total), { size: 12, fill: DIM })
  let x = PAD
  for (const m of models) {
    const c = list.filter(d => d.model === m).length
    if (total === 0 || c === 0) continue
    const seg = (c / total) * width
    out += `<rect x="${x}" y="${y + 18}" width="${Math.max(2, seg - 2)}" height="10" rx="5" fill="${MODEL[m]}"/>`
    x += seg
  }
  if (total === 0) out += `<rect x="${PAD}" y="${y + 18}" width="${width}" height="10" rx="5" fill="${LINE}"/>`

  // Legend: one row per model with its count and share, independent of segment width.
  models.forEach((m, i) => {
    const c = list.filter(d => d.model === m).length
    const pct = total > 0 ? Math.round((c / total) * 100) : 0
    const ry = y + 50 + i * 22
    out += `<circle cx="${PAD + 5}" cy="${ry - 4}" r="4" fill="${MODEL[m]}"/>`
    out += text(PAD + 15, ry, m, { size: 12, weight: 600 })
    out += text(w - PAD, ry, `${c} · ${pct}%`, { size: 12, fill: c > 0 ? TEXT : DIM, anchor: 'end' })
  })
  return out
}

export function dashboard(
  run: RunStatus | null,
  list: Decision[],
  pane: number,
  lang?: string,
): { source: string; alt: string; width: number; height: number } {
  // Hochformat: bis 440 px, nie breiter als das Pane (sonst wird skaliert und unscharf).
  T = messages(lang)
  const w = Math.max(300, Math.min(440, Math.floor(pane / 2) * 2))
  let y = 0
  let body = ''

  const color = run ? (RUN[run.status] ?? GREEN) : MUTE
  body += text(PAD, 36, T.title, { size: 19, weight: 600 })
  const idle = idleRun(run)
  if (run && idle) {
    const label = run.status
    const pw = 14 + label.length * 8
    body += pill(w - PAD - pw, 20, pw, label, color)
    body += text(PAD, 60, clip(idleLine(run, T), Math.floor((w - 2 * PAD) / 6.4)), { size: 12, fill: TEXT })
    body += text(PAD, 78, T.at(clock(run.updated, false)), { size: 11, fill: DIM })
    y = 96
    body += rule(w, y)
    y += 16
  } else if (run) {
    const label = run.status
    const pw = 14 + label.length * 8
    body += pill(w - PAD - pw, 20, pw, label, color)
    body += text(PAD, 56, clip(`${T.run(run.runId)}${run.note ? ` · ${run.note}` : ''}`, Math.floor((w - 2 * PAD) / 6)), {
      size: 11,
      fill: DIM,
    })
    body += pipeline(w, run, 98)
    y = 154
  } else {
    body += text(PAD, 60, clip(T.noRun, Math.floor((w - 2 * PAD) / 6.4)), {
      size: 12,
      fill: DIM,
    })
    y = 90
  }

  if (run && !idle) {
    const active = run.tasks.find(t => t.status === 'RUNNING')
    const focus = active ?? run.tasks[run.tasks.length - 1]
    if (focus) {
      body += rule(w, y)
      body += text(PAD, y + 30, clip(`${focus.id}  ${focus.title}`, Math.floor((w - 2 * PAD - 70) / 7.8)), {
        size: 15,
        weight: 600,
      })
      if (focus.model) body += pill(w - PAD - 58, y + 14, 58, focus.model, MODEL[focus.model] ?? '#888780')
      const meta = `${T.attempt(focus.attempt, focus.maxAttempts)}${focus.failedCriteria > 0 ? ` · ${T.criteriaOpen(focus.failedCriteria)}` : ''}`
      body += text(PAD, y + 50, meta, { size: 12, fill: focus.attempt > 1 ? AMBER : DIM })
      body += loop(w, focus, y + 68)
      y += 68 + 6 * ROW + 26
    }
    body += rule(w, y)
    y += 16
    for (const t of run.tasks) {
      body += taskRow(w, t, y)
      y += TASK_ROW
    }
    y += 6
    const b = budgets(w, run, y)
    body += b.svg
    y += b.height + 10
    body += rule(w, y)
    y += 16
    const u = usageBlock(w, run, y)
    body += u.svg
    y += u.height + 4
    body += rule(w, y)
    y += 16
  }
  body += routerBar(w, list, y)
  y += 50 + 3 * 22 + 4

  const source =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${y}" viewBox="0 0 ${w} ${y}">` +
    `<rect width="${w}" height="${y}" rx="14" fill="${BG}"/>${body}</svg>`
  const summary = run && idle
    ? `${run.status}: ${idleLine(run, T)}`
    : run
    ? `${T.run(run.runId)}, ${run.status}, ${run.phase}, ${run.tasks.length} tasks`
    : T.noRun
  return { source, alt: `${T.title}: ${summary}`, width: w, height: y }
}

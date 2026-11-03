// UI texts of the panel. English is the default; set KICKROLL_LANG=de for German.

export type Messages = {
  title: string
  phases: string[]
  steps: string[]
  hints: string[]
  budgets: Record<string, string>
  run: (id: string) => string
  noRun: string
  attempt: (n: number, max: number) => string
  criteriaOpen: (n: number) => string
  retry: (n: number, max: number) => string
  retryIdle: string
  router: (n: number) => string
  usage: string
  usageRow: (calls: number, tokens: string, seconds: string) => string
  paneTitle: string
  commandDescription: string
  paneOpened: string
  noDecisions: string
}

const en: Messages = {
  title: 'kickroll',
  phases: ['Check', 'Bypass', 'Plan', 'Approval', 'Run', 'Done'],
  steps: ['Routing', 'Worker', 'Checks', 'Tests', 'Review', 'Commit'],
  hints: [
    'Picks the tier',
    'claude -p works',
    'Paths, hashes',
    'Tests run',
    'Another tier reviews',
    'Onto the integration branch',
  ],
  budgets: { calls: 'Calls', opus: 'Opus', planner: 'Planner', minutes: 'Minutes' },
  run: id => `Run ${id}`,
  noRun: 'No run active. Start one with the kickroll CLI.',
  attempt: (n, max) => `Attempt ${n}/${max}`,
  criteriaOpen: n => `${n} ${n === 1 ? 'criterion' : 'criteria'} open`,
  retry: (n, max) => `↻ Retry ${n}/${max}: feedback, then a higher tier`,
  retryIdle: '↻ on failure: retry with feedback',
  router: n => `Router choices · ${n} decisions`,
  usage: 'Actual usage in this run',
  usageRow: (calls, tokens, seconds) => `${calls} ${calls === 1 ? 'call' : 'calls'} · ${tokens} tokens · ${seconds}`,
  paneTitle: 'kickroll',
  commandDescription: 'Show the kickroll loop and router decisions in a pane',
  paneOpened: 'kickroll pane opened.',
  noDecisions: 'No entries in router_log.jsonl yet.',
}

const de: Messages = {
  title: 'kickroll',
  phases: ['Check', 'Bypass', 'Plan', 'Freigabe', 'Lauf', 'Ende'],
  steps: ['Routing', 'Worker', 'Prüfung', 'Tests', 'Review', 'Commit'],
  hints: [
    'Wählt die Stufe',
    'claude -p arbeitet',
    'Pfade, Hashes',
    'Tests laufen',
    'Andere Stufe prüft',
    'Auf den Integrationsbranch',
  ],
  budgets: { calls: 'Aufrufe', opus: 'Opus', planner: 'Planer', minutes: 'Minuten' },
  run: id => `Lauf ${id}`,
  noRun: 'Kein Lauf aktiv. Starte einen mit der kickroll-CLI.',
  attempt: (n, max) => `Versuch ${n}/${max}`,
  criteriaOpen: n => `${n} Kriterium offen`,
  retry: (n, max) => `↻ Retry ${n}/${max}: Feedback, dann höhere Stufe`,
  retryIdle: '↻ bei Fehler: Retry mit Feedback',
  router: n => `Router-Wahl · ${n} Entscheidungen`,
  usage: 'Tatsächliche Nutzung in diesem Lauf',
  usageRow: (calls, tokens, seconds) => `${calls} ${calls === 1 ? 'Aufruf' : 'Aufrufe'} · ${tokens} Tokens · ${seconds}`,
  paneTitle: 'kickroll',
  commandDescription: 'kickroll-Loop und Router-Entscheidungen in einem Pane zeigen',
  paneOpened: 'kickroll-Pane geöffnet.',
  noDecisions: 'Noch keine Einträge in router_log.jsonl.',
}

export function messages(lang: string | undefined): Messages {
  return lang && lang.toLowerCase().startsWith('de') ? de : en
}

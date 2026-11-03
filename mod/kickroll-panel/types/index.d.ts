export type Decision = {
  ts: string
  model: string
  task: string
  complexity: number
  deep: number
  confidence: number
  latencyMs: number
  reason: string
}

export type Budget = { used: number; max: number }

export type TaskStatus = {
  id: string
  title: string
  kind: string
  status: string
  step: string
  attempt: number
  maxAttempts: number
  model: string
  dependsOn: string[]
  failedCriteria: number
}

export type ModelUsage = { calls: number; tokens: number; seconds: number }

export type JevUsage = { calls: number; errors: number; seconds: number }

export type RunStatus = {
  runId: string
  pid: number // writer process; 0 in status files before 0.1.2
  status: string // STALE when RUNNING but no live heartbeat
  phase: string
  note: string
  updated: string
  started: string
  budgets: Record<string, Budget>
  usage: Record<string, ModelUsage>
  jev: JevUsage
  tasks: TaskStatus[]
}

declare module 'claude-code' {
  interface PluginState {
    'kickroll-panel': {
      decisions: Shaped<Decision[]>
      run: Shaped<RunStatus | null>
    }
  }
}

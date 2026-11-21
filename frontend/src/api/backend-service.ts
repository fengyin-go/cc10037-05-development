import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'
import type {
  Drill,
  DrillPaths,
  DrillSummary,
  MigrationMapping,
  TrialResult,
  WriteImpact,
  WriteSource,
} from '@/data/schema-impact'

export { moduleMeta }

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) throw new Error(fallback)
  return (await response.json()) as T
}

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- Schema 写入影响与迁移演练 ----

export async function loadWriteImpact(params: {
  table?: string
  field?: string
  newField?: string
  changeId?: number
} = {}): Promise<WriteImpact> {
  const query = new URLSearchParams()
  if (params.table) query.set('table', params.table)
  if (params.field) query.set('field', params.field)
  if (params.newField) query.set('new_field', params.newField)
  if (params.changeId !== undefined) query.set('change_id', String(params.changeId))
  const response = await request(`/api/schema/write-impact?${query.toString()}`)
  return readJson<WriteImpact>(response, '写入影响明细读取失败')
}

export async function registerWriteSources(sources: WriteSource[]): Promise<WriteSource[]> {
  const response = await request('/api/schema/write-sources', {
    method: 'POST',
    body: JSON.stringify({ sources }),
  })
  const payload = await readJson<{ items: WriteSource[] }>(response, '写入来源登记失败')
  return payload.items
}

export async function listDrills(): Promise<DrillSummary[]> {
  const response = await request('/api/schema/drills')
  const payload = await readJson<{ items: DrillSummary[] }>(response, '迁移演练列表读取失败')
  return payload.items
}

export async function createDrill(values: {
  table?: string
  change_id?: number
  old_field?: string
  new_field?: string
} = {}): Promise<Drill> {
  const response = await request('/api/schema/drills', {
    method: 'POST',
    body: JSON.stringify(values),
  })
  return readJson<Drill>(response, '迁移演练编制失败')
}

export async function loadDrill(drillId: number): Promise<Drill> {
  const response = await request(`/api/schema/drills/${drillId}`)
  return readJson<Drill>(response, '迁移演练读取失败')
}

export async function lookupDrillPaths(drillId: number): Promise<DrillPaths> {
  const response = await request(`/api/schema/drills/${drillId}/paths`)
  return readJson<DrillPaths>(response, '演练关联路径回查失败')
}

export async function updateDrillMapping(
  drillId: number,
  values: Partial<MigrationMapping> & { edge_id: string; conversion_factor?: number | null },
): Promise<{ ok: boolean; message: string; drill: Drill }> {
  const response = await request(`/api/schema/drills/${drillId}/mapping`, {
    method: 'PATCH',
    body: JSON.stringify(values),
  })
  return readJson<{ ok: boolean; message: string; drill: Drill }>(response, '迁移映射更新失败')
}

export async function changeDrillRename(
  drillId: number,
  values: { old_field?: string; new_field: string },
): Promise<{ ok: boolean; message: string; drill: Drill }> {
  const response = await request(`/api/schema/drills/${drillId}/rename`, {
    method: 'PATCH',
    body: JSON.stringify(values),
  })
  return readJson<{ ok: boolean; message: string; drill: Drill }>(response, '改名口径更新失败')
}

export async function runDrillTrial(
  drillId: number,
  rows?: Record<string, unknown>[],
): Promise<{ ok: boolean; message: string; drill: Drill; trial: TrialResult }> {
  const response = await request(`/api/schema/drills/${drillId}/trial`, {
    method: 'POST',
    body: JSON.stringify(rows?.length ? { rows } : {}),
  })
  return readJson<{ ok: boolean; message: string; drill: Drill; trial: TrialResult }>(
    response,
    '候选数据试写失败',
  )
}

export async function submitDrill(
  drillId: number,
): Promise<{ ok: boolean; message: string; drill: Drill }> {
  const response = await request(`/api/schema/drills/${drillId}/submit`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
  return readJson<{ ok: boolean; message: string; drill: Drill }>(response, '迁移演练提交失败')
}

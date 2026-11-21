/** Schema 写入影响与迁移演练的前端契约，与后端 /api/schema/* 返回结构保持一致。 */

export type ColumnDiff = {
  old_field: string
  new_field: string
  data_type: string
  new_type: string
  change: string
  is_amount: boolean
  confidence: number
  reason: string
}

export type WriteSource = {
  edge_id: string
  target_table: string
  source_table: string
  source_field: string
  old_field: string
  new_field: string | null
  write_point: string
  compute_node: string
  pipeline: string
  mode: 'direct' | 'derived'
  label: string
  expression: string
  conversion_basis: string | null
  conversion_factor: number | null
  verified: boolean
  ready: boolean
  missing: string[]
  affects_settlement: boolean
  depends_on_table: string
  declared_by: string
}

export type PathStep = {
  node: string
  role: 'source' | 'transform' | 'write'
  field: string
  schema_version: 'old' | 'new'
  table: string
  write_point: string
  expression: string
  verified: boolean
  executable: boolean
  reason: string
}

export type WritePath = {
  path_id: string
  edge_id: string
  mode: 'direct' | 'derived'
  target_table: string
  write_point: string
  executable: boolean
  blocked_by: string[]
  steps: PathStep[]
}

export type WritePoint = {
  write_point: string
  pipeline: string
  target_table: string
  source_tables: string[]
  lineage_paths: { source: string; target: string }[]
  modes: string[]
  edge_ids: string[]
}

export type WriteImpact = {
  table: string
  field: string
  new_field: string
  change_id: number | null
  rename: {
    old_field: string
    new_field: string
    confidence: number
    declared: boolean
    reason: string
  }
  schema_versions: { old: string[]; new: string[] }
  amount_column_diff: ColumnDiff[]
  column_diff: ColumnDiff[]
  write_points: WritePoint[]
  write_sources: WriteSource[]
  paths: WritePath[]
  summary: {
    write_source_count: number
    direct_count: number
    derived_count: number
    missing_mapping: string[]
    missing_conversion_basis: string[]
    unverified_edges: string[]
    blocked_writes: string[]
  }
  analyzed_at: string
}

export type MigrationMapping = {
  mapping_id: string
  edge_id: string
  old_field: string
  new_field: string | null
  output_field: string
  mode: string
  write_point: string
  pipeline: string
  expression: string
  target_expression: string
  conversion_basis: string | null
  conversion_factor: number | null
  verified: boolean
  status: string
  missing: string[]
  upstream_ready: boolean
  upstream_blocked_by: string[]
  blocking: boolean
}

export type TrialColumn = {
  column: string
  source: 'direct' | 'derived' | 'preserved' | 'default'
  mode: string
  write_point: string
  input_fields: string[]
  inputs_ready: boolean
  verified: boolean
  values: (string | number | boolean | null)[] | null
  missing: string[]
  reason: string
}

export type TrialResult = {
  rows: Record<string, unknown>[]
  columns: TrialColumn[]
  complete_columns: TrialColumn[]
  incomplete_columns: TrialColumn[]
  inputs_ready: string[]
  missing_inputs: string[]
  blocked: boolean
  blocks_settlement_task: boolean
  blocking_columns: TrialColumn[]
  executed_writes: { edge_id: string; write_point: string; mode: string; column: string }[]
  blocked_writes: { edge_id: string; write_point: string; mode: string; column: string; reason: string }[]
  comparison: { row_index: number; direct: unknown; derived?: unknown; diff?: number | null }[]
  mapping_status: {
    mapping_id: string
    old_field: string
    new_field: string | null
    mode: string
    status: string
    blocking: boolean
  }[]
  executable: boolean
  row_count: number
}

export type Drill = {
  id: number
  drill_id: string
  status: string
  table: string
  change_id: number | null
  old_schema: { version: string; columns: { name: string }[] }
  new_schema: { version: string; columns: { name: string }[] }
  rename: WriteImpact['rename']
  write_impact: WriteImpact
  write_sources: WriteSource[]
  candidate_rows: Record<string, unknown>[]
  migration_mapping: MigrationMapping[]
  preserved_columns: string[]
  trial: TrialResult | null
  created_at: string
  updated_at: string
}

export type DrillSummary = {
  drill_id: string
  id: number
  table: string
  status: string
  rename: WriteImpact['rename']
  mapping_count: number
  blocking_count: number
  updated_at: string
}

export type DrillPaths = {
  drill_id: string
  table: string
  rename: WriteImpact['rename']
  schema_versions: { old: string[]; new: string[] }
  paths: WritePath[]
  write_points: WritePoint[]
  mapping: MigrationMapping[]
  preserved_columns: string[]
  looked_up_at: string
}

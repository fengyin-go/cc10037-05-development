<template>
  <section class="impact-panel" data-testid="schema-impact-panel">
    <header class="impact-head">
      <div>
        <h3>写入影响与迁移演练</h3>
        <p class="impact-desc">
          金额列改名后，沿表血缘追到实际写入点：直传与派生两种来源分开展开，
          缺映射或待核实的边会在路径上标出。
        </p>
      </div>
      <div class="impact-actions">
        <select
          v-if="drills.length"
          v-model.number="selectedDrillId"
          class="drill-select"
          data-testid="drill-select"
          @change="selectDrill"
        >
          <option :value="0">选择已有演练</option>
          <option v-for="item in drills" :key="item.id" :value="item.id">
            {{ item.drill_id }} · {{ item.status }} · {{ item.rename.old_field }}→{{ item.rename.new_field }}
          </option>
        </select>
        <button class="btn" data-testid="refresh-impact" type="button" :disabled="loading" @click="reload">
          刷新影响明细
        </button>
        <button class="btn" data-testid="register-sources" type="button" :disabled="loading" @click="registerSources">
          登记直传与派生边
        </button>
        <button class="btn primary" data-testid="create-drill" type="button" :disabled="loading" @click="createNewDrill">
          编制迁移演练
        </button>
      </div>
    </header>

    <p v-if="message" class="impact-message" data-testid="impact-message">{{ message }}</p>
    <p v-if="error" class="error-text" data-testid="impact-error">{{ error }}</p>

    <!-- 写入影响明细 -->
    <template v-if="impact">
      <div class="impact-summary">
        <span class="pill">改名：{{ impact.rename.old_field }} → {{ impact.rename.new_field }}</span>
        <span class="pill">写入来源 {{ impact.summary.write_source_count }}</span>
        <span class="pill">直传 {{ impact.summary.direct_count }}</span>
        <span class="pill">派生 {{ impact.summary.derived_count }}</span>
        <span v-if="impact.summary.missing_mapping.length" class="pill warn">
          缺映射 {{ impact.summary.missing_mapping.length }}
        </span>
        <span v-if="impact.summary.unverified_edges.length" class="pill warn">
          待核实 {{ impact.summary.unverified_edges.length }}
        </span>
      </div>

      <h4 class="sub-title">金额列差异</h4>
      <table class="data-table" data-testid="amount-diff">
        <thead>
          <tr><th>旧字段</th><th>新字段</th><th>旧类型</th><th>新类型</th><th>变化</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="(diff, index) in impact.amount_column_diff" :key="`diff-${index}`">
            <td>{{ diff.old_field || '—' }}</td>
            <td>{{ diff.new_field || '—' }}</td>
            <td>{{ diff.data_type || '—' }}</td>
            <td>{{ diff.new_type || '—' }}</td>
            <td>{{ changeLabel(diff.change) }}</td>
            <td>{{ diff.reason }}</td>
          </tr>
        </tbody>
      </table>

      <h4 class="sub-title">实际写入点</h4>
      <table class="data-table" data-testid="write-points">
        <thead>
          <tr><th>写入点</th><th>管线</th><th>血缘路径</th><th>写入方式</th></tr>
        </thead>
        <tbody>
          <tr v-for="point in impact.write_points" :key="point.write_point">
            <td>{{ point.write_point }}</td>
            <td>{{ point.pipeline }}</td>
            <td>{{ formatLineage(point.lineage_paths) }}</td>
            <td>{{ point.modes.map(modeLabel).join(' / ') }}</td>
          </tr>
        </tbody>
      </table>

      <h4 class="sub-title">写入来源（直传 / 派生）</h4>
      <table class="data-table" data-testid="write-sources">
        <thead>
          <tr>
            <th>来源</th><th>写入点</th><th>旧字段</th><th>新字段</th>
            <th>表达式</th><th>换算依据</th><th>核实</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="source in impact.write_sources"
            :key="source.edge_id"
            :data-testid="`source-${source.edge_id}`"
            :data-mode="source.mode"
          >
            <td><span class="pill">{{ source.label }}</span></td>
            <td>{{ source.write_point }}</td>
            <td>{{ source.old_field }}</td>
            <td>{{ source.new_field || '未映射' }}</td>
            <td><code>{{ source.expression }}</code></td>
            <td>{{ source.conversion_basis || '缺少换算依据' }}</td>
            <td>{{ source.verified ? '已核实' : '待核实' }}</td>
          </tr>
        </tbody>
      </table>

      <h4 class="sub-title">写入路径（新旧列各落 Schema 版本）</h4>
      <div class="path-list" data-testid="write-paths">
        <article
          v-for="path in impact.paths"
          :key="path.path_id"
          class="path-card"
          :class="{ blocked: !path.executable }"
          :data-testid="`path-${path.edge_id}`"
        >
          <header class="path-head">
            <strong>{{ modeLabel(path.mode) }}</strong>
            <span class="path-point">{{ path.write_point }}</span>
            <span class="pill" :class="path.executable ? 'ok' : 'warn'">
              {{ path.executable ? '可执行' : '阻断' }}
            </span>
          </header>
          <ol class="path-steps">
            <li v-for="(step, index) in path.steps" :key="`${path.path_id}-${index}`">
              <span class="step-node">{{ step.node }}</span>
              <code class="step-field">{{ step.field }}</code>
              <span class="version-tag" :class="step.schema_version">{{ step.schema_version }} schema</span>
              <span v-if="step.expression" class="step-expr">{{ step.expression }}</span>
              <span v-if="!step.executable && step.role === 'write'" class="step-reason">{{ step.reason }}</span>
            </li>
          </ol>
          <p v-if="path.blocked_by.length" class="path-blocked">{{ path.blocked_by.join('；') }}</p>
        </article>
      </div>
    </template>

    <!-- 迁移演练 -->
    <template v-if="drill">
      <hr class="impact-divider" />
      <div class="drill-head">
        <h3>迁移演练 {{ drill.drill_id }}</h3>
        <span class="pill" data-testid="drill-status">{{ drill.status }}</span>
        <span class="impact-desc">{{ drill.table }}</span>
        <div class="impact-actions">
          <button class="btn" data-testid="lookup-paths" type="button" @click="lookupPaths">
            回查字段路径
          </button>
          <button class="btn" data-testid="trial-drill" type="button" @click="trialDrill">
            生成候选试写
          </button>
          <button class="btn primary" data-testid="submit-drill" type="button" @click="submitDrill">
            提交迁移演练
          </button>
        </div>
      </div>

      <div class="rename-editor">
        <label>
          <span>改名口径：旧字段</span>
          <input v-model="renameForm.old_field" data-testid="rename-old" />
        </label>
        <label>
          <span>新字段</span>
          <input v-model="renameForm.new_field" data-testid="rename-new" />
        </label>
        <button class="btn" data-testid="apply-rename" type="button" @click="applyRename">
          应用改名口径
        </button>
        <span class="impact-desc">未参与改名的列原路径保留：{{ drill.preserved_columns.join('、') || '—' }}</span>
      </div>

      <h4 class="sub-title">迁移映射</h4>
      <table class="data-table" data-testid="migration-mapping">
        <thead>
          <tr>
            <th>写入边</th><th>旧字段</th><th>新字段</th><th>方式</th>
            <th>原表达式</th><th>迁移后表达式</th><th>映射状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in drill.migration_mapping"
            :key="item.mapping_id"
            :data-testid="`mapping-${item.edge_id}`"
            :data-status="item.status"
          >
            <td>{{ item.write_point }}</td>
            <td>{{ item.old_field }}</td>
            <td>{{ item.new_field || '—' }}</td>
            <td>{{ modeLabel(item.mode) }}</td>
            <td><code>{{ item.expression }}</code></td>
            <td><code>{{ item.target_expression }}</code></td>
            <td>
              <span class="pill" :class="item.blocking ? 'warn' : 'ok'">{{ item.status }}</span>
              <span v-if="item.upstream_blocked_by?.length" class="step-reason">
                {{ item.upstream_blocked_by.join('；') }}
              </span>
            </td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`edit-${item.edge_id}`"
                @click="startEdit(item)"
              >
                {{ item.blocking ? '补映射/核实' : '调整' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <form v-if="editingEdgeId" class="mapping-form" data-testid="mapping-form" @submit.prevent="saveMapping">
        <strong>编辑 {{ editingEdgeId }}</strong>
        <label>
          <span>新字段</span>
          <input v-model="mappingForm.new_field" data-testid="mapping-new-field" />
        </label>
        <label>
          <span>换算依据</span>
          <input
            v-model="mappingForm.conversion_basis"
            data-testid="mapping-basis"
            placeholder="例如：应收额 = 结算金额 * (1 - 折扣率)"
          />
        </label>
        <label>
          <span>换算系数</span>
          <input v-model="mappingForm.conversion_factor" data-testid="mapping-factor" placeholder="可留空" />
        </label>
        <label class="checkbox">
          <input v-model="mappingForm.verified" data-testid="mapping-verified" type="checkbox" />
          <span>转换边已核实</span>
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="editingEdgeId = ''">取消</button>
          <button class="btn primary" data-testid="save-mapping" type="submit">保存映射</button>
        </div>
      </form>

      <!-- 路径回查 -->
      <template v-if="pathsResult">
        <h4 class="sub-title">本次演练关联的字段路径</h4>
        <table class="data-table" data-testid="lookup-result">
          <thead>
            <tr><th>写入边</th><th>方式</th><th>路径</th><th>可执行</th></tr>
          </thead>
          <tbody>
            <tr v-for="path in pathsResult.paths" :key="path.path_id">
              <td>{{ path.edge_id }}</td>
              <td>{{ modeLabel(path.mode) }}</td>
              <td>{{ path.steps.map((step) => `${step.field}@${step.schema_version}`).join(' → ') }}</td>
              <td>{{ path.executable ? '是' : path.blocked_by.join('；') }}</td>
            </tr>
          </tbody>
        </table>
      </template>

      <!-- 试写结果 -->
      <template v-if="drill.trial">
        <h4 class="sub-title">候选数据试写</h4>
        <div class="impact-summary">
          <span class="pill">候选行 {{ drill.trial.row_count }}</span>
          <span class="pill" :class="drill.trial.blocks_settlement_task ? 'warn' : 'ok'">
            {{ drill.trial.blocks_settlement_task ? '结算任务被阻断' : '结算任务可继续' }}
          </span>
          <span class="pill">已具备输入：{{ drill.trial.inputs_ready.join('、') || '—' }}</span>
          <span v-if="drill.trial.missing_inputs.length" class="pill warn">
            缺少输入：{{ drill.trial.missing_inputs.join('、') }}
          </span>
        </div>

        <table class="data-table" data-testid="trial-columns">
          <thead>
            <tr><th>目标列</th><th>来源</th><th>输入字段</th><th>输入就绪</th><th>试写值</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="(column, index) in drill.trial.columns"
              :key="`${column.column}-${column.source}-${index}`"
              :data-testid="`trial-column-${column.column}-${column.source}`"
            >
              <td>{{ column.column }}</td>
              <td>{{ sourceLabel(column.source) }}</td>
              <td>{{ column.input_fields.join('、') || '—' }}</td>
              <td>{{ column.inputs_ready ? '是' : '否' }}</td>
              <td>{{ formatValues(column.values) }}</td>
              <td>{{ column.reason || column.missing.join('；') || '—' }}</td>
            </tr>
          </tbody>
        </table>

        <h4 class="sub-title">直传与派生对照</h4>
        <table class="data-table" data-testid="trial-comparison">
          <thead>
            <tr><th>行</th><th>直传金额</th><th>派生应收额</th><th>差异</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in drill.trial.comparison" :key="row.row_index">
              <td>{{ row.row_index + 1 }}</td>
              <td>{{ row.direct ?? '—' }}</td>
              <td>{{ row.derived ?? '—' }}</td>
              <td>{{ row.diff ?? '—' }}</td>
            </tr>
          </tbody>
        </table>

        <h4 class="sub-title">候选写入数据</h4>
        <table class="data-table" data-testid="trial-rows">
          <thead>
            <tr><th v-for="column in trialRowColumns" :key="column">{{ column }}</th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in drill.trial.rows" :key="index">
              <td v-for="column in trialRowColumns" :key="`${index}-${column}`">{{ row[column] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  changeDrillRename,
  createDrill,
  listDrills,
  loadDrill,
  loadWriteImpact,
  lookupDrillPaths as fetchDrillPaths,
  registerWriteSources,
  runDrillTrial,
  submitDrill as submitDrillRequest,
  updateDrillMapping,
} from '@/api/backend-service'
import type {
  Drill,
  DrillPaths,
  DrillSummary,
  MigrationMapping,
  TrialColumn,
  WriteImpact,
  WriteSource,
} from '@/data/schema-impact'

const props = defineProps<{ changeId?: number; drillId?: number }>()

const impact = ref<WriteImpact | null>(null)
const drill = ref<Drill | null>(null)
const drills = ref<DrillSummary[]>([])
const selectedDrillId = ref(0)
const pathsResult = ref<DrillPaths | null>(null)
const loading = ref(false)
const error = ref('')
const message = ref('')

const renameForm = ref({ old_field: '', new_field: '' })
const editingEdgeId = ref('')
const mappingForm = ref({
  new_field: '',
  conversion_basis: '',
  conversion_factor: '',
  verified: false,
})

const trialRowColumns = computed(() => {
  const rows = drill.value?.trial?.rows ?? []
  if (!rows.length) return []
  return Object.keys(rows[0])
})

function modeLabel(mode: string) {
  if (mode === 'direct') return '直接传值'
  if (mode === 'derived') return '依赖旧字段的计算'
  return mode
}

function changeLabel(change: string) {
  const labels: Record<string, string> = {
    rename: '改名',
    unchanged: '不变',
    add: '新增',
    drop: '删除',
    type_change: '类型变更',
  }
  return labels[change] ?? change
}

function sourceLabel(source: TrialColumn['source']) {
  const labels: Record<string, string> = {
    direct: '直传',
    derived: '派生',
    preserved: '原路径保留',
    default: '默认值',
  }
  return labels[source] ?? source
}

function formatLineage(paths: { source: string; target: string }[]) {
  return paths.map((item) => `${item.source} → ${item.target}`).join('；') || '—'
}

function formatValues(values: TrialColumn['values']) {
  if (!values) return '—'
  return values.slice(0, 3).join('、') + (values.length > 3 ? ' …' : '')
}

function syncDrillState(payload: Drill) {
  drill.value = payload
  renameForm.value = {
    old_field: payload.rename.old_field,
    new_field: payload.rename.new_field,
  }
  impact.value = payload.write_impact
}

async function refreshDrills() {
  try {
    drills.value = await listDrills()
  } catch {
    drills.value = []
  }
}

async function selectDrill() {
  if (!selectedDrillId.value) return
  error.value = ''
  try {
    const payload = await loadDrill(selectedDrillId.value)
    syncDrillState(payload)
    pathsResult.value = null
    message.value = `已读取演练 ${payload.drill_id} 的实际映射与待处理字段`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '迁移演练读取失败'
  }
}

async function reload() {
  loading.value = true
  error.value = ''
  try {
    impact.value = await loadWriteImpact({ changeId: props.changeId })
    if (drill.value) {
      drill.value = await loadDrill(drill.value.id)
    }
    await refreshDrills()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '写入影响明细读取失败'
  } finally {
    loading.value = false
  }
}

async function registerSources() {
  if (!impact.value) return
  loading.value = true
  error.value = ''
  try {
    const sources = impact.value.write_sources as WriteSource[]
    const registered = await registerWriteSources(sources)
    message.value = `已登记 ${registered.length} 条写入边（直传与派生各保留来源）`
    await reload()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '写入来源登记失败'
  } finally {
    loading.value = false
  }
}

async function createNewDrill() {
  loading.value = true
  error.value = ''
  try {
    const payload = await createDrill({
      change_id: props.changeId,
      table: impact.value?.table,
      old_field: impact.value?.field,
      new_field: impact.value?.new_field,
    })
    syncDrillState(payload)
    selectedDrillId.value = payload.id
    pathsResult.value = null
    message.value = '迁移演练已编制，迁移映射已生成'
    await refreshDrills()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '迁移演练编制失败'
  } finally {
    loading.value = false
  }
}

function startEdit(item: MigrationMapping) {
  editingEdgeId.value = item.edge_id
  mappingForm.value = {
    new_field: item.new_field ?? '',
    conversion_basis: item.conversion_basis ?? '',
    conversion_factor: item.conversion_factor === null || item.conversion_factor === undefined
      ? ''
      : String(item.conversion_factor),
    verified: item.verified,
  }
}

async function saveMapping() {
  if (!drill.value) return
  error.value = ''
  try {
    const factor = mappingForm.value.conversion_factor.trim()
    const current = drill.value.migration_mapping.find(
      (item) => item.edge_id === editingEdgeId.value,
    )
    const result = await updateDrillMapping(drill.value.id, {
      edge_id: editingEdgeId.value,
      new_field: mappingForm.value.new_field.trim() || null,
      conversion_basis: mappingForm.value.conversion_basis.trim() || null,
      // 系数留空时不覆盖已登记的换算系数。
      conversion_factor: factor ? Number(factor) : current?.conversion_factor ?? null,
      verified: mappingForm.value.verified,
    })
    syncDrillState(result.drill)
    editingEdgeId.value = ''
    message.value = result.message
    await refreshDrills()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '迁移映射更新失败'
  }
}

async function applyRename() {
  if (!drill.value) return
  error.value = ''
  try {
    const result = await changeDrillRename(drill.value.id, {
      old_field: renameForm.value.old_field.trim(),
      new_field: renameForm.value.new_field.trim(),
    })
    syncDrillState(result.drill)
    message.value = result.message
    await refreshDrills()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '改名口径更新失败'
  }
}

async function trialDrill() {
  if (!drill.value) return
  error.value = ''
  try {
    const result = await runDrillTrial(drill.value.id)
    syncDrillState(result.drill)
    message.value = result.message
    await refreshDrills()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '候选数据试写失败'
  }
}

async function submitDrill() {
  if (!drill.value) return
  error.value = ''
  try {
    const result = await submitDrillRequest(drill.value.id)
    syncDrillState(result.drill)
    message.value = result.message
    await refreshDrills()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '迁移演练提交失败'
  }
}

async function lookupPaths() {
  if (!drill.value) return
  error.value = ''
  try {
    pathsResult.value = await fetchDrillPaths(drill.value.id)
    message.value = '已回查本次演练关联的字段路径'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '演练关联路径回查失败'
  }
}

onMounted(async () => {
  await reload()
  if (props.drillId) {
    try {
      const payload = await loadDrill(props.drillId)
      syncDrillState(payload)
      selectedDrillId.value = payload.id
    } catch (err) {
      error.value = err instanceof Error ? err.message : '迁移演练读取失败'
    }
  }
})
</script>

<style scoped>
.impact-panel {
  margin-top: 20px;
  padding: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
}
.impact-head,
.drill-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}
.drill-head { align-items: center; }
.impact-head h3,
.drill-head h3 { margin: 0; font-size: 15px; }
.impact-desc { color: var(--muted); font-size: 12px; margin: 4px 0 0; }
.impact-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.drill-select {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  background: #fff;
}
.impact-message { color: #0a6c3d; font-size: 13px; }
.impact-summary { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eef2f7; font-size: 12px; color: #334155; }
.pill.ok { background: #e7f6ec; color: #0a6c3d; }
.pill.warn { background: #fdeeee; color: #b42318; }
.sub-title { font-size: 13px; margin: 16px 0 8px; }
.impact-divider { border: none; border-top: 1px solid var(--border); margin: 20px 0; }
.path-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
.path-card { border: 1px solid var(--border); border-radius: 8px; padding: 12px; background: #fafbfc; }
.path-card.blocked { border-color: #f3c2c2; background: #fff8f8; }
.path-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.path-point { color: var(--muted); font-size: 12px; }
.path-steps { margin: 0; padding-left: 18px; font-size: 12px; }
.path-steps li { margin-bottom: 6px; }
.step-node { font-weight: 600; margin-right: 6px; }
.step-field { background: #eef2f7; padding: 1px 4px; border-radius: 4px; margin-right: 6px; }
.version-tag { font-size: 11px; padding: 1px 6px; border-radius: 4px; margin-right: 6px; }
.version-tag.old { background: #fff1e6; color: #9a4b00; }
.version-tag.new { background: #e7f6ec; color: #0a6c3d; }
.step-expr { color: var(--muted); margin-right: 6px; }
.step-reason { color: #b42318; }
.path-blocked { color: #b42318; font-size: 12px; margin: 8px 0 0; }
.rename-editor { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 12px 0; }
.rename-editor label,
.mapping-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.rename-editor input,
.mapping-form input {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
}
.mapping-form {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
  margin-top: 12px;
  padding: 12px;
  border: 1px dashed var(--border);
  border-radius: 8px;
}
.mapping-form .checkbox { flex-direction: row; align-items: center; gap: 6px; }
</style>

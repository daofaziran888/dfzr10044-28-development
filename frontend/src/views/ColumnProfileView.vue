<template>
  <section class="page" data-testid="column-profile-page">
    <header class="page-head">
      <div>
        <h2>列画像 · 按样本范围计算</h2>
        <p class="page-desc">
          先按结构版本与来源批次筛定样本，再计算列画像；空值与缺列分开核对，文本取值不进入数值区间。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/column_stat">返回列统计清单</RouterLink>
        <RouterLink class="btn" to="/compact_job/candidates">压实候选</RouterLink>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="loadScope">
      <label>所属湖表
        <input v-model="form.lake_table" data-testid="scope-lake-table" required />
      </label>
      <label>列名称
        <input v-model="form.column" data-testid="scope-column" required />
      </label>
      <label>结构版本
        <select v-model="form.schema_version" data-testid="scope-schema-version">
          <option value="">全部结构版本</option>
          <option v-for="version in scope?.schema_versions ?? []" :key="version" :value="version">
            {{ version }}
          </option>
        </select>
      </label>
      <label>来源系统
        <select v-model="form.source_system" data-testid="scope-source-system">
          <option value="">全部来源系统</option>
          <option v-for="source in scope?.source_systems ?? []" :key="source" :value="source">
            {{ source }}
          </option>
        </select>
      </label>
      <button class="btn primary" data-testid="load-scope" type="submit">筛定样本</button>
      <button class="btn" data-testid="compute-profile" type="button" :disabled="computing" @click="compute">
        {{ computing ? '计算中…' : '计算列画像' }}
      </button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div v-if="scope" class="profile-panel">
      <h3>可筛定样本批次</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>纳入</th><th>批次</th><th>结构版本</th><th>修订</th><th>来源系统</th>
            <th>行数</th><th>含该列</th><th>缺列行</th><th>空值行</th><th>采集窗口</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in scope.available_batches" :key="batch.batch_id">
            <td>
              <input v-model="selectedBatches" type="checkbox" :value="batch.batch_id"
                     :data-testid="`select-${batch.batch_id}`" />
            </td>
            <td>{{ batch.batch_id }}</td>
            <td>{{ batch.schema_version ?? '—' }}</td>
            <td>{{ batch.revision }}</td>
            <td>{{ batch.source_system }}</td>
            <td>{{ batch.row_count }}</td>
            <td>{{ batch.has_column ? '是' : '否' }}</td>
            <td :class="{ 'warn-cell': batch.missing_column_rows > 0 }">{{ batch.missing_column_rows }}</td>
            <td :class="{ 'warn-cell': batch.null_rows > 0 }">{{ batch.null_rows }}</td>
            <td>{{ batch.window }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="profile" class="profile-panel" data-testid="profile-result">
      <header class="panel-head">
        <h3>画像 {{ profile.profile_version }}</h3>
        <div class="page-actions">
          <button class="btn" type="button" @click="buildCandidate">据此生成压实候选</button>
          <button class="btn" type="button" @click="recompute">复算画像原件</button>
        </div>
      </header>

      <div class="stat-row">
        <article class="stat-card"><span class="stat-label">样本行数</span><strong class="stat-value">{{ profile.stats.total_count }}</strong></article>
        <article class="stat-card"><span class="stat-label">空值行</span><strong class="stat-value">{{ profile.stats.null_count }}</strong></article>
        <article class="stat-card"><span class="stat-label">缺列行</span><strong class="stat-value">{{ profile.stats.missing_count }}</strong></article>
        <article class="stat-card"><span class="stat-label">数值区间</span><strong class="stat-value">{{ rangeText }}</strong></article>
        <article class="stat-card"><span class="stat-label">文本取值（不参与区间）</span><strong class="stat-value">{{ profile.stats.text_count }}</strong></article>
      </div>

      <dl class="detail-grid">
        <div><dt>画像编号</dt><dd data-testid="profile-id">{{ profile.profile_id }}</dd></div>
        <div><dt>画像版本</dt><dd data-testid="profile-version">{{ profile.profile_version }}</dd></div>
        <div><dt>样本指纹</dt><dd>{{ profile.fingerprint }}</dd></div>
        <div><dt>已确认分区</dt><dd data-testid="confirmed-partitions">{{ profile.stats.confirmed_partitions.join('、') || '无' }}</dd></div>
        <div><dt>缺列与空值分开核对</dt><dd>{{ profile.stats.missing_count }} 行缺列 / {{ profile.stats.null_count }} 行空值</dd></div>
        <div><dt>文本取值样例</dt><dd>{{ profile.stats.text_values.join('、') || '无' }}</dd></div>
      </dl>

      <h4>样本清单</h4>
      <table class="data-table">
        <thead>
          <tr><th>批次</th><th>结构版本</th><th>修订</th><th>来源系统</th><th>批次行数</th><th>校验值</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in profile.sample_manifest" :key="item.batch_id">
            <td>{{ item.batch_id }}</td>
            <td>{{ item.schema_version ?? '—' }}</td>
            <td>{{ item.revision }}</td>
            <td>{{ item.source_system }}</td>
            <td>{{ item.row_count }}</td>
            <td>{{ item.checksum }}</td>
          </tr>
        </tbody>
      </table>

      <h4>核对结果</h4>
      <ul class="check-list">
        <li v-for="check in profile.verification.checks" :key="check.name" :class="check.ok ? 'check-ok' : 'check-bad'">
          <strong>{{ check.ok ? '通过' : '异常' }}</strong> · {{ check.name }}：{{ check.detail }}
        </li>
      </ul>

      <div v-if="profile.candidates?.length">
        <h4>关联压实候选</h4>
        <table class="data-table">
          <thead>
            <tr><th>候选</th><th>画像版本</th><th>来源批次</th><th>分区</th><th>分区行数</th><th>门槛</th><th>结论</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="candidate in profile.candidates" :key="candidate.candidate_id">
              <td>{{ candidate.candidate_id }}</td>
              <td>{{ candidate.profile_version }}</td>
              <td>{{ candidate.sample_batch_ids.join('、') }}</td>
              <td>{{ candidate.partition_key }}</td>
              <td>{{ candidate.partition_rows }}</td>
              <td>{{ candidate.row_threshold }}</td>
              <td>{{ candidate.decision ? '处理' : '不处理' }}</td>
              <td>{{ candidate.status }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="recomputeResult" class="profile-panel">
      <h4>复算结果</h4>
      <p :class="recomputeResult.matches ? 'check-ok' : 'check-bad'">
        原件指纹 {{ recomputeResult.original_fingerprint }} ／ 复算指纹 {{ recomputeResult.recomputed_fingerprint }}
        —— {{ recomputeResult.matches ? '可复算一致' : '与原件不一致' }}
      </p>
    </div>

    <div v-if="profiles.length" class="profile-panel">
      <h4>已登记画像</h4>
      <table class="data-table">
        <thead>
          <tr><th>画像版本</th><th>湖表</th><th>列</th><th>结构版本</th><th>样本批次</th><th>已确认分区</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in profiles" :key="item.profile_id" :data-testid="`profile-row-${item.profile_id}`">
            <td>{{ item.profile_version }}</td>
            <td>{{ item.lake_table }}</td>
            <td>{{ item.column }}</td>
            <td>{{ item.schema_version ?? '—' }}</td>
            <td>{{ item.scope.batch_ids.join('、') }}</td>
            <td>{{ item.scope.confirmed_partitions.join('、') }}</td>
            <td><button class="link" type="button" @click="openProfile(item.profile_id)">查看详情</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  buildCandidates,
  computeColumnProfile,
  fetchSampleScope,
  getProfile,
  listProfiles,
  recomputeProfile,
  type ColumnProfile,
  type SampleScope,
} from '@/api/backend-service'

const form = ref({
  lake_table: 'ods_user_behavior',
  column: 'amount',
  schema_version: 'SCH-v1-001',
  source_system: '',
})
const scope = ref<SampleScope | null>(null)
const selectedBatches = ref<string[]>([])
const profile = ref<ColumnProfile | null>(null)
const profiles = ref<ColumnProfile[]>([])
const recomputeResult = ref<Record<string, unknown> | null>(null)
const errorMessage = ref('')
const computing = ref(false)

const rangeText = computed(() => {
  const range = profile.value?.stats.numeric_range
  return range ? `${range.min} ~ ${range.max}` : '无（无有效数值）'
})

async function loadScope() {
  errorMessage.value = ''
  try {
    scope.value = await fetchSampleScope({
      lake_table: form.value.lake_table,
      column: form.value.column,
      schema_version: form.value.schema_version,
      source_system: form.value.source_system,
    })
    selectedBatches.value = scope.value.available_batches
      .filter((batch) => batch.selected)
      .map((batch) => batch.batch_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '样本范围读取失败'
  }
}

async function compute() {
  errorMessage.value = ''
  computing.value = true
  try {
    profile.value = await computeColumnProfile({
      lake_table: form.value.lake_table,
      column: form.value.column,
      schema_version: form.value.schema_version || null,
      source_system: form.value.source_system || null,
      batch_ids: selectedBatches.value,
    })
    recomputeResult.value = null
    await refreshProfiles()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '列画像计算失败'
  } finally {
    computing.value = false
  }
}

async function buildCandidate() {
  if (!profile.value) return
  errorMessage.value = ''
  try {
    const result = await buildCandidates({
      lake_table: profile.value.lake_table,
      column: profile.value.column,
      schema_version: profile.value.schema_version,
      batch_ids: profile.value.scope.batch_ids,
    })
    // 候选绑定的是本次生成的新画像版本，详情页切换到该版本才能看到关联候选。
    profile.value = await getProfile(result.profile.profile_id)
    errorMessage.value = ''
    await refreshProfiles()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '压实候选生成失败'
  }
}

async function recompute() {
  if (!profile.value) return
  errorMessage.value = ''
  try {
    recomputeResult.value = await recomputeProfile(profile.value.profile_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '列画像复算失败'
  }
}

async function openProfile(profileId: string) {
  errorMessage.value = ''
  try {
    profile.value = await getProfile(profileId)
    recomputeResult.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '列画像详情读取失败'
  }
}

async function refreshProfiles() {
  try {
    profiles.value = await listProfiles()
  } catch {
    profiles.value = []
  }
}

onMounted(async () => {
  await loadScope()
  await refreshProfiles()
})
</script>

<style scoped>
.profile-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 14px 0; }
.panel-head { display: flex; justify-content: space-between; align-items: center; }
.detail-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.detail-grid dt { font-size: 12px; color: var(--muted); }
.detail-grid dd { margin: 2px 0 8px; font-size: 13px; word-break: break-all; }
.check-list { list-style: none; padding: 0; margin: 0 0 12px; font-size: 13px; }
.check-ok { color: #15803d; }
.check-bad { color: #b42318; }
.warn-cell { color: #b42318; font-weight: 600; }
</style>

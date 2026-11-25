<template>
  <section class="page" data-testid="compact-candidate-page">
    <header class="page-head">
      <div>
        <h2>压实候选 · 按列画像领取</h2>
        <p class="page-desc">
          候选记录绑定采用的画像版本与来源批次；批次修订后待领候选重新比对门槛，已领候选保留画像原件可复算。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/column_stat">列统计清单</RouterLink>
        <RouterLink class="btn" to="/column_stat/profiles">列画像</RouterLink>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <table class="data-table">
      <thead>
        <tr>
          <th>候选</th><th>湖表 / 列</th><th>画像版本</th><th>样本批次</th><th>分区</th>
          <th>分区行数</th><th>门槛</th><th>结论</th><th>状态</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="candidate in candidates" :key="candidate.candidate_id" :data-testid="`candidate-${candidate.candidate_id}`">
          <td>{{ candidate.candidate_id }}</td>
          <td>{{ candidate.lake_table }} / {{ candidate.column }}</td>
          <td>
            {{ candidate.profile_version }}
            <span v-if="candidate.claimed_profile_version" class="tag">已领 {{ candidate.claimed_profile_version }}</span>
          </td>
          <td>{{ candidate.sample_batch_ids.join('、') }}</td>
          <td>{{ candidate.partition_key }}</td>
          <td>{{ candidate.partition_rows }}</td>
          <td>{{ candidate.row_threshold }}</td>
          <td :class="candidate.decision ? 'check-ok' : 'check-bad'">
            {{ candidate.decision ? '处理' : '不处理' }} · {{ candidate.reason }}
          </td>
          <td>{{ candidate.status }}</td>
          <td class="row-actions">
            <button v-if="candidate.status === '待领取'" class="link" type="button"
                    :data-testid="`claim-${candidate.candidate_id}`" @click="claim(candidate)">
              领取
            </button>
            <button v-if="candidate.status === '已领取'" class="link" type="button"
                    :data-testid="`verify-${candidate.candidate_id}`" @click="verify(candidate)">
              复核
            </button>
            <button class="link" type="button" @click="openProfile(candidate.profile_id)">画像原件</button>
          </td>
        </tr>
        <tr v-if="!candidates.length">
          <td colspan="10" class="empty-state">暂无压实候选，请先在列画像页按样本范围计算并生成候选</td>
        </tr>
      </tbody>
    </table>

    <div v-if="lastResult" class="profile-panel" data-testid="candidate-result">
      <h4>{{ lastAction }}</h4>
      <dl class="detail-grid">
        <div><dt>消费画像版本</dt><dd>{{ consumedVersion }}</dd></div>
        <div><dt>消费样本批次</dt><dd>{{ consumedBatches }}</dd></div>
        <div><dt>样本修订</dt><dd>{{ consumedRevisions }}</dd></div>
        <div v-if="consumedFingerprint"><dt>消费指纹</dt><dd>{{ consumedFingerprint }}</dd></div>
        <div v-if="'reproducible' in lastResult"><dt>原件复算</dt><dd>{{ lastResult.reproducible ? '一致' : '不一致' }}</dd></div>
        <div v-if="'sample_changed_after_claim' in lastResult">
          <dt>领取后样本是否变化</dt><dd>{{ lastResult.sample_changed_after_claim ? '已变化（原件仍可复算）' : '未变化' }}</dd>
        </div>
      </dl>
      <pre class="result-json">{{ prettyResult }}</pre>
    </div>

    <div v-if="selectedProfile" class="profile-panel">
      <h4>画像原件 {{ selectedProfile.profile_version }}</h4>
      <p class="page-desc">已确认分区：{{ selectedProfile.scope.confirmed_partitions.join('、') || '无' }}；
        样本行数：{{ selectedProfile.scope.row_count }}</p>
      <table class="data-table">
        <thead>
          <tr><th>批次</th><th>结构版本</th><th>修订</th><th>来源系统</th><th>批次行数</th><th>校验值</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in selectedProfile.sample_manifest" :key="item.batch_id">
            <td>{{ item.batch_id }}</td>
            <td>{{ item.schema_version ?? '—' }}</td>
            <td>{{ item.revision }}</td>
            <td>{{ item.source_system }}</td>
            <td>{{ item.row_count }}</td>
            <td>{{ item.checksum }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  claimCandidate,
  getProfile,
  listCandidates,
  verifyCandidate,
  type ColumnProfile,
  type CompactCandidate,
} from '@/api/backend-service'

const candidates = ref<CompactCandidate[]>([])
const errorMessage = ref('')
const lastResult = ref<Record<string, unknown> | null>(null)
const lastAction = ref('')
const selectedProfile = ref<ColumnProfile | null>(null)

const consumedVersion = computed(() => {
  const sample = lastResult.value?.consumed_sample as Record<string, unknown> | undefined
  return String(
    sample?.profile_version ?? lastResult.value?.consumed_profile_version ?? '—',
  )
})
const consumedBatches = computed(() => {
  const sample = lastResult.value?.consumed_sample as Record<string, unknown> | undefined
  const ids = (sample?.batch_ids ?? lastResult.value?.consumed_batch_ids ?? []) as string[]
  return ids.length ? ids.join('、') : '—'
})
const consumedRevisions = computed(() => {
  const sample = lastResult.value?.consumed_sample as Record<string, unknown> | undefined
  const revisions = (sample?.batch_revisions ?? lastResult.value?.consumed_batch_revisions ?? {}) as Record<string, number>
  const entries = Object.entries(revisions)
  return entries.length ? entries.map(([id, rev]) => `${id}@r${rev}`).join('、') : '—'
})
const consumedFingerprint = computed(() => {
  const sample = lastResult.value?.consumed_sample as Record<string, unknown> | undefined
  return String(sample?.fingerprint ?? lastResult.value?.consumed_fingerprint ?? '')
})
const prettyResult = computed(() => JSON.stringify(lastResult.value, null, 2))

async function reload() {
  errorMessage.value = ''
  try {
    candidates.value = await listCandidates()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '压实候选读取失败'
  }
}

async function claim(candidate: CompactCandidate) {
  errorMessage.value = ''
  try {
    lastResult.value = await claimCandidate(candidate.candidate_id)
    lastAction.value = `已领取 ${candidate.candidate_id}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取失败'
  }
}

async function verify(candidate: CompactCandidate) {
  errorMessage.value = ''
  try {
    lastResult.value = await verifyCandidate(candidate.candidate_id)
    lastAction.value = `复核 ${candidate.candidate_id}`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核失败'
  }
}

async function openProfile(profileId: string) {
  errorMessage.value = ''
  try {
    selectedProfile.value = await getProfile(profileId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '画像原件读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.profile-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 14px 0; }
.detail-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.detail-grid dt { font-size: 12px; color: var(--muted); }
.detail-grid dd { margin: 2px 0 8px; font-size: 13px; word-break: break-all; }
.check-ok { color: #15803d; }
.check-bad { color: #b42318; }
.tag { margin-left: 6px; font-size: 11px; background: #eef4ff; color: var(--brand); border-radius: 4px; padding: 1px 5px; }
.result-json { background: #f7f9fc; border: 1px solid var(--border); border-radius: 6px; padding: 10px; font-size: 12px; max-height: 260px; overflow: auto; }
</style>

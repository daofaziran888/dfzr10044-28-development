import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

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

// ------------------------- 列画像 / 压实候选 -------------------------

export type SampleScope = {
  lake_table: string
  column: string
  partition_column: string
  schema_versions: string[]
  source_systems: string[]
  selected_batch_ids: string[]
  available_batches: {
    batch_id: string
    schema_version: string | null
    revision: number
    source_system: string
    row_count: number
    window: string
    checksum: string
    status: string
    has_column: boolean
    missing_column_rows: number
    null_rows: number
    selected: boolean
  }[]
}

export type ColumnProfile = {
  profile_id: string
  profile_version: string
  lake_table: string
  column: string
  schema_version: string | null
  scope: {
    batch_ids: string[]
    schema_versions: string[]
    source_systems: string[]
    partition_column: string
    batch_count: number
    row_count: number
    partitions: Record<string, number>
    confirmed_partitions: string[]
  }
  sample_manifest: {
    batch_id: string
    schema_version: string | null
    revision: number
    source_system: string
    row_count: number
    checksum: string
    window_start: string
    window_end: string
  }[]
  stats: {
    total_count: number
    present_count: number
    missing_count: number
    null_count: number
    non_null_count: number
    numeric_count: number
    text_count: number
    null_ratio: number
    missing_ratio: number
    distinct_base: number
    min_value: number | null
    max_value: number | null
    numeric_range: { min: number; max: number } | null
    text_values: string[]
    partitions: Record<string, number>
    confirmed_partitions: string[]
    per_batch: {
      batch_id: string
      schema_version: string | null
      revision: number
      total_count: number
      missing_count: number
      null_count: number
      numeric_count: number
      text_count: number
    }[]
  }
  verification: { checks: { name: string; detail: string; ok: boolean }[] }
  fingerprint: string
  created_at: number
  candidates?: CompactCandidate[]
}

export type CompactCandidate = {
  candidate_id: string
  lake_table: string
  column: string
  partition_key: string
  profile_id: string
  profile_version: string
  profile_fingerprint: string
  sample_batch_ids: string[]
  sample_manifest: ColumnProfile['sample_manifest']
  total_rows: number
  partition_rows: number
  row_threshold: number
  decision: boolean
  reason: string
  judgement: Record<string, unknown>
  status: string
  claimed_profile_version: string | null
  claimed_fingerprint: string | null
  claimed_batch_revisions: Record<string, number>
  revalidated_profile_id: string | null
  revalidated_fingerprint: string | null
  recompare_history: unknown[]
}

export async function fetchSampleScope(params: {
  lake_table: string
  column: string
  schema_version?: string
  source_system?: string
}): Promise<SampleScope> {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, value]) => String(value ?? '').trim() !== '') as [string, string][],
  ).toString()
  const response = await request(`/api/column_stat/scope?${query}`)
  if (!response.ok) throw new Error('样本范围读取失败')
  return await response.json()
}

export async function computeColumnProfile(payload: {
  lake_table: string
  column: string
  schema_version?: string | null
  batch_ids?: string[]
  source_system?: string | null
  partition_column?: string
}): Promise<ColumnProfile> {
  const response = await request('/api/column_stat/profiles/compute', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('列画像计算失败')
  return await response.json()
}

export async function listProfiles(): Promise<ColumnProfile[]> {
  const response = await request('/api/column_stat/profiles')
  if (!response.ok) throw new Error('列画像列表读取失败')
  return (await response.json()).items
}

export async function getProfile(profileId: string): Promise<ColumnProfile> {
  const response = await request(`/api/column_stat/profiles/${profileId}`)
  if (!response.ok) throw new Error('列画像详情读取失败')
  return await response.json()
}

export async function recomputeProfile(profileId: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/column_stat/profiles/${profileId}/recompute`, { method: 'POST' })
  if (!response.ok) throw new Error('列画像复算失败')
  return await response.json()
}

export async function buildCandidates(payload: {
  lake_table: string
  column: string
  schema_version?: string | null
  batch_ids?: string[]
  source_system?: string | null
  partition_key?: string | null
  row_threshold?: number
}): Promise<{ profile: ColumnProfile; candidates: CompactCandidate[] }> {
  const response = await request('/api/compact_job/candidates/build', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('压实候选生成失败')
  return await response.json()
}

export async function listCandidates(status?: string): Promise<CompactCandidate[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  const response = await request(`/api/compact_job/candidates${query}`)
  if (!response.ok) throw new Error('压实候选读取失败')
  return (await response.json()).items
}

export async function claimCandidate(candidateId: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/compact_job/candidates/${candidateId}/claim`, {
    method: 'POST',
    body: JSON.stringify({ claimed_by: '前端值班' }),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '领取失败')
  return payload
}

export async function verifyCandidate(candidateId: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/compact_job/candidates/${candidateId}/verify`, { method: 'POST' })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '复核失败')
  return payload
}

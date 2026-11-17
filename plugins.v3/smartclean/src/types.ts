export interface Envelope<T> { success: boolean; message: string; data: T | null }
export interface Api {
  get<T>(url: string, options?: { params?: Record<string, string | number>; feedback: 'silent' }): Promise<Envelope<T>>
  post<T>(url: string, data?: unknown, options?: { feedback: 'silent' }): Promise<Envelope<T>>
}

export type Combine = 'all' | 'any'
export type DeleteMode = 'source' | 'library' | 'all'

export interface Condition {
  field: string
  op: string
  value: string | number | boolean | string[]
}

export interface Rule {
  id: string
  name: string
  combine: Combine
  conditions: Condition[]
}

export interface LibraryAutomation {
  id: string
  server: string
  server_type: string
  library_id: string
  library_name: string
  library_kind: 'movie' | 'tv'
  enabled: boolean
  cron: string
  delete_mode: DeleteMode
  delay_days: number
  rule_id: string
  pending?: PendingItem[]
  history?: HistoryItem[]
}

export interface HitDetail {
  added_at?: string
  favorite?: boolean
  whitelisted?: boolean
  douban?: { stars: number | null; watched_on: string; title?: string; douban_id?: string } | null
  transfers?: { username: string; transferred_at: string }[]
  playback?: {
    has_data?: boolean
    unplayed: boolean
    last_played: string
    play_count: number | null
    episode_count: number
  }
  has_source?: boolean | null
  conditions?: Record<string, boolean>
}

export interface PendingItem {
  key: string
  title: string
  year: string
  season: number | null
  reason: string
  started_at?: string
  snapshot?: Record<string, unknown>
  detail?: HitDetail
  /** 命中时的全属性行，供自动化表展示 */
  attrs?: Partial<ScanRow>
  /** 命中的规则字段，用于列高亮 */
  hit_fields?: string[]
}

export interface HistoryItem {
  id: string
  title: string
  year: string
  season: number | null
  reason: string
  mode: string
  deleted_at: string
  message: string
  library_deleted: number
  source_deleted: number
  source_skipped: boolean
}

export interface ScanRow {
  key: string
  kind: string
  server?: string
  library_id?: string
  library_name?: string
  item_id?: string
  title: string
  year: string
  season: number | null
  tmdb?: string
  imdb?: string
  douban?: string
  favorite: boolean
  whitelisted: boolean
  has_source: boolean | null
  library_age_days: number | null
  transfer_users: string[]
  latest_transfer_at: string
  latest_transfer_user: string
  transfer_days: number | null
  transfer_origin?: 'moviepilot' | ''
  douban_stars: number | null
  douban_watched_on: string
  douban_watched_days: number | null
  douban_id?: string
  playback: HitDetail['playback']
  snapshot: Record<string, unknown>
  added_at?: string
}

export interface WhitelistItem {
  id: string
  kind: string
  title: string
  year: string
  season: number | null
  tmdb: string
  imdb: string
  douban_id: string
  server?: string
  library_id?: string
  item_id?: string
  link?: string
  description?: string
}

export interface Whitelist {
  manual: WhitelistItem[]
}

export interface EffectiveEntry {
  item: WhitelistItem
  sources: string[]
}

export interface LibraryOption {
  server: string
  server_type: string
  library_id: string
  library_name: string
  library_kind: 'movie' | 'tv'
}

export interface Overview {
  libraries: LibraryAutomation[]
  rules: Rule[]
  whitelist: Whitelist
  effective?: EffectiveEntry[]
  main_user: string
  douban_user: string
  enabled?: boolean
}

export interface RunResult {
  items: PendingItem[]
  pending: PendingItem[]
  errors: string[]
  deleted: { key: string; title: string; message: string }[]
  scanned?: number
}

export const FIELD_OPTIONS: { title: string; value: string; ops: string[] }[] = [
  { title: '片名', value: 'title', ops: ['contains', 'eq'] },
  { title: '年份', value: 'year', ops: ['eq', 'gte', 'lte'] },
  { title: '季号', value: 'season', ops: ['eq'] },
  { title: '豆瓣星级', value: 'douban_stars', ops: ['eq', 'lte', 'gte'] },
  { title: '豆瓣看过天数', value: 'douban_watched_days', ops: ['gte'] },
  { title: 'MP入库用户', value: 'transfer_users', ops: ['in'] },
  { title: '最近MP入库天数', value: 'transfer_days', ops: ['gte'] },
  { title: '在库天数', value: 'library_age_days', ops: ['gte'] },
  { title: '未播放', value: 'playback_unplayed', ops: ['eq'] },
  { title: '播放过期天数', value: 'playback_stale_days', ops: ['gte'] },
  { title: '收藏', value: 'favorite', ops: ['eq'] },
  { title: '有源文件', value: 'has_source', ops: ['eq'] },
]

export const OP_LABELS: Record<string, string> = {
  contains: '包含',
  eq: '等于',
  gte: '≥',
  lte: '≤',
  in: '属于',
}

function normImdb(value?: string): string {
  const text = String(value || '').trim().toLowerCase()
  if (!text) return ''
  if (text.startsWith('tt')) return text
  if (/^\d+$/.test(text)) return `tt${text}`
  return text
}

function itemMatchesRow(item: WhitelistItem, row: ScanRow): boolean {
  if (item.kind && item.kind !== row.kind) return false
  if (row.kind === 'season' && item.season != null && item.season !== row.season) return false
  const douban = row.douban_id || row.douban || ''
  if (item.tmdb && row.tmdb && item.tmdb === row.tmdb) return true
  if (item.imdb && row.imdb && normImdb(item.imdb) === normImdb(row.imdb)) return true
  if (item.douban_id && douban && item.douban_id === douban) return true
  if (
    !item.tmdb && !item.imdb && !item.douban_id
    && item.item_id && row.item_id && item.item_id === row.item_id
    && (!item.server || item.server === row.server)
    && (!item.library_id || item.library_id === row.library_id)
  ) return true
  return false
}

/** 前端轻量对齐：仅 manual。 */
export function rowIsWhitelisted(row: ScanRow, wl: Whitelist | null | undefined): boolean {
  if (!wl) return false
  return (wl.manual || []).some(item => itemMatchesRow(item, row))
}

export function identityKeyOf(item: Pick<WhitelistItem, 'kind' | 'season' | 'tmdb' | 'imdb' | 'douban_id' | 'server' | 'library_id' | 'item_id'>): string {
  const kind = item.kind || 'movie'
  const seasonPart = item.season == null ? '' : String(item.season)
  if (item.tmdb) return `${kind}|s${seasonPart}|tmdb:${item.tmdb}`
  if (item.imdb) return `${kind}|s${seasonPart}|imdb:${normImdb(item.imdb)}`
  if (item.douban_id) return `${kind}|s${seasonPart}|douban:${item.douban_id}`
  return `${kind}|s${seasonPart}|item:${item.server || ''}|${item.library_id || ''}|${item.item_id || ''}`
}

export function unwrap<T>(response: Envelope<T>): T {
  if (!response.success || response.data == null) throw new Error(response.message || '操作失败，请重试')
  return response.data
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请稍后重试'
}

export function labelOf(item: { title: string; year?: string; season?: number | null }): string {
  const year = item.year ? ` (${item.year})` : ''
  const season = item.season == null ? '' : ` 第${item.season}季`
  return `${item.title}${year}${season}`
}

export function blankCondition(): Condition {
  return { field: 'library_age_days', op: 'gte', value: 30 }
}

export function blankRule(): Rule {
  return { id: '', name: '', combine: 'all', conditions: [blankCondition()] }
}

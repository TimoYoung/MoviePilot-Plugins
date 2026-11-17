<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from 'vue'
import type {
  Api, Condition, EffectiveEntry, HistoryItem, LibraryAutomation, LibraryOption, Overview, PendingItem, Rule, RunResult, ScanRow,
  WhitelistItem,
} from '../types'
import {
  FIELD_OPTIONS, OP_LABELS, blankCondition, blankRule, errorMessage, identityKeyOf, labelOf, rowIsWhitelisted, unwrap,
} from '../types'

type HostConfirm = (options?: {
  type?: 'info' | 'warn' | 'error'
  title?: string
  content?: string
  confirmText?: string
  cancelText?: string
}) => Promise<boolean>

const props = defineProps<{
  api: Api
  pluginId: string
  hostSdk?: { ui?: { confirm?: HostConfirm } } | null
  show_switch?: boolean
}>()
const emit = defineEmits<{ close: []; switch: [] }>()
const silent = { feedback: 'silent' as const }
const url = (path: string) => `plugin/${props.pluginId}${path}`
const injectedConfirm = inject<HostConfirm | null>('moviepilot:confirm', null)

/** 插件内确认框：Cursor 内置浏览器会静默放行 window.confirm，不能当兜底。 */
const confirmOpen = ref(false)
const confirmTitle = ref('')
const confirmContent = ref('')
const confirmActionText = ref('确认删除')
let confirmResolver: ((ok: boolean) => void) | null = null

function settleConfirm(ok: boolean) {
  confirmOpen.value = false
  const resolve = confirmResolver
  confirmResolver = null
  resolve?.(ok)
}

async function askConfirm(title: string, content: string, confirmText = '确认删除'): Promise<boolean> {
  const hostConfirm = injectedConfirm || props.hostSdk?.ui?.confirm || null
  if (typeof hostConfirm === 'function') {
    try {
      return await hostConfirm({
        type: 'warn',
        title,
        content,
        confirmText,
        cancelText: '取消',
      })
    } catch {
      // 宿主确认失败时改用插件内弹层
    }
  }
  return await new Promise<boolean>((resolve) => {
    confirmTitle.value = title
    confirmContent.value = content
    confirmActionText.value = confirmText
    confirmResolver = resolve
    confirmOpen.value = true
  })
}

const tab = ref('libraries')
const overview = ref<Overview | null>(null)
const catalog = ref<LibraryOption[]>([])
const users = ref<string[]>([])
const errorText = ref('')
const notice = ref('')
const busy = ref(false)

const selectedLibKey = ref('')
const libMode = ref<'manual' | 'auto' | 'history'>('manual')
const autoDraft = ref<LibraryAutomation | null>(null)
type HistorySortKey = 'deleted_at' | 'title' | 'mode' | 'reason' | 'message'
const historySortKey = ref<HistorySortKey>('deleted_at')
const historySortDir = ref<'asc' | 'desc'>('desc')
const historyPage = ref(1)

interface ScanCacheEntry {
  rows: ScanRow[]
  errors: string[]
  scanned: number
  at: number
}
const scanCache = ref<Record<string, ScanCacheEntry>>({})
const scanRows = ref<ScanRow[]>([])
const scanErrors = ref<string[]>([])
const scanningKeys = ref<Record<string, boolean>>({})
const filterConditions = ref<Condition[]>([])
const filterCombine = ref<'all' | 'any'>('all')
const selectedKeys = ref<string[]>([])
const page = ref(1)
const pageSize = 50
type ColId =
  | 'title' | 'year' | 'douban_stars' | 'douban_watched'
  | 'transfer' | 'library' | 'playback' | 'favorite' | 'whitelisted' | 'has_source'
type PendingColId =
  | 'title' | 'year' | 'douban_stars' | 'douban_watched'
  | 'transfer' | 'library' | 'playback' | 'favorite' | 'has_source' | 'due'
type SortKey = ColId
const COLUMN_OPTIONS: { id: ColId; title: string; required?: boolean }[] = [
  { id: 'title', title: '片名', required: true },
  { id: 'year', title: '年/季' },
  { id: 'douban_stars', title: '豆瓣星级' },
  { id: 'douban_watched', title: '豆瓣看过' },
  { id: 'transfer', title: 'MP入库' },
  { id: 'library', title: '在库' },
  { id: 'playback', title: '最近播放' },
  { id: 'favorite', title: '收藏' },
  { id: 'whitelisted', title: '白名单' },
  { id: 'has_source', title: '有源' },
]
const PENDING_COLUMN_OPTIONS: { id: PendingColId; title: string; required?: boolean }[] = [
  { id: 'title', title: '片名', required: true },
  { id: 'year', title: '年/季' },
  { id: 'douban_stars', title: '豆瓣星级' },
  { id: 'douban_watched', title: '豆瓣看过' },
  { id: 'transfer', title: 'MP入库' },
  { id: 'library', title: '在库' },
  { id: 'playback', title: '最近播放' },
  { id: 'favorite', title: '收藏' },
  { id: 'has_source', title: '有源' },
  { id: 'due', title: '到期' },
]
/** 默认少显几列，避免默认分辨率下整页横向撑开 */
const DEFAULT_MANUAL_COLUMNS: ColId[] = [
  'title', 'year', 'douban_stars', 'douban_watched', 'transfer', 'library', 'playback',
]
const DEFAULT_PENDING_COLUMNS: PendingColId[] = [
  'title', 'year', 'douban_stars', 'douban_watched', 'transfer', 'library', 'playback', 'due',
]
const COLUMN_STORAGE_KEY = 'smartclean.manualColumns.v3'
const PENDING_COLUMN_STORAGE_KEY = 'smartclean.pendingColumns.v3'

function loadColumnPrefs<T extends string>(
  key: string,
  options: { id: T }[],
  defaults: T[],
): T[] {
  const all = options.map(item => item.id)
  const allowed = new Set(all)
  const fallback = defaults.filter(id => allowed.has(id))
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback.length ? fallback : all
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return fallback.length ? fallback : all
    const picked = parsed.filter((id: unknown): id is T => typeof id === 'string' && allowed.has(id as T))
    if (!picked.includes('title' as T)) picked.unshift('title' as T)
    return picked.length ? picked : (fallback.length ? fallback : all)
  } catch {
    return fallback.length ? fallback : all
  }
}

const visibleColumns = ref<ColId[]>(
  loadColumnPrefs(COLUMN_STORAGE_KEY, COLUMN_OPTIONS, DEFAULT_MANUAL_COLUMNS),
)
const pendingVisibleColumns = ref<PendingColId[]>(
  loadColumnPrefs(PENDING_COLUMN_STORAGE_KEY, PENDING_COLUMN_OPTIONS, DEFAULT_PENDING_COLUMNS),
)
const sortKey = ref<SortKey | ''>('')
const sortDir = ref<'asc' | 'desc'>('asc')
const saveRuleName = ref('')
const deleteMode = ref<'library' | 'source' | 'all'>('library')
const selectionByLib = ref<Record<string, string[]>>({})

function bindColumnPrefs<T extends string>(
  colsRef: { value: T[] },
  storageKey: string,
) {
  watch(colsRef, (cols) => {
    if (!cols.includes('title' as T)) {
      colsRef.value = ['title' as T, ...cols]
      return
    }
    try {
      localStorage.setItem(storageKey, JSON.stringify(cols))
    } catch { /* ignore quota */ }
  })
}

bindColumnPrefs(visibleColumns, COLUMN_STORAGE_KEY)
bindColumnPrefs(pendingVisibleColumns, PENDING_COLUMN_STORAGE_KEY)

function colVisible(id: ColId): boolean {
  return visibleColumns.value.includes(id)
}

function pendingColVisible(id: PendingColId): boolean {
  return pendingVisibleColumns.value.includes(id)
}

function manualColTitle(id: ColId): string {
  return COLUMN_OPTIONS.find(col => col.id === id)?.title || id
}

function pendingColTitle(id: PendingColId): string {
  return PENDING_COLUMN_OPTIONS.find(col => col.id === id)?.title || id
}

function toggleManualColumn(id: ColId) {
  if (id === 'title') return
  const on = new Set(visibleColumns.value)
  if (on.has(id)) on.delete(id)
  else on.add(id)
  on.add('title')
  visibleColumns.value = COLUMN_OPTIONS.map(col => col.id).filter(colId => on.has(colId))
}

function togglePendingColumn(id: PendingColId) {
  if (id === 'title') return
  const on = new Set(pendingVisibleColumns.value)
  if (on.has(id)) on.delete(id)
  else on.add(id)
  on.add('title')
  pendingVisibleColumns.value = PENDING_COLUMN_OPTIONS.map(col => col.id).filter(colId => on.has(colId))
}

const ruleDraft = ref<Rule>(blankRule())
const editingRuleId = ref('')

const pendingSelected = ref<string[]>([])
type PendingSortKey = SortKey | 'due'
const pendingSortKey = ref<PendingSortKey>('due')
const pendingSortDir = ref<'asc' | 'desc'>('asc')
const pendingPage = ref(1)
const wlSelectedKeys = ref<string[]>([])

const selectedCatalog = computed(() =>
  catalog.value.find(item => `${item.server}|${item.library_id}` === selectedLibKey.value) || null,
)

const automation = computed(() => {
  const lib = selectedCatalog.value
  if (!lib || !overview.value) return null
  return overview.value.libraries.find(item => item.server === lib.server && item.library_id === lib.library_id) || null
})

const ruleItems = computed(() => (overview.value?.rules || []).map(rule => ({ title: rule.name, value: rule.id })))

function sortValue(row: ScanRow, key: SortKey): string | number | boolean | null {
  switch (key) {
    case 'title':
      return (row.title || '').toLowerCase()
    case 'year':
      return (Number(row.year) || 0) * 100 + (row.season ?? 0)
    case 'douban_stars':
      return row.douban_stars
    case 'douban_watched':
      return row.douban_watched_on || null
    case 'transfer':
      return row.latest_transfer_at || null
    case 'library':
      return row.added_at || null
    case 'playback': {
      if (!row.playback?.has_data) return null
      if (row.playback.unplayed) return ''
      return row.playback.last_played || null
    }
    case 'favorite':
      return Boolean(row.favorite)
    case 'whitelisted':
      return Boolean(row.whitelisted)
    case 'has_source':
      return row.has_source
    default:
      return null
  }
}

function compareSort(a: ScanRow, b: ScanRow, key: SortKey, dir: 'asc' | 'desc'): number {
  const av = sortValue(a, key)
  const bv = sortValue(b, key)
  const aEmpty = av == null || av === ''
  const bEmpty = bv == null || bv === ''
  if (aEmpty && bEmpty) return 0
  if (aEmpty) return 1
  if (bEmpty) return -1
  let cmp = 0
  if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv
  else if (typeof av === 'boolean' && typeof bv === 'boolean') cmp = Number(av) - Number(bv)
  else cmp = String(av).localeCompare(String(bv), 'zh-CN')
  return dir === 'asc' ? cmp : -cmp
}

function toggleSort(key: SortKey) {
  if (sortKey.value === key) sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  else {
    sortKey.value = key
    sortDir.value = key === 'title' ? 'asc' : 'desc'
  }
  page.value = 1
}

function sortMark(key: SortKey): string {
  if (sortKey.value !== key) return ''
  return sortDir.value === 'asc' ? ' ↑' : ' ↓'
}

const filteredRows = computed(() => {
  const conditions = filterConditions.value
  let rows = scanRows.value
  if (conditions.length) {
    rows = rows.filter(row => {
      const hits = conditions.map(cond => matchRow(row, cond))
      return filterCombine.value === 'any' ? hits.some(Boolean) : hits.every(Boolean)
    })
  }
  if (!sortKey.value) return rows
  const key = sortKey.value
  const dir = sortDir.value
  return [...rows].sort((a, b) => compareSort(a, b, key, dir))
})

const totalPages = computed(() => Math.max(1, Math.ceil(filteredRows.value.length / pageSize)))
const pageRows = computed(() => {
  const start = (page.value - 1) * pageSize
  return filteredRows.value.slice(start, start + pageSize)
})

const allFilteredSelected = computed(() =>
  filteredRows.value.length > 0 && filteredRows.value.every(row => selectedKeys.value.includes(row.key)),
)
const someFilteredSelected = computed(() =>
  filteredRows.value.some(row => selectedKeys.value.includes(row.key)) && !allFilteredSelected.value,
)
const currentScanning = computed(() => Boolean(selectedLibKey.value && scanningKeys.value[selectedLibKey.value]))
const scanAgeText = computed(() => {
  const key = selectedLibKey.value
  const entry = key ? scanCache.value[key] : null
  if (!entry) return ''
  const seconds = Math.max(0, Math.round((Date.now() - entry.at) / 1000))
  if (seconds < 60) return `${seconds} 秒前`
  return `${Math.round(seconds / 60)} 分钟前`
})

watch(filteredRows, () => {
  if (page.value > totalPages.value) page.value = 1
})

const MODE_LABELS: Record<string, string> = {
  library: '仅媒体库',
  source: '仅源文件',
  all: '全部',
}

function modeLabel(mode: string): string {
  return MODE_LABELS[mode] || mode || '—'
}

/** 历史结果列：兼容旧记录里成功但 message 为空的情况。 */
function historyResult(item: HistoryItem): string {
  const raw = String(item.message || '').trim()
  if (raw) return raw
  const parts: string[] = []
  const libraryDeleted = Number(item.library_deleted || 0)
  const sourceDeleted = Number(item.source_deleted || 0)
  if (libraryDeleted === 1) parts.push('已删除媒体库')
  else if (libraryDeleted > 1) parts.push(`已删除媒体库（${libraryDeleted} 项）`)
  if (sourceDeleted === 1) parts.push('已删除源文件')
  else if (sourceDeleted > 1) parts.push(`已删除源文件（${sourceDeleted} 个）`)
  if (item.source_skipped) parts.push('无源文件，已跳过')
  return parts.length ? parts.join('；') : '无变更'
}

function historySortValue(item: HistoryItem, key: HistorySortKey): string | number | null {
  switch (key) {
    case 'deleted_at':
      return item.deleted_at || null
    case 'title':
      return labelOf(item).toLowerCase()
    case 'mode':
      return item.mode || ''
    case 'reason':
      return item.reason || ''
    case 'message':
      return historyResult(item).toLowerCase()
    default:
      return null
  }
}

function toggleHistorySort(key: HistorySortKey) {
  if (historySortKey.value === key) {
    historySortDir.value = historySortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    historySortKey.value = key
    historySortDir.value = key === 'title' ? 'asc' : 'desc'
  }
  historyPage.value = 1
}

function historySortMark(key: HistorySortKey): string {
  if (historySortKey.value !== key) return ''
  return historySortDir.value === 'asc' ? ' ↑' : ' ↓'
}

const historyRows = computed(() => {
  const rows = [...(automation.value?.history || [])]
  const key = historySortKey.value
  const dir = historySortDir.value
  rows.sort((a, b) => {
    const av = historySortValue(a, key)
    const bv = historySortValue(b, key)
    const aEmpty = av == null || av === ''
    const bEmpty = bv == null || bv === ''
    if (aEmpty && bEmpty) return 0
    if (aEmpty) return 1
    if (bEmpty) return -1
    let cmp = 0
    if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv
    else cmp = String(av).localeCompare(String(bv), 'zh-CN')
    return dir === 'asc' ? cmp : -cmp
  })
  return rows
})

const historyTotalPages = computed(() => Math.max(1, Math.ceil(historyRows.value.length / pageSize)))
const historyPageRows = computed(() => {
  const start = (historyPage.value - 1) * pageSize
  return historyRows.value.slice(start, start + pageSize)
})

watch(historyRows, () => {
  if (historyPage.value > historyTotalPages.value) historyPage.value = 1
})

async function clearHistory() {
  const id = automation.value?.id
  const count = historyRows.value.length
  if (!id || !count) return
  if (!(await askConfirm('确认清空', `将清空本媒体库的全部删除历史（${count} 条），不可恢复。确认继续？`, '确认清空'))) {
    return
  }
  await act(
    () => props.api.post(url('/libraries/history/clear'), { library_id: id, keys: [] }, silent).then(unwrap),
    '历史已清空',
  )
}

function matchRow(row: ScanRow, cond: Condition): boolean {
  const value = cond.value
  switch (cond.field) {
    case 'title': {
      const title = row.title || ''
      const needle = String(value || '')
      return cond.op === 'contains' ? title.toLowerCase().includes(needle.toLowerCase()) : title === needle
    }
    case 'year': {
      const year = Number(row.year || NaN)
      const n = Number(value)
      if (!Number.isFinite(year)) return false
      if (cond.op === 'eq') return year === n
      if (cond.op === 'gte') return year >= n
      return year <= n
    }
    case 'season':
      return row.season === Number(value)
    case 'douban_stars': {
      if (row.douban_stars == null) return false
      const n = Number(value)
      if (cond.op === 'eq') return row.douban_stars === n
      if (cond.op === 'gte') return row.douban_stars >= n
      return row.douban_stars <= n
    }
    case 'douban_watched_days':
      return row.douban_watched_days != null && row.douban_watched_days >= Number(value)
    case 'transfer_users': {
      const names = Array.isArray(value) ? value : [String(value)]
      return names.some(name => row.transfer_users?.includes(name))
    }
    case 'transfer_days':
      return row.transfer_days != null && row.transfer_days >= Number(value)
    case 'library_age_days':
      return row.library_age_days != null && row.library_age_days >= Number(value)
    case 'playback_unplayed':
      return Boolean(row.playback?.has_data) && row.playback?.unplayed === Boolean(value)
    case 'playback_stale_days': {
      if (!row.playback?.has_data) return false
      if (row.playback.unplayed) return true
      if (!row.playback.last_played) return false
      const last = Date.parse(row.playback.last_played)
      if (!Number.isFinite(last)) return false
      const days = (Date.now() - last) / 86400000
      return days >= Number(value)
    }
    case 'favorite':
      return Boolean(row.favorite) === Boolean(value)
    case 'has_source':
      return row.has_source === Boolean(value)
    default:
      return true
  }
}

async function load() {
  errorText.value = ''
  try {
    const [data, libs, names] = await Promise.all([
      unwrap(await props.api.get<Overview>(url('/overview'), silent)),
      unwrap(await props.api.get<{ libraries: LibraryOption[] }>(url('/libraries'), silent)),
      unwrap(await props.api.get<{ users: string[] }>(url('/users'), silent)),
    ])
    overview.value = data
    catalog.value = libs.libraries
    users.value = names.users
    syncAutoDraft()
  } catch (error) {
    errorText.value = errorMessage(error)
  }
}

function syncAutoDraft() {
  const lib = selectedCatalog.value
  if (!lib) {
    autoDraft.value = null
    return
  }
  const existing = automation.value
  autoDraft.value = existing
    ? JSON.parse(JSON.stringify(existing))
    : {
        id: '',
        server: lib.server,
        server_type: lib.server_type,
        library_id: lib.library_id,
        library_name: lib.library_name,
        library_kind: lib.library_kind,
        enabled: false,
        cron: '0 4 * * *',
        delete_mode: 'library',
        delay_days: 7,
        rule_id: '',
        pending: [],
        history: [],
      }
}

function applyCacheToView(key: string) {
  const cached = scanCache.value[key]
  if (cached) {
    scanRows.value = cached.rows
    scanErrors.value = cached.errors
  } else {
    scanRows.value = []
    scanErrors.value = []
  }
  selectedKeys.value = [...(selectionByLib.value[key] || [])]
  page.value = 1
}

watch(selectedLibKey, (key, prev) => {
  if (prev) selectionByLib.value[prev] = [...selectedKeys.value]
  pendingSelected.value = []
  pendingPage.value = 1
  historyPage.value = 1
  syncAutoDraft()
  if (!key) {
    scanRows.value = []
    scanErrors.value = []
    selectedKeys.value = []
    return
  }
  applyCacheToView(key)
})

watch(selectedKeys, (keys) => {
  if (!selectedLibKey.value) return
  selectionByLib.value[selectedLibKey.value] = [...keys]
})

onMounted(load)

async function act(action: () => Promise<unknown>, text: string, opts?: { recomputeWhitelist?: boolean }) {
  if (busy.value) return
  busy.value = true
  errorText.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = text
    await load()
    if (opts?.recomputeWhitelist) recomputeWhitelistedFlags()
  } catch (error) {
    errorText.value = errorMessage(error)
  } finally {
    busy.value = false
  }
}

async function scanLibrary(force = false) {
  const lib = selectedCatalog.value
  if (!lib) return
  const key = `${lib.server}|${lib.library_id}`
  if (!force && scanCache.value[key]?.rows.length) {
    applyCacheToView(key)
    return
  }
  if (scanningKeys.value[key] && !force) return

  scanningKeys.value = { ...scanningKeys.value, [key]: true }
  errorText.value = ''
  if (force) notice.value = ''
  try {
    const data = unwrap(await props.api.post<{ rows: ScanRow[]; errors: string[]; scanned: number; cached?: boolean }>(
      url('/libraries/scan'),
      {
        server: lib.server,
        server_type: lib.server_type,
        library_id: lib.library_id,
        library_name: lib.library_name,
        library_kind: lib.library_kind,
        force,
      },
      silent,
    ))
    scanCache.value = {
      ...scanCache.value,
      [key]: { rows: data.rows, errors: data.errors || [], scanned: data.scanned, at: Date.now() },
    }
    // 用户已切走：只写入缓存，不抢当前视图；切回来时 watch 会套用缓存
    if (selectedLibKey.value === key) {
      applyCacheToView(key)
      notice.value = data.cached ? `已用缓存 ${data.scanned} 项` : `已扫描 ${data.scanned} 项`
    }
  } catch (error) {
    if (selectedLibKey.value === key) errorText.value = errorMessage(error)
  } finally {
    const next = { ...scanningKeys.value }
    delete next[key]
    scanningKeys.value = next
  }
}

function enterLibrary(lib: LibraryOption) {
  const key = `${lib.server}|${lib.library_id}`
  selectedLibKey.value = key
  libMode.value = 'manual'
  // 有缓存立刻展示；无缓存后台扫，不挡住切到别的库
  void scanLibrary(false)
}

function toggleSelectAll() {
  if (allFilteredSelected.value) {
    const drop = new Set(filteredRows.value.map(row => row.key))
    selectedKeys.value = selectedKeys.value.filter(key => !drop.has(key))
  } else {
    const set = new Set(selectedKeys.value)
    for (const row of filteredRows.value) set.add(row.key)
    selectedKeys.value = [...set]
  }
}

function addFilter() {
  filterConditions.value.push(blankCondition())
}

function opsFor(field: string) {
  return FIELD_OPTIONS.find(item => item.value === field)?.ops || ['eq']
}

function onFieldChange(cond: Condition) {
  const ops = opsFor(cond.field)
  if (!ops.includes(cond.op)) cond.op = ops[0]
  if (cond.field === 'transfer_users') cond.value = []
  else if (['playback_unplayed', 'favorite', 'has_source'].includes(cond.field)) cond.value = true
  else if (cond.field === 'title') cond.value = ''
  else cond.value = 30
}

async function saveFiltersAsRule() {
  if (!filterConditions.value.length) {
    errorText.value = '请先添加筛选条件'
    return
  }
  const name = saveRuleName.value.trim() || `规则 ${new Date().toLocaleString()}`
  await act(async () => {
    unwrap(await props.api.post(url('/rules'), {
      id: '',
      name,
      combine: filterCombine.value,
      conditions: filterConditions.value,
    }, silent))
    saveRuleName.value = ''
  }, '规则已保存')
}

function patchCachedRows(mutate: (row: ScanRow) => void, libKey = selectedLibKey.value) {
  if (libKey && libKey === selectedLibKey.value) {
    for (const row of scanRows.value) mutate(row)
    scanRows.value = [...scanRows.value]
  }
  if (!libKey) return
  const cached = scanCache.value[libKey]
  if (!cached) return
  for (const row of cached.rows) mutate(row)
  scanCache.value = { ...scanCache.value, [libKey]: { ...cached, rows: [...cached.rows] } }
}

function removeCachedRows(keys: Set<string>, libKey = selectedLibKey.value) {
  if (!libKey || !keys.size) return
  if (libKey === selectedLibKey.value) {
    scanRows.value = scanRows.value.filter(row => !keys.has(row.key))
  }
  const cached = scanCache.value[libKey]
  if (!cached) return
  scanCache.value = {
    ...scanCache.value,
    [libKey]: { ...cached, rows: cached.rows.filter(row => !keys.has(row.key)), scanned: cached.rows.filter(row => !keys.has(row.key)).length },
  }
}

function recomputeWhitelistedFlags() {
  const wl = overview.value?.whitelist
  const next: Record<string, ScanCacheEntry> = {}
  for (const [key, entry] of Object.entries(scanCache.value)) {
    const rows = entry.rows.map(row => ({ ...row, whitelisted: rowIsWhitelisted(row, wl) }))
    next[key] = { ...entry, rows }
  }
  scanCache.value = next
  if (selectedLibKey.value && next[selectedLibKey.value]) {
    scanRows.value = next[selectedLibKey.value].rows
  }
}

async function batchWhitelist() {
  const items = filteredRows.value.filter(row => selectedKeys.value.includes(row.key))
  if (!items.length) return
  const keys = new Set(items.map(row => row.key))
  await act(async () => {
    unwrap(await props.api.post(url('/libraries/whitelist'), { items }, silent))
    selectedKeys.value = []
    patchCachedRows((row) => {
      if (keys.has(row.key)) row.whitelisted = true
    })
  }, '已加入白名单')
}

async function batchDelete() {
  const lib = selectedCatalog.value
  if (!lib) return
  const items = filteredRows.value.filter(row => selectedKeys.value.includes(row.key))
  if (!items.length) return
  if (!(await askConfirm('确认删除', `将立即删除 ${items.length} 项，不可撤销。确认继续？`))) return
  const mode = deleteMode.value
  busy.value = true
  errorText.value = ''
  notice.value = ''
  try {
    const result = unwrap(await props.api.post<{
      deleted: { key: string; title?: string; message?: string }[]
      skipped: { key: string; title?: string; reason?: string }[]
    }>(
      url('/libraries/delete'),
      {
        server: lib.server,
        server_type: lib.server_type,
        library_id: lib.library_id,
        library_name: lib.library_name,
        library_kind: lib.library_kind,
        delete_mode: mode,
        items,
      },
      silent,
    ))
    selectedKeys.value = []
    const deletedKeys = new Set((result.deleted || []).map(item => item.key))
    if (mode === 'source') {
      patchCachedRows((row) => {
        if (deletedKeys.has(row.key)) row.has_source = false
      })
    } else {
      removeCachedRows(deletedKeys)
    }
    const deletedN = result.deleted?.length || 0
    const skipped = result.skipped || []
    // 仅失败时强制重扫核对；成功项已改本地缓存，不必整库重扫
    if (skipped.length) await scanLibrary(true)
    await load()
    if (skipped.length) {
      const reasons = [...new Set(skipped.map(item => item.reason || '跳过'))].join('；')
      const msg = `已删除 ${deletedN} 项，失败/跳过 ${skipped.length} 项：${reasons}`
      if (deletedN === 0) errorText.value = msg
      else notice.value = msg
    } else {
      notice.value = `已删除 ${deletedN} 项`
    }
  } catch (error) {
    errorText.value = errorMessage(error)
  } finally {
    busy.value = false
  }
}

async function saveAutomation() {
  if (!autoDraft.value) return
  await act(async () => {
    const saved = unwrap(await props.api.post<LibraryAutomation>(url('/libraries/automation'), autoDraft.value, silent))
    autoDraft.value = saved
  }, '自动化已保存')
}

async function runAutomation() {
  if (!autoDraft.value) return
  if (!(await askConfirm(
    '执行一次',
    '将按当前规则扫描并整表替换待删除清单（不删除媒体）。确认继续？',
    '执行一次',
  ))) return
  busy.value = true
  errorText.value = ''
  notice.value = ''
  try {
    const saved = unwrap(await props.api.post<LibraryAutomation>(url('/libraries/automation'), autoDraft.value, silent))
    autoDraft.value = saved
    const result = unwrap(await props.api.post<RunResult>(url('/libraries/run'), { library_id: saved.id, keys: [] }, silent))
    pendingSelected.value = []
    pendingPage.value = 1
    notice.value = `已扫描 ${result.scanned || 0} 项，待删除 ${result.pending?.length ?? result.items?.length ?? 0} 项`
    await load()
    syncAutoDraft()
  } catch (error) {
    errorText.value = errorMessage(error)
  } finally {
    busy.value = false
  }
}

async function pendingAction(
  path: string,
  keys: string[],
  text: string,
  confirm: { title: string; content: string; action: string },
) {
  const id = automation.value?.id
  if (!id || !keys.length) return
  if (!(await askConfirm(confirm.title, confirm.content, confirm.action))) return
  await act(async () => {
    unwrap(await props.api.post(url(path), { library_id: id, keys }, silent))
    pendingSelected.value = []
  }, text, { recomputeWhitelist: path.includes('whitelist') })
}

async function pendingDeleteNow() {
  const n = pendingSelected.value.length
  await pendingAction(
    '/libraries/delete-now',
    pendingSelected.value,
    '已删除',
    { title: '立即删除', content: `将立即删除选中的 ${n} 项，不可撤销。确认继续？`, action: '立即删除' },
  )
}

async function pendingRemove() {
  const n = pendingSelected.value.length
  await pendingAction(
    '/libraries/remove',
    pendingSelected.value,
    '已移出待删除',
    { title: '移出待删除', content: `将从待删除清单移出 ${n} 项（不删媒体）。确认继续？`, action: '移出' },
  )
}

async function pendingWhitelist() {
  const n = pendingSelected.value.length
  await pendingAction(
    '/libraries/pending/whitelist',
    pendingSelected.value,
    '已加入白名单',
    { title: '加入白名单', content: `将选中的 ${n} 项加入白名单并移出待删除。确认继续？`, action: '加入白名单' },
  )
}

function editRule(rule: Rule) {
  editingRuleId.value = rule.id
  ruleDraft.value = JSON.parse(JSON.stringify(rule))
  tab.value = 'rules'
}

function newRule() {
  editingRuleId.value = ''
  ruleDraft.value = blankRule()
}

async function saveRule() {
  await act(async () => {
    const saved = unwrap(await props.api.post<Rule>(url('/rules'), {
      ...ruleDraft.value,
      id: editingRuleId.value || '',
    }, silent))
    editingRuleId.value = saved.id
    ruleDraft.value = saved
  }, '规则已保存')
}

async function deleteRule(ruleId: string) {
  await act(async () => {
    unwrap(await props.api.post(url('/rules/delete'), { rule_id: ruleId }, silent))
    if (editingRuleId.value === ruleId) newRule()
  }, '规则已删除')
}

const effectiveEntries = computed(() => overview.value?.effective || [])
const libraryIndex = computed(() => {
  const index = new Map<string, string[]>()
  for (const entry of Object.values(scanCache.value)) {
    for (const row of entry.rows) {
      const keys = [
        row.tmdb ? `tmdb:${row.tmdb}` : '',
        row.imdb ? `imdb:${String(row.imdb).toLowerCase()}` : '',
        (row.douban_id || row.douban) ? `douban:${row.douban_id || row.douban}` : '',
      ].filter(Boolean)
      const label = `${row.server || ''} / ${row.library_name || row.library_id || ''}`
      for (const key of keys) {
        const list = index.get(key) || []
        if (!list.includes(label)) list.push(label)
        index.set(key, list)
      }
    }
  }
  return index
})

function inLibraryText(item: WhitelistItem): string {
  const keys = [
    item.tmdb ? `tmdb:${item.tmdb}` : '',
    item.imdb ? `imdb:${String(item.imdb).toLowerCase()}` : '',
    item.douban_id ? `douban:${item.douban_id}` : '',
  ].filter(Boolean)
  const found = new Set<string>()
  for (const key of keys) {
    for (const label of libraryIndex.value.get(key) || []) found.add(label)
  }
  if (!found.size) return Object.keys(scanCache.value).length ? '未在缓存库中' : '—'
  return [...found].join('；')
}

function entryKey(entry: EffectiveEntry): string {
  return identityKeyOf(entry.item)
}

function toggleAllEffective(ev: Event) {
  const on = (ev.target as HTMLInputElement).checked
  wlSelectedKeys.value = on ? effectiveEntries.value.map(entryKey) : []
}

async function excludeEffective() {
  const chosen = effectiveEntries.value.filter(entry => wlSelectedKeys.value.includes(entryKey(entry)))
  if (!chosen.length) return
  await act(async () => {
    unwrap(await props.api.post(url('/whitelist/exclude'), { items: chosen.map(entry => entry.item) }, silent))
    wlSelectedKeys.value = []
  }, '已移出白名单', { recomputeWhitelist: true })
}

function formatTime(value?: string | null) {
  if (!value) return ''
  const text = String(value).trim().replace('T', ' ')
  // 保留到秒：2024-02-15 18:55:07
  return text.length >= 19 ? text.slice(0, 19) : text
}

function playbackText(row: ScanRow) {
  const play = row.playback
  if (!play?.has_data) return '无播放数据'
  if (play.unplayed) return '未播放'
  return formatTime(play.last_played) || '已播放'
}

const FIELD_TO_COL: Record<string, ColId> = {
  title: 'title',
  year: 'year',
  season: 'year',
  douban_stars: 'douban_stars',
  douban_watched_days: 'douban_watched',
  transfer_users: 'transfer',
  transfer_days: 'transfer',
  library_age_days: 'library',
  playback_unplayed: 'playback',
  playback_stale_days: 'playback',
  favorite: 'favorite',
  has_source: 'has_source',
}

type PendingRow = ScanRow & {
  reason: string
  started_at: string
  hit_fields: string[]
  due_at: string
  is_due: boolean
}

function delayDaysOf(): number {
  return Number(autoDraft.value?.delay_days ?? automation.value?.delay_days ?? 0)
}

function dueAtIso(startedAt: string | undefined, delayDays: number): string {
  if (!startedAt) return ''
  const start = Date.parse(startedAt)
  if (Number.isNaN(start)) return ''
  return new Date(start + Math.max(delayDays, 0) * 86400000).toISOString().slice(0, 19)
}

function pendingIsDue(startedAt: string | undefined, delayDays: number, nowMs = Date.now()): boolean {
  if (delayDays <= 0) return true
  const start = startedAt ? Date.parse(startedAt) : NaN
  if (Number.isNaN(start)) return false
  return nowMs >= start + delayDays * 86400000
}

function pendingToRow(item: PendingItem, delayDays: number): PendingRow {
  const attrs = item.attrs || {}
  const playback = attrs.playback || item.detail?.playback || {
    has_data: false,
    unplayed: false,
    last_played: '',
    play_count: null,
    episode_count: 0,
  }
  return {
    key: item.key,
    kind: String(attrs.kind || (item.season != null ? 'season' : 'movie')),
    title: item.title || attrs.title || '',
    year: item.year || attrs.year || '',
    season: item.season ?? attrs.season ?? null,
    favorite: Boolean(attrs.favorite),
    whitelisted: Boolean(attrs.whitelisted),
    has_source: attrs.has_source ?? null,
    library_age_days: attrs.library_age_days ?? null,
    transfer_users: attrs.transfer_users || [],
    latest_transfer_at: attrs.latest_transfer_at || '',
    latest_transfer_user: attrs.latest_transfer_user || '',
    transfer_days: attrs.transfer_days ?? null,
    transfer_origin: attrs.transfer_origin || '',
    douban_stars: attrs.douban_stars ?? null,
    douban_watched_on: attrs.douban_watched_on || '',
    douban_watched_days: attrs.douban_watched_days ?? null,
    douban_id: attrs.douban_id || '',
    playback,
    snapshot: (attrs.snapshot || item.snapshot || {}) as Record<string, unknown>,
    added_at: attrs.added_at || '',
    reason: item.reason || '',
    started_at: item.started_at || '',
    hit_fields: item.hit_fields || [],
    due_at: dueAtIso(item.started_at, delayDays),
    is_due: pendingIsDue(item.started_at, delayDays),
  }
}

function hitColsOf(row: PendingRow): Set<ColId> {
  const cols = new Set<ColId>()
  for (const field of row.hit_fields || []) {
    const col = FIELD_TO_COL[field]
    if (col) cols.add(col)
  }
  return cols
}

function cellHit(row: PendingRow, col: ColId): boolean {
  return hitColsOf(row).has(col)
}

function togglePendingSort(key: PendingSortKey) {
  if (pendingSortKey.value === key) {
    pendingSortDir.value = pendingSortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    pendingSortKey.value = key
    pendingSortDir.value = key === 'title' ? 'asc' : 'asc'
  }
  pendingPage.value = 1
}

function pendingSortMark(key: PendingSortKey): string {
  if (pendingSortKey.value !== key) return ''
  return pendingSortDir.value === 'asc' ? ' ↑' : ' ↓'
}

const pendingRows = computed(() => {
  const delay = delayDaysOf()
  const rows = (automation.value?.pending || []).map(item => pendingToRow(item, delay))
  const key = pendingSortKey.value
  const dir = pendingSortDir.value
  return [...rows].sort((a, b) => {
    if (key === 'due') {
      const av = a.due_at || ''
      const bv = b.due_at || ''
      const cmp = av.localeCompare(bv)
      return dir === 'asc' ? cmp : -cmp
    }
    return compareSort(a, b, key, dir)
  })
})

const pendingTotalPages = computed(() => Math.max(1, Math.ceil(pendingRows.value.length / pageSize)))
const pendingPageRows = computed(() => {
  const start = (pendingPage.value - 1) * pageSize
  return pendingRows.value.slice(start, start + pageSize)
})

const allPendingSelected = computed(() =>
  pendingRows.value.length > 0 && pendingRows.value.every(row => pendingSelected.value.includes(row.key)),
)
const somePendingSelected = computed(() =>
  pendingRows.value.some(row => pendingSelected.value.includes(row.key)) && !allPendingSelected.value,
)

function toggleSelectAllPending() {
  if (allPendingSelected.value) pendingSelected.value = []
  else pendingSelected.value = pendingRows.value.map(row => row.key)
}

watch(pendingRows, () => {
  if (pendingPage.value > pendingTotalPages.value) pendingPage.value = 1
})
</script>

<template>
  <div class="smartclean">
    <header class="smartclean-header">
      <strong>智能清理</strong>
      <span class="smartclean-meta">按媒体库清理</span>
      <div class="smartclean-header-actions">
        <VBtn
          v-if="show_switch"
          icon="mdi-cog-outline"
          variant="text"
          size="small"
          aria-label="插件设置"
          @click="emit('switch')"
        />
        <VBtn icon="mdi-close" variant="text" size="small" aria-label="关闭" @click="emit('close')" />
      </div>
    </header>
    <VDialog v-model="confirmOpen" max-width="28rem" persistent>
      <VCard>
        <VCardTitle class="text-wrap">{{ confirmTitle }}</VCardTitle>
        <VCardText>{{ confirmContent }}</VCardText>
        <VCardActions>
          <VSpacer />
          <VBtn variant="text" @click="settleConfirm(false)">取消</VBtn>
          <VBtn color="error" variant="flat" @click="settleConfirm(true)">{{ confirmActionText }}</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>
    <VAlert v-if="errorText" type="error" variant="tonal" class="mb-3">{{ errorText }}</VAlert>
    <VAlert v-if="notice" type="success" variant="tonal" class="mb-3">{{ notice }}</VAlert>

    <VTabs v-model="tab" density="compact" class="mb-3">
      <VTab value="libraries">媒体库</VTab>
      <VTab value="rules">规则</VTab>
      <VTab value="whitelist">白名单</VTab>
    </VTabs>

    <div v-if="tab === 'libraries'" class="smartclean-layout">
      <aside class="smartclean-side">
        <h3>媒体库</h3>
        <button
          v-for="lib in catalog"
          :key="`${lib.server}|${lib.library_id}`"
          type="button"
          class="smartclean-lib"
          :class="{ active: selectedLibKey === `${lib.server}|${lib.library_id}` }"
          @click="enterLibrary(lib)"
        >
          <strong>{{ lib.library_name }}</strong>
          <span>
            {{ lib.server }} · {{ lib.library_kind === 'movie' ? '电影' : '剧集' }}
            <template v-if="scanningKeys[`${lib.server}|${lib.library_id}`]"> · 扫描中</template>
            <template v-else-if="scanCache[`${lib.server}|${lib.library_id}`]"> · 已缓存</template>
          </span>
        </button>
        <p v-if="!catalog.length" class="smartclean-meta">未发现已连接的 Emby / Jellyfin 电影或剧集库。</p>
      </aside>

      <section v-if="selectedCatalog" class="smartclean-main">
        <div class="smartclean-toolbar">
          <h3>{{ selectedCatalog.library_name }}</h3>
          <VBtnToggle v-model="libMode" mandatory density="compact" color="primary">
            <VBtn value="manual" size="small">手动</VBtn>
            <VBtn value="auto" size="small">自动化</VBtn>
            <VBtn value="history" size="small">历史</VBtn>
          </VBtnToggle>
        </div>

        <template v-if="libMode === 'manual'">
          <VAlert v-if="currentScanning" type="info" variant="tonal" class="mb-2" density="compact">
            正在扫描「{{ selectedCatalog.library_name }}」… 可先切换其他媒体库，结果会在后台写入缓存。
          </VAlert>

          <div class="smartclean-bar">
            <div class="smartclean-bar-group">
              <VBtn size="small" variant="tonal" :disabled="currentScanning" :loading="currentScanning" @click="scanLibrary(true)">刷新扫描</VBtn>
              <span class="smartclean-meta">
                已选 {{ selectedKeys.length }} · 筛选 {{ filteredRows.length }} · 全库 {{ scanRows.length }}
                <template v-if="scanAgeText"> · 缓存 {{ scanAgeText }}</template>
              </span>
            </div>
            <div class="smartclean-bar-group smartclean-bar-group--end">
              <VMenu :close-on-content-click="false">
                <template #activator="{ props: menuProps }">
                  <VBtn v-bind="menuProps" size="small" variant="text" prepend-icon="mdi-view-column-outline">
                    列 {{ visibleColumns.length }}/{{ COLUMN_OPTIONS.length }}
                  </VBtn>
                </template>
                <VList density="compact" class="smartclean-col-menu">
                  <VListItem
                    v-for="col in COLUMN_OPTIONS"
                    :key="col.id"
                    :disabled="col.required"
                    @click="toggleManualColumn(col.id)"
                  >
                    <template #prepend>
                      <VCheckboxBtn :model-value="colVisible(col.id)" :disabled="col.required" />
                    </template>
                    <VListItemTitle>{{ col.title }}</VListItemTitle>
                  </VListItem>
                </VList>
              </VMenu>
              <VSelect
                v-model="deleteMode"
                :items="[
                  { title: '仅媒体库', value: 'library' },
                  { title: '仅源文件', value: 'source' },
                  { title: '全部', value: 'all' },
                ]"
                label="删除方式"
                density="compact"
                hide-details
                style="max-width: 140px"
              />
              <VBtn size="small" variant="tonal" :disabled="busy || currentScanning || !selectedKeys.length" @click="batchWhitelist">加白名单</VBtn>
              <VBtn size="small" color="error" :disabled="busy || currentScanning || !selectedKeys.length" @click="batchDelete">删除选中</VBtn>
            </div>
          </div>

          <div class="smartclean-bar smartclean-bar--filters">
            <div class="smartclean-bar-group">
              <span class="smartclean-bar-label">筛选</span>
              <VSelect
                v-model="filterCombine"
                :items="[{ title: '全部满足', value: 'all' }, { title: '任一满足', value: 'any' }]"
                density="compact"
                hide-details
                style="max-width: 128px"
              />
              <VBtn size="small" variant="text" @click="addFilter">加条件</VBtn>
            </div>
            <div class="smartclean-bar-group smartclean-bar-group--end">
              <VTextField
                v-model="saveRuleName"
                label="规则名称"
                density="compact"
                hide-details
                class="smartclean-rule-name"
              />
              <VBtn size="small" variant="text" :disabled="busy || !filterConditions.length" @click="saveFiltersAsRule">保存为规则</VBtn>
            </div>
          </div>
          <div v-for="(cond, index) in filterConditions" :key="index" class="smartclean-bar smartclean-bar--cond">
            <VSelect
              v-model="cond.field"
              :items="FIELD_OPTIONS"
              item-title="title"
              item-value="value"
              label="字段"
              density="compact"
              hide-details
              style="max-width: 160px"
              @update:model-value="onFieldChange(cond)"
            />
            <VSelect
              v-model="cond.op"
              :items="opsFor(cond.field).map(op => ({ title: OP_LABELS[op] || op, value: op }))"
              label="运算"
              density="compact"
              hide-details
              style="max-width: 120px"
            />
            <VSelect
              v-if="cond.field === 'transfer_users'"
              v-model="cond.value"
              :items="users"
              label="用户"
              multiple
              chips
              density="compact"
              hide-details
              style="min-width: 200px"
            />
            <VSelect
              v-else-if="['playback_unplayed', 'favorite', 'has_source'].includes(cond.field)"
              v-model="cond.value"
              :items="[{ title: '是', value: true }, { title: '否', value: false }]"
              label="值"
              density="compact"
              hide-details
              style="max-width: 120px"
            />
            <VTextField
              v-else
              v-model="cond.value"
              label="值"
              density="compact"
              hide-details
              style="max-width: 160px"
            />
            <VBtn size="small" variant="text" @click="filterConditions.splice(index, 1)">移除</VBtn>
          </div>

          <VAlert v-for="(err, i) in scanErrors" :key="i" type="warning" variant="tonal" class="mb-2">{{ err }}</VAlert>

          <div class="smartclean-table-wrap">
            <table class="smartclean-table">
              <thead>
                <tr>
                  <th>
                    <input
                      type="checkbox"
                      :checked="allFilteredSelected"
                      :indeterminate.prop="someFilteredSelected"
                      @change="toggleSelectAll"
                    >
                  </th>
                  <th v-if="colVisible('title')" class="smartclean-sortable" @click="toggleSort('title')">{{ manualColTitle('title') }}{{ sortMark('title') }}</th>
                  <th v-if="colVisible('year')" class="smartclean-sortable" @click="toggleSort('year')">{{ manualColTitle('year') }}{{ sortMark('year') }}</th>
                  <th v-if="colVisible('douban_stars')" class="smartclean-sortable" @click="toggleSort('douban_stars')">{{ manualColTitle('douban_stars') }}{{ sortMark('douban_stars') }}</th>
                  <th v-if="colVisible('douban_watched')" class="smartclean-sortable" @click="toggleSort('douban_watched')">{{ manualColTitle('douban_watched') }}{{ sortMark('douban_watched') }}</th>
                  <th v-if="colVisible('transfer')" class="smartclean-sortable" @click="toggleSort('transfer')">{{ manualColTitle('transfer') }}{{ sortMark('transfer') }}</th>
                  <th v-if="colVisible('library')" class="smartclean-sortable" @click="toggleSort('library')">{{ manualColTitle('library') }}{{ sortMark('library') }}</th>
                  <th v-if="colVisible('playback')" class="smartclean-sortable" @click="toggleSort('playback')">{{ manualColTitle('playback') }}{{ sortMark('playback') }}</th>
                  <th v-if="colVisible('favorite')" class="smartclean-sortable" @click="toggleSort('favorite')">{{ manualColTitle('favorite') }}{{ sortMark('favorite') }}</th>
                  <th v-if="colVisible('whitelisted')" class="smartclean-sortable" @click="toggleSort('whitelisted')">{{ manualColTitle('whitelisted') }}{{ sortMark('whitelisted') }}</th>
                  <th v-if="colVisible('has_source')" class="smartclean-sortable" @click="toggleSort('has_source')">{{ manualColTitle('has_source') }}{{ sortMark('has_source') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in pageRows" :key="`${row.key}|${row.item_id}|${row.year}`">
                  <td>
                    <input v-model="selectedKeys" type="checkbox" :value="row.key">
                  </td>
                  <td v-if="colVisible('title')">{{ row.title }}</td>
                  <td v-if="colVisible('year')">{{ row.year }}<template v-if="row.season != null"> / S{{ row.season }}</template></td>
                  <td v-if="colVisible('douban_stars')">{{ row.douban_stars != null ? `${row.douban_stars}星` : '—' }}</td>
                  <td v-if="colVisible('douban_watched')">
                    <template v-if="row.douban_watched_on">
                      <div>{{ row.douban_watched_days != null ? `${row.douban_watched_days}天前` : '看过' }}</div>
                      <div class="smartclean-meta">{{ formatTime(row.douban_watched_on) }}</div>
                    </template>
                    <template v-else>—</template>
                  </td>
                  <td v-if="colVisible('transfer')">
                    <div>{{ (row.transfer_users || []).join('、') || '—' }}</div>
                    <div class="smartclean-meta">{{ formatTime(row.latest_transfer_at) }}</div>
                  </td>
                  <td v-if="colVisible('library')">
                    <div>{{ row.library_age_days ?? '—' }}天</div>
                    <div class="smartclean-meta">{{ formatTime(row.added_at) }}</div>
                  </td>
                  <td v-if="colVisible('playback')">{{ playbackText(row) }}</td>
                  <td v-if="colVisible('favorite')">{{ row.favorite ? '是' : '' }}</td>
                  <td v-if="colVisible('whitelisted')">{{ row.whitelisted ? '是' : '' }}</td>
                  <td v-if="colVisible('has_source')">{{ row.has_source ? '是' : (row.has_source === false ? '否' : '—') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="smartclean-toolbar">
            <VBtn size="small" :disabled="page <= 1" @click="page -= 1">上一页</VBtn>
            <span class="smartclean-meta">{{ page }} / {{ totalPages }}</span>
            <VBtn size="small" :disabled="page >= totalPages" @click="page += 1">下一页</VBtn>
          </div>
        </template>

        <template v-else-if="libMode === 'auto' && autoDraft">
          <div class="smartclean-form">
            <VSwitch v-model="autoDraft.enabled" label="启用自动化" hide-details />
            <VCronField v-model="autoDraft.cron" label="定时" />
            <VSelect
              v-model="autoDraft.delete_mode"
              :items="[
                { title: '仅媒体库', value: 'library' },
                { title: '仅源文件', value: 'source' },
                { title: '全部', value: 'all' },
              ]"
              label="删除方式"
              density="compact"
            />
            <VTextField v-model.number="autoDraft.delay_days" type="number" label="延迟删除天数" density="compact" />
            <VSelect
              v-model="autoDraft.rule_id"
              :items="[{ title: '（未绑定）', value: '' }, ...ruleItems]"
              label="绑定规则"
              density="compact"
            />
            <div class="smartclean-toolbar">
              <VBtn color="primary" :disabled="busy" @click="saveAutomation">保存</VBtn>
              <VBtn :disabled="busy" @click="runAutomation">执行一次</VBtn>
            </div>
          </div>

          <div class="smartclean-bar">
            <div class="smartclean-bar-group">
              <h4 class="smartclean-bar-title">待删除</h4>
              <span class="smartclean-meta">共 {{ pendingRows.length }} 条 · 已选 {{ pendingSelected.length }}</span>
            </div>
            <div class="smartclean-bar-group smartclean-bar-group--end">
              <VMenu :close-on-content-click="false">
                <template #activator="{ props: menuProps }">
                  <VBtn v-bind="menuProps" size="small" variant="text" prepend-icon="mdi-view-column-outline">
                    列 {{ pendingVisibleColumns.length }}/{{ PENDING_COLUMN_OPTIONS.length }}
                  </VBtn>
                </template>
                <VList density="compact" class="smartclean-col-menu">
                  <VListItem
                    v-for="col in PENDING_COLUMN_OPTIONS"
                    :key="col.id"
                    :disabled="col.required"
                    @click="togglePendingColumn(col.id)"
                  >
                    <template #prepend>
                      <VCheckboxBtn :model-value="pendingColVisible(col.id)" :disabled="col.required" />
                    </template>
                    <VListItemTitle>{{ col.title }}</VListItemTitle>
                  </VListItem>
                </VList>
              </VMenu>
              <VBtn size="small" variant="tonal" :disabled="busy || !pendingSelected.length" @click="pendingWhitelist">加入白名单</VBtn>
              <VBtn size="small" variant="text" :disabled="busy || !pendingSelected.length" @click="pendingRemove">移出待删除</VBtn>
              <VBtn size="small" color="error" variant="text" :disabled="busy || !pendingSelected.length" @click="pendingDeleteNow">立即删除</VBtn>
            </div>
          </div>
          <div v-if="pendingRows.length" class="smartclean-table-wrap">
            <table class="smartclean-table">
              <thead>
                <tr>
                  <th>
                    <input
                      type="checkbox"
                      :checked="allPendingSelected"
                      :indeterminate.prop="somePendingSelected"
                      @change="toggleSelectAllPending"
                    >
                  </th>
                  <th v-if="pendingColVisible('title')" class="smartclean-sortable" @click="togglePendingSort('title')">{{ pendingColTitle('title') }}{{ pendingSortMark('title') }}</th>
                  <th v-if="pendingColVisible('year')" class="smartclean-sortable" @click="togglePendingSort('year')">{{ pendingColTitle('year') }}{{ pendingSortMark('year') }}</th>
                  <th v-if="pendingColVisible('douban_stars')" class="smartclean-sortable" @click="togglePendingSort('douban_stars')">{{ pendingColTitle('douban_stars') }}{{ pendingSortMark('douban_stars') }}</th>
                  <th v-if="pendingColVisible('douban_watched')" class="smartclean-sortable" @click="togglePendingSort('douban_watched')">{{ pendingColTitle('douban_watched') }}{{ pendingSortMark('douban_watched') }}</th>
                  <th v-if="pendingColVisible('transfer')" class="smartclean-sortable" @click="togglePendingSort('transfer')">{{ pendingColTitle('transfer') }}{{ pendingSortMark('transfer') }}</th>
                  <th v-if="pendingColVisible('library')" class="smartclean-sortable" @click="togglePendingSort('library')">{{ pendingColTitle('library') }}{{ pendingSortMark('library') }}</th>
                  <th v-if="pendingColVisible('playback')" class="smartclean-sortable" @click="togglePendingSort('playback')">{{ pendingColTitle('playback') }}{{ pendingSortMark('playback') }}</th>
                  <th v-if="pendingColVisible('favorite')" class="smartclean-sortable" @click="togglePendingSort('favorite')">{{ pendingColTitle('favorite') }}{{ pendingSortMark('favorite') }}</th>
                  <th v-if="pendingColVisible('has_source')" class="smartclean-sortable" @click="togglePendingSort('has_source')">{{ pendingColTitle('has_source') }}{{ pendingSortMark('has_source') }}</th>
                  <th v-if="pendingColVisible('due')" class="smartclean-sortable" @click="togglePendingSort('due')">{{ pendingColTitle('due') }}{{ pendingSortMark('due') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in pendingPageRows" :key="row.key">
                  <td>
                    <input v-model="pendingSelected" type="checkbox" :value="row.key">
                  </td>
                  <td v-if="pendingColVisible('title')" :class="{ 'smartclean-hit-cell': cellHit(row, 'title') }">
                    <div>{{ row.title }}</div>
                    <div v-if="row.reason" class="smartclean-meta">{{ row.reason }}</div>
                  </td>
                  <td v-if="pendingColVisible('year')" :class="{ 'smartclean-hit-cell': cellHit(row, 'year') }">
                    {{ row.year }}<template v-if="row.season != null"> / S{{ row.season }}</template>
                  </td>
                  <td v-if="pendingColVisible('douban_stars')" :class="{ 'smartclean-hit-cell': cellHit(row, 'douban_stars') }">
                    {{ row.douban_stars != null ? `${row.douban_stars}星` : '—' }}
                  </td>
                  <td v-if="pendingColVisible('douban_watched')" :class="{ 'smartclean-hit-cell': cellHit(row, 'douban_watched') }">
                    <template v-if="row.douban_watched_on">
                      <div>{{ row.douban_watched_days != null ? `${row.douban_watched_days}天前` : '看过' }}</div>
                      <div class="smartclean-meta">{{ formatTime(row.douban_watched_on) }}</div>
                    </template>
                    <template v-else>—</template>
                  </td>
                  <td v-if="pendingColVisible('transfer')" :class="{ 'smartclean-hit-cell': cellHit(row, 'transfer') }">
                    <div>{{ (row.transfer_users || []).join('、') || '—' }}</div>
                    <div class="smartclean-meta">{{ formatTime(row.latest_transfer_at) }}</div>
                  </td>
                  <td v-if="pendingColVisible('library')" :class="{ 'smartclean-hit-cell': cellHit(row, 'library') }">
                    <div>{{ row.library_age_days ?? '—' }}天</div>
                    <div class="smartclean-meta">{{ formatTime(row.added_at) }}</div>
                  </td>
                  <td v-if="pendingColVisible('playback')" :class="{ 'smartclean-hit-cell': cellHit(row, 'playback') }">{{ playbackText(row) }}</td>
                  <td v-if="pendingColVisible('favorite')" :class="{ 'smartclean-hit-cell': cellHit(row, 'favorite') }">{{ row.favorite ? '是' : '' }}</td>
                  <td v-if="pendingColVisible('has_source')" :class="{ 'smartclean-hit-cell': cellHit(row, 'has_source') }">
                    {{ row.has_source ? '是' : (row.has_source === false ? '否' : '—') }}
                  </td>
                  <td v-if="pendingColVisible('due')" :class="{ 'smartclean-due': row.is_due }">
                    <div>{{ formatTime(row.due_at) || '—' }}</div>
                    <div class="smartclean-meta">自 {{ formatTime(row.started_at) || '—' }}</div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="smartclean-meta">暂无待删除项。保存配置后点「执行一次」生成清单。</p>
          <div v-if="pendingRows.length" class="smartclean-toolbar">
            <VBtn size="small" :disabled="pendingPage <= 1" @click="pendingPage -= 1">上一页</VBtn>
            <span class="smartclean-meta">{{ pendingPage }} / {{ pendingTotalPages }}</span>
            <VBtn size="small" :disabled="pendingPage >= pendingTotalPages" @click="pendingPage += 1">下一页</VBtn>
          </div>
        </template>

        <template v-else-if="libMode === 'history'">
          <div class="smartclean-toolbar">
            <h4>删除历史</h4>
            <span class="smartclean-meta">共 {{ historyRows.length }} 条</span>
            <VBtn
              size="small"
              variant="text"
              color="error"
              :disabled="busy || !automation?.id || !historyRows.length"
              @click="clearHistory"
            >
              清空历史
            </VBtn>
          </div>
          <div v-if="historyRows.length" class="smartclean-table-wrap">
            <table class="smartclean-table">
              <thead>
                <tr>
                  <th class="smartclean-sortable" @click="toggleHistorySort('deleted_at')">删除时间{{ historySortMark('deleted_at') }}</th>
                  <th class="smartclean-sortable" @click="toggleHistorySort('title')">片名{{ historySortMark('title') }}</th>
                  <th class="smartclean-sortable" @click="toggleHistorySort('mode')">删除方式{{ historySortMark('mode') }}</th>
                  <th class="smartclean-sortable" @click="toggleHistorySort('reason')">原因{{ historySortMark('reason') }}</th>
                  <th class="smartclean-sortable" @click="toggleHistorySort('message')">结果{{ historySortMark('message') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in historyPageRows" :key="item.id">
                  <td>{{ formatTime(item.deleted_at) }}</td>
                  <td>{{ labelOf(item) }}</td>
                  <td>{{ modeLabel(item.mode) }}</td>
                  <td>{{ item.reason || '—' }}</td>
                  <td>{{ historyResult(item) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="smartclean-meta">暂无删除历史</p>
          <div v-if="historyRows.length" class="smartclean-toolbar">
            <VBtn size="small" :disabled="historyPage <= 1" @click="historyPage -= 1">上一页</VBtn>
            <span class="smartclean-meta">{{ historyPage }} / {{ historyTotalPages }}</span>
            <VBtn size="small" :disabled="historyPage >= historyTotalPages" @click="historyPage += 1">下一页</VBtn>
          </div>
        </template>
      </section>
      <section v-else class="smartclean-main">
        <p class="smartclean-meta">选择左侧媒体库，进入手动清理或自动化配置。</p>
      </section>
    </div>

    <div v-else-if="tab === 'rules'" class="smartclean-layout">
      <aside class="smartclean-side">
        <div class="smartclean-toolbar">
          <h3>规则</h3>
          <VBtn size="small" @click="newRule">新建</VBtn>
        </div>
        <button
          v-for="rule in overview?.rules || []"
          :key="rule.id"
          type="button"
          class="smartclean-lib"
          :class="{ active: editingRuleId === rule.id }"
          @click="editRule(rule)"
        >
          <strong>{{ rule.name }}</strong>
          <span>{{ rule.conditions.length }} 条件 · {{ rule.combine === 'any' ? '任一' : '全部' }}</span>
        </button>
      </aside>
      <section class="smartclean-main smartclean-rule-editor">
        <div class="smartclean-bar">
          <h4 class="smartclean-bar-title">{{ editingRuleId ? '编辑规则' : '新建规则' }}</h4>
          <span class="smartclean-meta">{{ ruleDraft.conditions.length }} 个条件</span>
        </div>

        <div class="smartclean-rule-meta">
          <VTextField
            v-model="ruleDraft.name"
            label="规则名称"
            density="compact"
            hide-details
            class="smartclean-rule-meta-name"
          />
          <VSelect
            v-model="ruleDraft.combine"
            :items="[{ title: '全部满足', value: 'all' }, { title: '任一满足', value: 'any' }]"
            label="组合方式"
            density="compact"
            hide-details
            class="smartclean-rule-meta-combine"
          />
        </div>

        <div class="smartclean-rule-conditions">
          <div class="smartclean-bar">
            <span class="smartclean-bar-label">条件</span>
            <VBtn size="small" variant="text" @click="ruleDraft.conditions.push(blankCondition())">加条件</VBtn>
          </div>
          <div
            v-for="(cond, index) in ruleDraft.conditions"
            :key="index"
            class="smartclean-rule-cond"
          >
            <VSelect
              v-model="cond.field"
              :items="FIELD_OPTIONS"
              item-title="title"
              item-value="value"
              label="字段"
              density="compact"
              hide-details
              class="smartclean-rule-cond-field"
              @update:model-value="onFieldChange(cond)"
            />
            <VSelect
              v-model="cond.op"
              :items="opsFor(cond.field).map(op => ({ title: OP_LABELS[op] || op, value: op }))"
              label="运算"
              density="compact"
              hide-details
              class="smartclean-rule-cond-op"
            />
            <VSelect
              v-if="cond.field === 'transfer_users'"
              v-model="cond.value"
              :items="users"
              label="用户"
              multiple
              chips
              density="compact"
              hide-details
              class="smartclean-rule-cond-value smartclean-rule-cond-value--wide"
            />
            <VSelect
              v-else-if="['playback_unplayed', 'favorite', 'has_source'].includes(cond.field)"
              v-model="cond.value"
              :items="[{ title: '是', value: true }, { title: '否', value: false }]"
              label="值"
              density="compact"
              hide-details
              class="smartclean-rule-cond-value"
            />
            <VTextField
              v-else
              v-model="cond.value"
              label="值"
              density="compact"
              hide-details
              class="smartclean-rule-cond-value"
            />
            <VBtn size="small" variant="text" @click="ruleDraft.conditions.splice(index, 1)">移除</VBtn>
          </div>
          <p v-if="!ruleDraft.conditions.length" class="smartclean-meta">还没有条件，点「加条件」开始配置。</p>
        </div>

        <div class="smartclean-bar">
          <div class="smartclean-bar-group smartclean-bar-group--end">
            <VBtn
              v-if="editingRuleId"
              color="error"
              variant="text"
              :disabled="busy"
              @click="deleteRule(editingRuleId)"
            >
              删除
            </VBtn>
            <VBtn color="primary" :disabled="busy" @click="saveRule">保存规则</VBtn>
          </div>
        </div>
      </section>
    </div>

    <div v-else class="smartclean-main">
      <h4>白名单</h4>
      <p class="smartclean-meta">仅手动添加。可从媒体库或待删除加白；移出即删除。</p>
      <div class="smartclean-toolbar">
        <VBtn size="small" color="error" variant="text" :disabled="busy || !wlSelectedKeys.length" @click="excludeEffective">移出白名单</VBtn>
        <span class="smartclean-meta">已选 {{ wlSelectedKeys.length }} / {{ effectiveEntries.length }}</span>
      </div>
      <div class="smartclean-table-wrap">
        <table class="smartclean-table">
          <thead>
            <tr>
              <th>
                <input
                  type="checkbox"
                  :checked="effectiveEntries.length > 0 && effectiveEntries.every(e => wlSelectedKeys.includes(entryKey(e)))"
                  @change="toggleAllEffective"
                >
              </th>
              <th>片名</th>
              <th>年/季</th>
              <th>在库</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in effectiveEntries" :key="entryKey(entry)">
              <td><input v-model="wlSelectedKeys" type="checkbox" :value="entryKey(entry)"></td>
              <td>{{ entry.item.title }}</td>
              <td>{{ entry.item.year }}<template v-if="entry.item.season != null"> / S{{ entry.item.season }}</template></td>
              <td>{{ inLibraryText(entry.item) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!effectiveEntries.length" class="smartclean-meta">暂无条目。请从媒体库或待删除加入白名单。</p>
    </div>
  </div>
</template>

<style scoped>
.smartclean {
  padding: 0 4px 24px;
  min-width: 0;
  max-width: 100%;
  overflow-x: hidden;
  box-sizing: border-box;
}
.smartclean-header {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 4px;
  margin-bottom: 4px;
  background: rgba(var(--v-theme-surface), 0.55);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
}
.smartclean-header strong { font-size: 16px; }
.smartclean-header-actions { margin-left: auto; display: flex; align-items: center; }
.smartclean-layout {
  display: grid;
  grid-template-columns: minmax(148px, 188px) minmax(0, 1fr);
  gap: 12px;
  min-width: 0;
  max-width: 100%;
}
.smartclean-side {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.smartclean-main {
  min-width: 0;
  max-width: 100%;
  overflow-x: hidden;
}
.smartclean-lib {
  text-align: left; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  background: transparent; border-radius: 8px; padding: 8px 10px; cursor: pointer;
}
.smartclean-lib.active { border-color: rgb(var(--v-theme-primary)); }
.smartclean-lib strong { display: block; }
.smartclean-lib span, .smartclean-meta { opacity: 0.7; font-size: 12px; }
.smartclean-toolbar, .smartclean-filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
.smartclean-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
  margin-bottom: 10px;
}
.smartclean-bar--filters {
  border-top: 1px solid rgba(var(--v-border-color), calc(var(--v-border-opacity) * 0.7));
  margin-top: 2px;
  padding-top: 10px;
}
.smartclean-bar--cond {
  justify-content: flex-start;
  margin-bottom: 8px;
}
.smartclean-bar-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.smartclean-bar-group--end {
  margin-left: auto;
  justify-content: flex-end;
}
.smartclean-bar-label {
  font-size: 12px;
  opacity: 0.7;
  letter-spacing: 0.02em;
}
.smartclean-bar-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.smartclean-col-menu {
  min-width: 168px;
  max-height: 320px;
  overflow: auto;
}
.smartclean-rule-name {
  flex: 0 0 168px;
  width: 168px;
  min-width: 168px;
  max-width: 220px;
}
.smartclean-form { display: flex; flex-direction: column; gap: 16px; max-width: 520px; margin-bottom: 16px; }
.smartclean-rule-editor {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.smartclean-rule-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 14px 16px;
  align-items: flex-start;
}
.smartclean-rule-meta-name {
  flex: 1 1 260px;
  min-width: 220px;
  max-width: 420px;
}
.smartclean-rule-meta-combine {
  flex: 0 0 168px;
  width: 168px;
  min-width: 168px;
}
.smartclean-rule-conditions {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-top: 14px;
  border-top: 1px solid rgba(var(--v-border-color), calc(var(--v-border-opacity) * 0.7));
}
.smartclean-rule-cond {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 12px;
  align-items: center;
}
.smartclean-rule-cond-field {
  flex: 0 1 168px;
  width: 168px;
  min-width: 140px;
}
.smartclean-rule-cond-op {
  flex: 0 0 112px;
  width: 112px;
  min-width: 96px;
}
.smartclean-rule-cond-value {
  flex: 0 1 140px;
  width: 140px;
  min-width: 112px;
}
.smartclean-rule-cond-value--wide {
  flex: 1 1 220px;
  width: auto;
  min-width: 200px;
  max-width: 360px;
}
.smartclean-table-wrap {
  overflow: auto;
  max-height: 60vh;
  max-width: 100%;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
}
.smartclean-table { width: max-content; min-width: 100%; border-collapse: collapse; font-size: 12.5px; }
.smartclean-table th, .smartclean-table td {
  padding: 5px 7px;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  text-align: left;
  white-space: nowrap;
  vertical-align: top;
}
.smartclean-table th:nth-child(2),
.smartclean-table td:nth-child(2) {
  /* 片名列可适度换行，减少横向占用 */
  white-space: normal;
  min-width: 9rem;
  max-width: 16rem;
}
.smartclean-sortable { cursor: pointer; user-select: none; }
.smartclean-sortable:hover { color: rgb(var(--v-theme-primary)); }
.smartclean-item { display: flex; justify-content: space-between; gap: 8px; align-items: center; padding: 8px 0; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.smartclean-check { display: flex; gap: 8px; align-items: flex-start; }
.smartclean-hit-cell { background: rgba(var(--v-theme-primary), 0.12); }
.smartclean-due { color: rgb(var(--v-theme-error)); font-weight: 600; }
@media (max-width: 900px) {
  .smartclean-layout { grid-template-columns: 1fr; }
}
</style>

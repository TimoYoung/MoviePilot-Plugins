<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { Api } from '../types'
import { unwrap } from '../types'

interface WatchedStatus {
  user?: string
  count?: number
  updated_at?: string
  total?: number
  last_error?: string
  last_sync_mode?: string
  pages_fetched?: number
  has_snapshot?: boolean
  hint?: string
}

const props = withDefaults(defineProps<{
  initialConfig?: {
    enabled?: boolean
    main_user?: string
    douban_cookie?: string
    douban_watched_sync_enabled?: boolean
    douban_watched_sync_cron?: string
  }
  api?: Api
  pluginId?: string
}>(), {
  initialConfig: () => ({}),
  pluginId: 'SmartClean',
})
const emit = defineEmits<{
  save: [config: {
    enabled: boolean
    main_user: string
    douban_cookie: string
    douban_watched_sync_enabled: boolean
    douban_watched_sync_cron: string
  }]
  close: []
  switch: []
}>()

const silent = { feedback: 'silent' as const }
const enabled = ref(false)
const mainUser = ref<string | null>(null)
const doubanCookie = ref('')
const doubanWatchedSyncEnabled = ref(true)
const doubanWatchedSyncCron = ref('0 6 * * *')
const users = ref<string[]>([])
const loadError = ref('')
const notice = ref('')
const syncBusy = ref(false)
const watched = ref<WatchedStatus | null>(null)

const url = (path: string) => `plugin/${props.pluginId}${path}`
const userItems = computed(() => users.value.map(name => ({ title: name, value: name })))

watch(() => props.initialConfig, config => {
  enabled.value = config.enabled === true
  mainUser.value = config.main_user || null
  doubanCookie.value = config.douban_cookie || ''
  doubanWatchedSyncEnabled.value = config.douban_watched_sync_enabled !== false
  doubanWatchedSyncCron.value = config.douban_watched_sync_cron || '0 6 * * *'
}, { immediate: true })

function formatTime(value?: string) {
  if (!value) return '—'
  const text = String(value).trim().replace('T', ' ')
  return text.length >= 19 ? text.slice(0, 19) : text
}

async function loadWatchedStatus() {
  if (!props.api?.get) return
  try {
    watched.value = unwrap(await props.api.get<WatchedStatus>(url('/douban/watched/status'), silent))
  } catch {
    // 状态失败不挡设置页
  }
}

async function syncWatched(mode: 'incremental' | 'full') {
  if (!props.api?.post || syncBusy.value) return
  if (mode === 'full') {
    const ok = window.confirm('全量重建会分页拉取整份豆瓣看过列表，耗时长且易触发限流。确认继续？')
    if (!ok) return
  }
  syncBusy.value = true
  loadError.value = ''
  notice.value = ''
  try {
    const result = unwrap(await props.api.post<WatchedStatus>(url('/douban/watched/sync'), { mode }, silent))
    watched.value = result
    notice.value = mode === 'full'
      ? `全量重建完成：${result.count ?? 0} 条`
      : `增量更新完成：${result.count ?? 0} 条`
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : '同步豆瓣看过失败'
    await loadWatchedStatus()
  } finally {
    syncBusy.value = false
  }
}

onMounted(async () => {
  if (!props.api?.get) return
  try {
    const data = unwrap(await props.api.get<{ users: string[] }>(url('/users'), silent))
    users.value = data.users || []
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : '读取用户列表失败'
  }
  await loadWatchedStatus()
})
</script>

<template>
  <section class="smartclean-config">
    <header class="smartclean-config-header">
      <strong>智能清理 · 设置</strong>
      <div class="smartclean-config-header-actions">
        <VBtn
          variant="tonal"
          color="info"
          size="small"
          prepend-icon="mdi-database-eye-outline"
          @click="emit('switch')"
        >
          返回详情
        </VBtn>
        <VBtn icon="mdi-close" variant="text" size="small" aria-label="关闭" @click="emit('close')" />
      </div>
    </header>
    <VSwitch v-model="enabled" label="启用插件" hide-details />
    <VSelect
      v-model="mainUser"
      :items="userItems"
      label="主用户"
      hint="读取该用户个人资料里的「豆瓣用户」；不选则使用系统超级用户"
      persistent-hint
      clearable
      hide-details="auto"
    />
    <VTextField
      v-model="doubanCookie"
      label="豆瓣 Cookie（可选）"
      type="password"
      hint="勾选了「仅自己可见」的看过必须填；从浏览器复制含 dbcl2、ck 的整段 Cookie"
      persistent-hint
      hide-details="auto"
    />

    <VAlert type="info" variant="tonal" :icon="false">
      平时扫描/自动化只读本地快照；快照为空时会自动全量重建一次。日常请用定时或「更新豆瓣看过」做增量。
    </VAlert>
    <div class="smartclean-watched">
      <div class="smartclean-meta">
        豆瓣用户：{{ watched?.user || '未设置' }} ·
        本地 {{ watched?.count ?? 0 }} 条 ·
        更新于 {{ formatTime(watched?.updated_at) }}
        <template v-if="watched?.last_sync_mode"> · 上次 {{ watched.last_sync_mode }}</template>
      </div>
      <div v-if="watched?.last_error" class="smartclean-meta smartclean-error">上次错误：{{ watched.last_error }}</div>
      <div v-if="watched?.hint" class="smartclean-meta">{{ watched.hint }}</div>
      <div class="smartclean-actions" style="justify-content: flex-start">
        <VBtn size="small" :disabled="syncBusy" :loading="syncBusy" @click="syncWatched('incremental')">更新豆瓣看过</VBtn>
        <VBtn size="small" variant="text" color="warning" :disabled="syncBusy" @click="syncWatched('full')">全量重建</VBtn>
      </div>
    </div>

    <VSwitch v-model="doubanWatchedSyncEnabled" label="定时增量同步豆瓣看过" hide-details />
    <VCronField
      v-if="doubanWatchedSyncEnabled"
      v-model="doubanWatchedSyncCron"
      label="豆瓣看过同步定时"
    />

    <VAlert v-if="loadError" type="warning" variant="tonal" :icon="false">{{ loadError }}</VAlert>
    <VAlert v-if="notice" type="success" variant="tonal" :icon="false">{{ notice }}</VAlert>
    <div class="smartclean-actions">
      <VBtn variant="text" @click="emit('close')">取消</VBtn>
      <VBtn
        color="primary"
        @click="emit('save', {
          enabled,
          main_user: mainUser || '',
          douban_cookie: doubanCookie,
          douban_watched_sync_enabled: doubanWatchedSyncEnabled,
          douban_watched_sync_cron: doubanWatchedSyncCron,
        })"
      >
        保存
      </VBtn>
    </div>
  </section>
</template>

<style scoped>
.smartclean-config { display: grid; gap: 1rem; padding: 1rem; }
.smartclean-config-header {
  display: flex;
  align-items: center;
  gap: 8px;
  position: sticky;
  top: 0;
  z-index: 2;
  padding-bottom: 4px;
  /* 跟随 glass 卡片半透明，避免 sticky 实色底与主题割裂 */
  background: rgba(var(--v-theme-surface), 0.55);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
}
.smartclean-config-header strong { font-size: 16px; }
.smartclean-config-header-actions { margin-left: auto; display: flex; align-items: center; gap: 4px; }
.smartclean-actions { display: flex; justify-content: flex-end; gap: .5rem; }
.smartclean-watched { display: grid; gap: 8px; }
.smartclean-meta { opacity: 0.75; font-size: 12px; }
.smartclean-error { color: rgb(var(--v-theme-error)); opacity: 1; }
</style>

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  deleteProxyFromPool,
  getProxyPool,
  replaceProxyPool,
} from '@/api/proxy'

const LEGACY_KEY = 'dango_proxy_pool_v1'
const OLD_FORM_KEY = 'gpt_outlook_register_form_v2'

function parseLines(value) {
  return String(value || '')
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)
}

function dedup(items) {
  return [...new Set(items)]
}

// 代理池由服务端 SQLite 持久化，跨浏览器和跨电脑共享。
export const useProxyStore = defineStore('proxy', () => {
  const list = ref([])
  const loaded = ref(false)
  const loading = ref(false)
  const text = computed(() => list.value.join('\n'))
  const count = computed(() => list.value.length)

  async function load(force = false) {
    if (loading.value) return list.value
    if (loaded.value && !force) return list.value
    loading.value = true
    try {
      const response = await getProxyPool()
      let items = response.items || []
      if (!items.length) {
        let legacy = []
        try { legacy = JSON.parse(localStorage.getItem(LEGACY_KEY) || '[]') } catch (_) { legacy = [] }
        if (!legacy.length) {
          try {
            const old = JSON.parse(localStorage.getItem(OLD_FORM_KEY) || '{}')
            if (old.autoProxyPool) legacy = dedup(parseLines(old.autoProxyPool))
          } catch (_) { /* ignore */ }
        }
        if (legacy.length) {
          const migrated = await replaceProxyPool(dedup(legacy))
          items = migrated.items || []
          localStorage.removeItem(LEGACY_KEY)
        }
      }
      list.value = items
      loaded.value = true
      return list.value
    } finally {
      loading.value = false
    }
  }

  async function setFromText(value) {
    const parsed = parseLines(value)
    const unique = dedup(parsed)
    const response = await replaceProxyPool(unique)
    list.value = response.items || []
    loaded.value = true
    return {
      total: parsed.length,
      kept: unique.length,
      duplicated: parsed.length - unique.length,
    }
  }

  async function append(value) {
    const merged = dedup([...list.value, ...parseLines(value)])
    const added = merged.length - list.value.length
    const response = await replaceProxyPool(merged)
    list.value = response.items || []
    loaded.value = true
    return { added }
  }

  async function remove(proxy) {
    const response = await deleteProxyFromPool(proxy)
    list.value = response.items || []
  }

  async function clear() {
    const response = await replaceProxyPool([])
    list.value = response.items || []
  }

  load().catch(() => {})

  return { list, text, count, loaded, loading, load, setFromText, append, remove, clear }
})

/** 判断代理格式是否合法：[scheme://][user:pass@]host:port */
export function isValidProxy(proxy) {
  return /^((socks5h?|socks4|https?):\/\/)?\S+:\d+$/i.test(proxy.trim())
}

/** 该代理生效的协议类型。 */
export function proxyScheme(proxy) {
  const match = /^(socks5h?|socks4|https?):\/\//i.exec(proxy.trim())
  return match ? match[1].toLowerCase() : 'http(默认)'
}

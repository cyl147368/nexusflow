import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getStats } from '@/api/accounts'

const emptyStats = () => ({
  total: 0, available: 0, in_use: 0, done: 0, failed: 0,
})

// 原有流程与一体化流程分开计数；stats 始终保持原有流程，避免旧页面混用。
export const useStatsStore = defineStore('stats', () => {
  const stats = ref(emptyStats())
  const byWorkflow = ref({
    all: emptyStats(),
    integration: emptyStats(),
    legacy: emptyStats(),
  })
  let timer = null

  async function refresh() {
    try {
      const result = await getStats('legacy')
      if (result.by_workflow) byWorkflow.value = result.by_workflow
      if (result.stats) stats.value = result.stats
    } catch (e) {
      // 静默：统计失败不打扰用户
      console.error('stats refresh:', e)
    }
  }

  function startPolling(interval = 5000) {
    refresh()
    if (timer) clearInterval(timer)
    timer = setInterval(refresh, interval)
  }

  function stopPolling() {
    if (timer) clearInterval(timer)
    timer = null
  }

  return { stats, byWorkflow, refresh, startPolling, stopPolling }
})

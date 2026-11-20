<script setup>
import { computed, onActivated, onDeactivated, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getPaymentHealth,
  getPaymentRun,
  listPaymentCandidates,
  listPaymentResults,
  startPaymentCheck,
} from '@/api/payment'
import {
  getIntegrationConfig,
  saveIntegrationConfig,
} from '@/api/integration'
import { fmtTime } from '@/api/request'

const loading = ref(true)
const starting = ref(false)
const health = ref({})
const candidates = ref([])
const results = ref([])
const selected = ref([])
const methods = ref(['momo', 'upi'])
const proxies = ref({ momo: '', upi: '' })
const timeout = ref(45)
const workers = ref(2)
const activeRun = ref(null)
let timer = null

const latestByEmail = computed(() => {
  const out = {}
  for (const item of results.value) {
    ;(out[item.email] ||= {})[item.method] = item
  }
  return out
})

const running = computed(() => activeRun.value?.run?.status === 'running')
const progress = computed(() => {
  const run = activeRun.value?.run
  if (!run?.total) return 0
  return Math.round(((run.completed || 0) / run.total) * 100)
})

function stateType(state) {
  if (state === 'eligible') return 'success'
  if (state === 'ineligible') return 'danger'
  if (state === 'unknown') return 'warning'
  return 'info'
}

function stateLabel(item) {
  if (!item) return '未检测'
  return item.label || item.state || '未知'
}

async function load() {
  loading.value = true
  try {
    const [h, c, r, cfg] = await Promise.all([
      getPaymentHealth(),
      listPaymentCandidates(),
      listPaymentResults(),
      getIntegrationConfig(),
    ])
    health.value = h
    candidates.value = c.items || []
    results.value = r.items || []
    proxies.value.momo = cfg.config?.momo_proxy || ''
    proxies.value.upi = cfg.config?.upi_proxy || ''
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function refreshResults() {
  try {
    const [c, r] = await Promise.all([listPaymentCandidates(), listPaymentResults()])
    candidates.value = c.items || []
    results.value = r.items || []
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function start() {
  if (!methods.value.length) return ElMessage.warning('至少选择 MoMo 或 UPI')
  if (!selected.value.length) return ElMessage.warning('请选择账号')
  const missing = methods.value.filter((method) => !proxies.value[method]?.trim())
  if (missing.length) {
    return ElMessage.warning(`请配置出口代理：${missing.map((m) => m.toUpperCase()).join('、')}`)
  }
  starting.value = true
  try {
    await saveIntegrationConfig({
      momo_proxy: proxies.value.momo.trim(),
      upi_proxy: proxies.value.upi.trim(),
    })
    const r = await startPaymentCheck({
      emails: selected.value,
      methods: methods.value,
      proxies: proxies.value,
      timeout: timeout.value,
      workers: workers.value,
    })
    activeRun.value = { run: { run_id: r.run_id, status: 'running', completed: 0, total: r.total }, items: [] }
    pollRun(r.run_id)
    startPolling()
    ElMessage.success('检测任务已启动')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    starting.value = false
  }
}

async function pollRun(runId) {
  try {
    const r = await getPaymentRun(runId)
    activeRun.value = r
    if (r.run?.status !== 'running') {
      stopPolling()
      await refreshResults()
      ElMessage[r.run?.status === 'completed' ? 'success' : 'error'](
        r.run?.status === 'completed' ? '资格检测完成' : '资格检测失败',
      )
    }
  } catch (e) {
    stopPolling()
    ElMessage.error(e.message)
  }
}

function startPolling() {
  stopPolling()
  timer = setInterval(() => {
    if (activeRun.value?.run?.run_id) pollRun(activeRun.value.run.run_id)
  }, 2000)
}

function stopPolling() {
  if (timer) clearInterval(timer)
  timer = null
}

onActivated(() => {
  load()
  if (activeRun.value?.run?.run_id && running.value) startPolling()
})
onDeactivated(stopPolling)
load()
</script>

<template>
  <div class="page payment-page" v-loading="loading">
    <el-card shadow="never" class="intro">
      <div class="intro-row">
        <div>
          <h2>MoMo / UPI 支付资格检测</h2>
          <p>只读读取主项目已注册账号，创建 Checkout 并读取 Stripe init；不创建支付方式，不 Confirm，不 Approve，不扣款。</p>
        </div>
        <div class="health">
          <el-tag :type="health.ok ? 'success' : 'danger'">
            {{ health.ok ? '检测服务在线' : '检测服务离线' }}
          </el-tag>
          <span>{{ health.usable_accounts || 0 }} / {{ health.registered_accounts || 0 }} 个账号可检测</span>
          <span>Node {{ health.node || '未就绪' }} · Chromium {{ health.chromium ? '就绪' : '未就绪' }}</span>
        </div>
      </div>
    </el-card>

    <el-row :gutter="16">
      <el-col :xs="24" :lg="8">
        <el-card shadow="never">
          <template #header><span>检测配置</span></template>
          <el-form label-position="top">
            <el-form-item label="支付方式">
              <el-checkbox-group v-model="methods">
                <el-checkbox value="momo">MoMo · 越南出口</el-checkbox>
                <el-checkbox value="upi">UPI · 印度出口</el-checkbox>
              </el-checkbox-group>
            </el-form-item>
            <el-form-item v-if="methods.includes('momo')" label="MoMo 出口代理">
              <el-input v-model="proxies.momo" placeholder="socks5://user:pass@vn-exit:port" />
            </el-form-item>
            <el-form-item v-if="methods.includes('upi')" label="UPI 出口代理">
              <el-input v-model="proxies.upi" placeholder="socks5://user:pass@in-exit:port" />
            </el-form-item>
            <el-form-item label="单请求超时（秒）">
              <el-input-number v-model="timeout" :min="10" :max="180" />
            </el-form-item>
            <el-form-item label="并发账号数">
              <el-input-number v-model="workers" :min="1" :max="8" />
            </el-form-item>
          </el-form>
          <el-alert
            type="warning" :closable="false" show-icon
            title="地区出口必须与支付方式一致，否则服务端会返回 Billing country mismatch。"
          />
          <el-button
            type="primary" class="start-btn" :loading="starting || running" :disabled="running"
            @click="start"
          >
            {{ running ? `检测中 ${progress}%` : '开始资格检测' }}
          </el-button>
          <el-progress v-if="activeRun" :percentage="progress" :status="running ? undefined : 'success'" />
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="16">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span>主项目账号</span>
              <el-button size="small" @click="refreshResults">刷新</el-button>
            </div>
          </template>
          <el-table
            :data="candidates" row-key="email" size="small"
            @selection-change="(rows) => (selected = rows.map((row) => row.email))"
          >
            <el-table-column type="selection" width="44" :selectable="(row) => row.usable" />
            <el-table-column prop="email" label="账号" min-width="210" />
            <el-table-column label="凭证" width="110">
              <template #default="{ row }">
                <el-tooltip
                  v-if="!row.usable"
                  :content="`缺少：${(row.missing_credentials || []).join('、')}`"
                >
                  <el-tag type="warning" size="small">不可检测</el-tag>
                </el-tooltip>
                <el-tag v-else type="success" size="small">完整</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="MoMo" min-width="150">
              <template #default="{ row }">
                <el-tag :type="stateType(latestByEmail[row.email]?.momo?.state)" size="small">
                  {{ stateLabel(latestByEmail[row.email]?.momo) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="UPI" min-width="150">
              <template #default="{ row }">
                <el-tag :type="stateType(latestByEmail[row.email]?.upi?.state)" size="small">
                  {{ stateLabel(latestByEmail[row.email]?.upi) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="检测时间" width="170">
              <template #default="{ row }">
                {{ fmtTime(Math.max(
                  latestByEmail[row.email]?.momo?.checked_at || 0,
                  latestByEmail[row.email]?.upi?.checked_at || 0,
                )) }}
              </template>
            </el-table-column>
          </el-table>
          <div class="hint">
            已选择 {{ selected.length }} 个可检测账号；缺少 Access Token 或 Session 的记录不能执行支付资格检测。
          </div>
        </el-card>

        <el-card v-if="activeRun?.items?.length" shadow="never" class="results-card">
          <template #header><span>本次检测结果</span></template>
          <el-table :data="activeRun.items" size="small">
            <el-table-column prop="email" label="账号" min-width="210" />
            <el-table-column prop="method" label="方式" width="90" />
            <el-table-column label="结论" min-width="170">
              <template #default="{ row }">
                <el-tag :type="stateType(row.state)" size="small">{{ stateLabel(row) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="country" label="地区" width="80" />
            <el-table-column prop="error" label="错误/原因" min-width="220" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.payment-page { max-width: 1280px; margin: 0 auto; }
.intro { margin-bottom: 16px; border: 1px solid var(--el-color-primary-light-7); }
.intro-row { display: flex; justify-content: space-between; gap: 24px; align-items: center; }
.intro h2 { margin: 0 0 8px; }
.intro p { margin: 0; color: var(--el-text-color-secondary); }
.health { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; white-space: nowrap; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.start-btn { width: 100%; margin: 16px 0 12px; }
.results-card { margin-top: 16px; }
.hint { margin-top: 10px; color: var(--el-text-color-secondary); font-size: 13px; }
@media (max-width: 900px) {
  .intro-row { flex-direction: column; align-items: flex-start; }
  .health { align-items: flex-start; }
}
</style>

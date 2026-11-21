<script setup>
import { computed, onActivated, onDeactivated, ref } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import {
  startRegister, startRecoveryBatch, getRegistered, getLatestRecoveryBatch,
  getRecoveryBatch,
} from '@/api/register'
import { listAccounts } from '@/api/accounts'
import { copyText } from '@/api/request'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import LogPanel from '@/components/LogPanel.vue'

const route = useRoute()
const { form } = storeToRefs(useFormStore())
const { list: proxyList } = storeToRefs(useProxyStore())
const runtime = useRuntimeStore()
const { runningSingle, lastRunResult } = storeToRefs(runtime)

const starting = ref(false)
const mode = ref('register')
const regEmail = ref('')
const recoveryEmails = ref([])
const recoverySource = ref('pool')
const recoveryRelay = ref('')
const recoveryPassword = ref('')
const recoveryWant2fa = ref(true)
const recoveryScope = ref('legacy')
const recoveryAccounts = ref([])
const recoveryBusy = ref(false)
const activeRecoveryBatchId = ref('')
const recoveryBatchJob = ref(null)
let recoveryBatchTimer = null
// 2FA 默认开（主人要求每个号都绑）。绑定不可逆，所以留开关。
// 放在 form store（localStorage 持久化）而不是组件局部 ref —— 组件是
// keep-alive 的，切页不丢，但刷新页面会重建，关了就白关。

// 从「邮箱列表 → 使用」跳转过来时，带上指定邮箱
onActivated(() => {
  if (route.query.email) {
    regEmail.value = String(route.query.email)
    recoveryEmails.value = [String(route.query.email)]
  }
  loadRecoveryAccounts()
  loadLatestRecoveryBatch()
})
onDeactivated(stopBatchPolling)

async function loadRecoveryAccounts() {
  try {
    const result = await listAccounts({
      limit: 500,
      offset: 0,
      workflow_scope: '',
    })
    recoveryAccounts.value = (result.items || []).filter((item) => item.relay_url)
  } catch (_) {
    recoveryAccounts.value = []
  }
}

async function loadLatestRecoveryBatch() {
  try {
    const { job } = await getLatestRecoveryBatch()
    if (!job?.id) return
    activeRecoveryBatchId.value = job.id
    recoveryBatchJob.value = job
    if (job.status === 'running') {
      mode.value = 'recovery'
      startBatchPolling(job.id)
    } else if (
      ['done', 'partial', 'failed'].includes(job.status)
      && !lastRunResult.value
    ) {
      applyBatchResult(job)
    }
  } catch (_) {
    // No previous batch.
  }
}

function applyBatchResult(job) {
  recoveryBatchJob.value = job
  lastRunResult.value = {
    email: `批量找回 ${job.items?.length || 0} 个邮箱`,
    credential_source: 'recovery',
    credential_source_label: '邮箱验证码找回',
    batch_total: job.items?.length || 0,
    batch_success: job.success_count || 0,
    batch_failure: job.failure_count || 0,
    batch_results: job.results || [],
    partial: job.status === 'partial',
    error: job.status === 'failed' ? (job.message || '批量找回失败') : '',
  }
}

async function pollRecoveryBatch(batchId) {
  try {
    const { job } = await getRecoveryBatch(batchId)
    if (!job) return
    recoveryBatchJob.value = job
    if (!['running', 'paused'].includes(job.status)) {
      stopBatchPolling()
      applyBatchResult(job)
      runtime.bumpData()
    }
  } catch (_) {
    // Keep polling through transient network failures.
  }
}

function startBatchPolling(batchId) {
  stopBatchPolling()
  pollRecoveryBatch(batchId)
  recoveryBatchTimer = setInterval(() => pollRecoveryBatch(batchId), 2500)
}

function stopBatchPolling() {
  if (recoveryBatchTimer) clearInterval(recoveryBatchTimer)
  recoveryBatchTimer = null
}

const recoveryBatchPercent = computed(() => {
  const total = Number(recoveryBatchJob.value?.items?.length || 0)
  if (!total) return 0
  return Math.min(100, Math.round(
    Number(recoveryBatchJob.value?.completed_count || 0) / total * 100,
  ))
})

async function run() {
  starting.value = true
  runtime.clearLogs()
  lastRunResult.value = null
  try {
    const r = await startRegister({
      email: regEmail.value.trim() || null,
      proxy: proxyText(form.value),
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: true,
      want_2fa: form.value.want2fa,
    })
    runtime.addLog(`[client] 启动注册 run_id=${r.run_id} email=${r.email}`, 'evt')
    runtime.streamRun(r.run_id)
  } catch (e) {
    ElMessage.error(e.message)
    lastRunResult.value = { error: e.message }
  } finally {
    starting.value = false
  }
}

async function copyField(email, field) {
  try {
    const { data } = await getRegistered(email)
    const val = data[field] || ''
    if (!val) { ElMessage.warning(`${field} 为空`); return }
    await copyText(val)
  } catch (e) {
    ElMessage.error('加载凭证失败: ' + e.message)
  }
}

async function runRecovery() {
  let items = []
  if (recoverySource.value === 'manual') {
    for (const rawLine of recoveryRelay.value.split(/\r?\n/)) {
      const line = rawLine.trim()
      if (!line) continue
      const parts = line.split('----')
      if (parts.length !== 2 || !parts[0].includes('@') || !/^https?:\/\//i.test(parts[1].trim())) {
        ElMessage.warning(`接码地址格式错误：${line}`)
        return
      }
      items.push({ email: parts[0].trim(), relay_url: parts[1].trim() })
    }
  } else {
    items = recoveryEmails.value.map((email) => ({ email, relay_url: '' }))
  }
  if (!items.length) {
    ElMessage.warning(recoverySource.value === 'manual'
      ? '请按 email----接码地址 每行一个填写'
      : '请选择至少一个邮箱')
    return
  }
  recoveryBusy.value = true
  runtime.clearLogs()
  lastRunResult.value = null
  try {
    const result = await startRecoveryBatch({
      items,
      proxy: proxyText(form.value),
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 180,
      want_2fa: recoveryWant2fa.value,
      new_password: recoveryPassword.value.trim(),
      workflow_scope: recoveryScope.value,
    })
    activeRecoveryBatchId.value = result.batch_id
    recoveryBatchJob.value = {
      id: result.batch_id,
      status: 'running',
      items: result.items || items,
      completed_count: 0,
      success_count: 0,
      failure_count: 0,
    }
    startBatchPolling(result.batch_id)
    runtime.addLog(
      `[client] 启动批量账号找回 run_id=${result.run_id} 共 ${items.length} 个邮箱`,
      'evt',
    )
    runtime.streamRun(result.run_id)
  } catch (e) {
    ElMessage.error(e.message)
    lastRunResult.value = { error: e.message, credential_source: 'recovery' }
  } finally {
    recoveryBusy.value = false
  }
}
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px">
              <span class="section-title" style="margin: 0">账号注册与找回</span>
              <el-radio-group v-model="mode" size="small">
                <el-radio-button value="register">新账号注册</el-radio-button>
                <el-radio-button value="recovery">邮箱验证码找回</el-radio-button>
              </el-radio-group>
            </div>
          </template>
          <el-form v-if="mode === 'register'" label-position="top">
            <el-form-item label="邮箱（留空 = 自动 claim 下一个 available）">
              <el-input v-model="regEmail" placeholder="留空 = 自动选号 / 或填指定邮箱" clearable />
            </el-form-item>
            <el-form-item label="本次使用的单个代理（可从代理池选，或手动输入；直连留空）">
              <el-select
                v-model="form.proxy" filterable clearable allow-create default-first-option
                :reserve-keyword="false" placeholder="socks5://user:pass@host:1080"
                style="width: 100%"
              >
                <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
              </el-select>
              <div class="hint" style="margin-top: 4px">
                Plus 检测、自动批量的兜底代理都复用这里；批量并发轮换请到「代理池」页管理。
              </div>
            </el-form-item>
            <el-form-item label="OTP 等待秒数">
              <el-input-number v-model="form.otpTimeout" :min="10" :max="600" />
            </el-form-item>
            <el-form-item>
              <div style="display: flex; align-items: center; gap: 10px">
                <el-switch v-model="form.want2fa" />
                <span>注册成功后自动绑定 2FA（TOTP）</span>
              </div>
              <div class="hint" style="margin-top: 6px; line-height: 1.5">
                默认开。绑定不可逆、即刻生效：<b>之后该号所有登录都需 6 位动态码</b>；
                secret 仅在绑定时下发<b>一次</b>、服务端取不回，
                请在下方结果或「注册结果」页<b>立刻复制导出</b>并录入验证器，丢失 = 该号 2FA 永久锁死。
                仅对<b>有密码</b>的号生效，无密码号会自动跳过。
              </div>
            </el-form-item>
            <el-button type="primary" :loading="starting || runningSingle" @click="run">
              开始注册
            </el-button>
          </el-form>

          <el-form v-else label-position="top">
            <el-alert
              type="info" :closable="false" show-icon style="margin-bottom: 14px"
              title="不会新建账号：通过邮箱验证码登录已有账号，自动设置新密码，并在缺少 2FA 时补齐 TOTP。"
            />
            <el-form-item label="接码来源">
              <el-radio-group v-model="recoverySource">
                <el-radio-button value="pool">使用号池隐私邮箱</el-radio-button>
                <el-radio-button value="manual">手填接码地址</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="recoverySource === 'pool'" label="批量选择邮箱">
              <el-select
                v-model="recoveryEmails"
                multiple filterable clearable allow-create collapse-tags
                :max-collapse-tags="5"
                placeholder="可选择多个带隐私邮箱取件链接的账号"
                style="width: 100%"
              >
                <el-option
                  v-for="item in recoveryAccounts"
                  :key="item.email"
                  :label="`${item.email} · ${item.workflow_scope === 'integration' ? '一体化' : '原有'}`"
                  :value="item.email"
                />
              </el-select>
              <div class="hint" style="margin-top: 6px">
                已选择 {{ recoveryEmails.length }} 个邮箱；后端会按顺序逐个执行。
              </div>
            </el-form-item>
            <el-form-item v-if="recoverySource === 'manual'" label="邮箱接码地址">
              <el-input
                v-model="recoveryRelay"
                type="textarea"
                :rows="5"
                placeholder="每行一个：email----https://... 隐私邮箱取件链接"
              />
            </el-form-item>
            <el-form-item label="代理（可选）">
              <el-select
                v-model="form.proxy" filterable clearable allow-create default-first-option
                :reserve-keyword="false" placeholder="从代理池选择，留空直连"
                style="width: 100%"
              >
                <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
              </el-select>
            </el-form-item>
            <el-form-item label="OTP 等待秒数">
              <el-input-number v-model="form.otpTimeout" :min="30" :max="600" />
            </el-form-item>
            <el-form-item label="新密码（留空自动生成）">
              <el-input
                v-model="recoveryPassword"
                type="password"
                show-password
                clearable
                placeholder="留空则生成 16 位强密码"
              />
              <div class="hint" style="margin-top: 6px">
                批量找回建议留空，每个账号会生成独立密码。
              </div>
            </el-form-item>
            <el-form-item label="找回结果归属">
              <el-radio-group v-model="recoveryScope">
                <el-radio-button value="legacy">原有流程</el-radio-button>
                <el-radio-button value="integration">一体化流程</el-radio-button>
              </el-radio-group>
              <div class="hint" style="margin-top: 6px">
                如果邮箱已在号池中，则自动沿用它在号池中的归属。
              </div>
            </el-form-item>
            <el-form-item>
              <div style="display: flex; align-items: center; gap: 10px">
                <el-switch v-model="recoveryWant2fa" />
                <span>缺少 2FA 时自动绑定 TOTP</span>
              </div>
            </el-form-item>
            <el-button
              type="primary"
              :loading="recoveryBusy || runningSingle"
              :disabled="recoveryBatchJob?.status === 'running'"
              @click="runRecovery"
            >
              {{ recoverySource === 'manual'
                ? '批量导入并找回'
                : `找回选中的 ${recoveryEmails.length} 个邮箱` }}
            </el-button>
            <div
              v-if="recoveryBatchJob && recoveryBatchJob.status === 'running'"
              class="recovery-progress"
            >
              <el-progress :percentage="recoveryBatchPercent" />
              <div class="hint">
                已完成 {{ recoveryBatchJob.completed_count || 0 }}
                / {{ recoveryBatchJob.items?.length || 0 }}
                <template v-if="recoveryBatchJob.current_email">
                  · 当前 {{ recoveryBatchJob.current_email }}
                </template>
              </div>
            </div>
          </el-form>

          <el-alert
            v-if="lastRunResult && !lastRunResult.error"
            type="success" :closable="false" style="margin-top: 14px"
          >
            {{ lastRunResult.credential_source_label || '自动注册' }}完成 {{ lastRunResult.email }}
            <el-tag
              size="small"
              :type="lastRunResult.credential_source === 'recovery' ? 'warning' : 'success'"
              style="margin-left: 8px"
            >
              {{ lastRunResult.credential_source === 'recovery' ? '找回账号' : '原生成账号' }}
            </el-tag>
            <el-tag
              v-if="lastRunResult.two_fa_status"
              size="small"
              :type="lastRunResult.two_fa_status === 'failed' ? 'danger' : 'info'"
              style="margin-left: 6px"
            >
              {{
                lastRunResult.two_fa_status === 'created' ? '已补绑2FA'
                  : lastRunResult.two_fa_status === 'existing' ? '原有2FA'
                    : lastRunResult.two_fa_status === 'failed' ? '2FA失败'
                      : lastRunResult.two_fa_status
              }}
            </el-tag>
            (access_token len={{ lastRunResult.access_token_len }}{{ lastRunResult.partial ? ', 部分凭证' : '' }})
            <div v-if="lastRunResult.batch_total" style="margin-top: 10px">
              <div style="display: flex; gap: 8px; margin-bottom: 8px">
                <el-tag type="success">成功 {{ lastRunResult.batch_success }}</el-tag>
                <el-tag type="danger">失败 {{ lastRunResult.batch_failure }}</el-tag>
                <el-tag v-if="activeRecoveryBatchId" type="info">
                  任务 {{ activeRecoveryBatchId }}
                </el-tag>
              </div>
              <div class="batch-recovery-results">
                <div
                  v-for="item in lastRunResult.batch_results || []"
                  :key="item.email"
                  class="batch-recovery-row"
                >
                  <span class="mono">{{ item.email }}</span>
                  <el-tag size="small" :type="item.ok ? 'success' : 'danger'">
                    {{ item.ok ? '成功' : '失败' }}
                  </el-tag>
                  <span v-if="item.error" class="hint batch-error">{{ item.error }}</span>
                </div>
              </div>
            </div>
            <div v-if="lastRunResult.password" class="cred-line">
              <span class="cred-label">密码</span><code class="cred-val">{{ lastRunResult.password }}</code>
            </div>
            <div v-else class="cred-line hint">该号未设置密码（服务端未走密码注册流程）</div>
            <div v-if="lastRunResult.totp_secret" class="cred-line">
              <span class="cred-label">2FA</span><code class="cred-val">{{ lastRunResult.totp_secret }}</code>
              <span class="hint" style="margin-left: 6px">仅此一次！务必复制录入验证器</span>
            </div>
            <div
              v-else-if="lastRunResult.two_fa_status === 'existing'"
              class="cred-line hint"
            >
              账号已有 TOTP 2FA；服务端不能重新取回旧 secret，请使用原验证器。
            </div>
            <div style="margin-top: 8px">
              <el-button size="small" @click="copyText(lastRunResult.email)">复制邮箱</el-button>
              <template v-if="lastRunResult.password">
                <el-button size="small" type="primary" @click="copyText(lastRunResult.password)">复制密码</el-button>
                <el-button size="small" @click="copyText(lastRunResult.email + '----' + lastRunResult.password)">
                  复制 邮箱----密码
                </el-button>
              </template>
              <el-button v-if="lastRunResult.access_token_len > 0" size="small"
                         @click="copyField(lastRunResult.email, 'access_token')">复制 access_token</el-button>
              <el-button v-if="lastRunResult.totp_secret" size="small" type="warning"
                         @click="copyText(lastRunResult.totp_secret)">复制 2FA secret</el-button>
            </div>
          </el-alert>
          <el-alert
            v-else-if="lastRunResult && lastRunResult.error"
            type="error" :closable="false" style="margin-top: 14px"
          >
            <template #title>{{ lastRunResult.error }}</template>
            <div v-if="lastRunResult.batch_results?.length" class="batch-recovery-results">
              <div
                v-for="item in lastRunResult.batch_results"
                :key="item.email"
                class="batch-recovery-row"
              >
                <span class="mono">{{ item.email }}</span>
                <el-tag size="small" :type="item.ok ? 'success' : 'danger'">
                  {{ item.ok ? '成功' : '失败' }}
                </el-tag>
                <span v-if="item.error" class="hint batch-error">{{ item.error }}</span>
              </div>
            </div>
          </el-alert>
        </el-card>
      </el-col>

      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <LogPanel />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.batch-recovery-results {
  max-height: 220px;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}
.batch-recovery-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 5px 10px;
  align-items: center;
  padding: 7px 10px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.batch-recovery-row:last-child { border-bottom: 0; }
.batch-recovery-row > .batch-error {
  grid-column: 1 / -1;
  overflow-wrap: anywhere;
}
.recovery-progress {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
}
</style>

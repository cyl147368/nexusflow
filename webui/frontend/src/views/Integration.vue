<script setup>
import { computed, onActivated, onDeactivated, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  createICloudAccount,
  deleteICloudAlias,
  getIntegrationConfig,
  getIntegrationStatus,
  listICloudAliases,
  listICloudAccounts,
  normalizeCookie,
  saveIntegrationConfig,
  startIntegration,
  testICloud,
  testMailHub,
} from '@/api/integration'
import { fmtTime } from '@/api/request'

const loading = ref(true)
const saving = ref(false)
const testing = ref('')
const running = ref(false)
const setupBusy = ref(false)
const activeStep = ref(0)
const accounts = ref([])
const aliases = ref([])
const aliasesBusy = ref(false)
const job = ref({ status: 'idle', stage: 'idle', logs: [] })
let timer = null

const form = ref({
  mailhub_url: 'http://mail-hub:8090',
  mailhub_token: '',
  icloud_url: 'http://icloud-prime:8081',
  icloud_token: '',
  icloud_account_id: '',
  proxy: '',
  otp_timeout: 180,
  want_access_token: true,
  want_session_token: true,
  want_refresh_token: true,
  want_2fa: false,
})

const accountForm = ref({
  name: 'NexusFlow iCloud',
  host: 'icloud.com',
  real_email: '',
  icloud_email: '',
  cookies: '',
  password: '',
  otp_code: '',
  app_password: '',
  proxy: '',
})

const statusType = computed(() => ({
  done: 'success',
  running: 'warning',
  failed: 'danger',
}[job.value.status] || 'info'))

const stageLabel = computed(() => ({
  idle: '等待启动',
  starting: '准备中',
  creating_alias: '创建 iCloud 隐藏邮箱',
  registering_mailbox: '登记邮件中转',
  filling_pool: '写入邮箱池',
  registering_main: '启动主项目注册',
  waiting_registration: '等待注册完成',
  completed: '流程完成',
  failed: '流程失败',
  registration_failed: '注册失败',
  registration_timeout: '等待注册超时',
}[job.value.stage] || job.value.stage))

function configPayload() {
  const payload = { ...form.value }
  if (payload.mailhub_token === '***') payload.mailhub_token = ''
  if (payload.icloud_token === '***') payload.icloud_token = ''
  return payload
}

async function load() {
  loading.value = true
  try {
    const r = await getIntegrationConfig()
    const cfg = r.config || {}
    form.value = { ...form.value, ...cfg }
    job.value = r.job || job.value
    if (cfg.icloud_account_id) activeStep.value = 2
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function save({ quiet = false } = {}) {
  saving.value = true
  try {
    const r = await saveIntegrationConfig(configPayload())
    form.value = { ...form.value, ...(r.config || {}) }
    if (!quiet) ElMessage.success('配置已保存')
  } catch (e) {
    ElMessage.error(e.message)
    throw e
  } finally {
    saving.value = false
  }
}

async function runTest(target) {
  testing.value = target
  try {
    await save({ quiet: true })
    if (target === 'mailhub') {
      const r = await testMailHub()
      ElMessage.success(r.message)
    } else {
      const r = await testICloud()
      accounts.value = r.accounts || []
      ElMessage.success(r.message)
    }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    testing.value = ''
  }
}

async function refreshAccounts() {
  try {
    await save({ quiet: true })
    const r = await listICloudAccounts()
    accounts.value = r.accounts || []
    if (!accounts.value.length) ElMessage.warning('iCloud 服务里还没有账号，请先添加')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function setupAccount() {
  setupBusy.value = true
  try {
    await save({ quiet: true })
    const r = await createICloudAccount(accountForm.value)
    form.value.icloud_account_id = r.account_id
    await save({ quiet: true })
    await refreshAccounts()
    activeStep.value = 2
    ElMessage.success(`iCloud 账号已就绪：${r.account_id}`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    setupBusy.value = false
  }
}

async function normalizeAccountCookie() {
  if (!accountForm.value.cookies.trim()) {
    return ElMessage.warning('请先粘贴 Cookie、Cookie JSON 或 DevTools cURL')
  }
  try {
    const r = await normalizeCookie(accountForm.value.cookies)
    accountForm.value.cookies = r.text
    ElMessage.success(`已识别 ${r.count} 个 Cookie，其中包含登录凭证`)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function loadAliases() {
  aliasesBusy.value = true
  try {
    const r = await listICloudAliases(form.value.icloud_account_id)
    aliases.value = r.aliases || []
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    aliasesBusy.value = false
  }
}

async function removeAlias(alias) {
  const id = alias.anonymousId || alias.anonymous_id || alias.id
  if (!id) return
  try {
    await deleteICloudAlias({
      account_id: form.value.icloud_account_id,
      anonymous_id: id,
    })
    aliases.value = aliases.value.filter(
      (item) => (item.anonymousId || item.anonymous_id || item.id) !== id,
    )
    ElMessage.success('iCloud 隐藏邮箱已删除')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function refreshStatus() {
  try {
    const r = await getIntegrationStatus()
    job.value = r.job || job.value
    running.value = job.value.status === 'running'
  } catch (_) {
    // 轮询失败不打断当前页面。
  }
}

async function start() {
  try {
    await save({ quiet: true })
    const r = await startIntegration()
    job.value = r.job || {}
    running.value = true
    activeStep.value = 2
    startPolling()
    ElMessage.success('一体化流程已启动')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function startPolling() {
  if (timer) clearInterval(timer)
  timer = setInterval(refreshStatus, 1500)
}

function stopPolling() {
  if (timer) clearInterval(timer)
  timer = null
}

onActivated(() => {
  load()
  startPolling()
})
onDeactivated(stopPolling)
load()
startPolling()
</script>

<template>
  <div class="page integration-page" v-loading="loading">
    <div class="hero">
      <div>
        <div class="eyebrow">NexusFlow / Unified Automation</div>
        <h1>一条链完成隐藏邮箱与自动注册</h1>
        <p>配置邮件中转、iCloud 与主项目参数，然后启动。账号池、代理、SMS、2FA、导出仍沿用主项目能力。</p>
      </div>
      <el-tag :type="statusType" size="large" effect="dark">{{ stageLabel }}</el-tag>
    </div>

    <el-steps :active="activeStep" finish-status="success" align-center class="steps">
      <el-step title="邮件服务器" description="中转取件接口" />
      <el-step title="iCloud" description="Cookie / App 密码" />
      <el-step title="主项目" description="一键启动" />
    </el-steps>

    <div class="grid">
      <el-card shadow="never" class="config-card">
        <template #header>
          <div class="card-head">
            <span>1. 邮件服务器</span>
            <el-button size="small" :loading="testing === 'mailhub'" @click="runTest('mailhub')">测试</el-button>
          </div>
        </template>
        <el-form label-position="top">
          <el-form-item label="内网服务地址">
            <el-input v-model="form.mailhub_url" placeholder="http://mail-hub:8090" />
          </el-form-item>
          <el-form-item label="管理员 Token">
            <el-input v-model="form.mailhub_token" type="password" show-password
              placeholder="已设置时留空不修改" />
          </el-form-item>
        </el-form>
        <div class="hint">服务会生成 <code>email ---- relay_url</code>，再写入主项目邮箱池。</div>
      </el-card>

      <el-card shadow="never" class="config-card">
        <template #header>
          <div class="card-head">
            <span>2. iCloud 隐藏邮箱</span>
            <el-button size="small" :loading="testing === 'icloud'" @click="runTest('icloud')">测试</el-button>
          </div>
        </template>
        <el-form label-position="top">
          <el-form-item label="内网服务地址">
            <el-input v-model="form.icloud_url" placeholder="http://icloud-prime:8081" />
          </el-form-item>
          <el-form-item label="API Token">
            <el-input v-model="form.icloud_token" type="password" show-password
              placeholder="已设置时留空不修改" />
          </el-form-item>
          <el-form-item label="已有账号">
            <el-select v-model="form.icloud_account_id" clearable filterable
              placeholder="选择 iCloud 账号" style="width: 100%">
              <el-option v-for="a in accounts" :key="a.id" :value="a.id"
                :label="`${a.name || a.real_email || a.icloud_email || a.id} · ${a.status || ''}`" />
            </el-select>
          </el-form-item>
        </el-form>
        <el-button text type="primary" @click="refreshAccounts">刷新账号列表</el-button>
      </el-card>

      <el-card shadow="never" class="config-card wide">
        <template #header>
          <div class="card-head">
            <span>添加 / 修复 iCloud 账号</span>
            <el-button size="small" :loading="setupBusy" @click="setupAccount">保存到 iCloud 服务</el-button>
          </div>
        </template>
        <el-form label-position="top" class="account-form">
          <el-form-item label="账号名称"><el-input v-model="accountForm.name" /></el-form-item>
          <el-form-item label="区域">
            <el-select v-model="accountForm.host">
              <el-option label="国际版 icloud.com" value="icloud.com" />
              <el-option label="中国版 icloud.com.cn" value="icloud.com.cn" />
            </el-select>
          </el-form-item>
          <el-form-item label="Apple ID"><el-input v-model="accountForm.real_email" /></el-form-item>
          <el-form-item label="iCloud 邮箱"><el-input v-model="accountForm.icloud_email" /></el-form-item>
          <el-form-item label="App 专用密码">
            <el-input v-model="accountForm.app_password" type="password" show-password />
          </el-form-item>
          <el-form-item label="账号 Cookie">
            <el-input v-model="accountForm.cookies" type="textarea" :rows="4"
              placeholder="可直接粘贴 DevTools Copy as cURL、Cookie Header 或 Cookie JSON" />
          </el-form-item>
        </el-form>
        <div class="cookie-actions">
          <el-button text type="primary" @click="normalizeAccountCookie">识别并整理 Cookie</el-button>
          <span class="hint">DevTools → Network → 右键请求 → Copy → Copy as cURL，然后直接粘贴。</span>
        </div>
      </el-card>

      <el-card shadow="never" class="config-card wide">
        <template #header>
          <div class="card-head">
            <span>iCloud 隐藏邮箱管理</span>
            <el-button size="small" :loading="aliasesBusy" @click="loadAliases">读取别名列表</el-button>
          </div>
        </template>
        <el-table :data="aliases" size="small" empty-text="点击读取别名列表">
          <el-table-column prop="email" label="隐藏邮箱" min-width="220" />
          <el-table-column prop="label" label="标签" min-width="140" />
          <el-table-column prop="active" label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.active === false ? 'info' : 'success'" size="small">
                {{ row.active === false ? '停用' : '启用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" align="right">
            <template #default="{ row }">
              <el-button link type="danger" @click="removeAlias(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never" class="config-card">
        <template #header>
          <div class="card-head"><span>3. 主项目注册参数</span></div>
        </template>
        <el-form label-position="top">
          <el-form-item label="代理 URL">
            <el-input v-model="form.proxy" placeholder="socks5://user:pass@host:port，可留空" />
          </el-form-item>
          <el-form-item label="OTP 超时（秒）">
            <el-input-number v-model="form.otp_timeout" :min="60" :max="600" />
          </el-form-item>
          <div class="switches">
            <el-checkbox v-model="form.want_access_token">Access Token</el-checkbox>
            <el-checkbox v-model="form.want_session_token">Session Token</el-checkbox>
            <el-checkbox v-model="form.want_refresh_token">Refresh Token</el-checkbox>
            <el-checkbox v-model="form.want_2fa">注册后绑定 2FA</el-checkbox>
          </div>
        </el-form>
      </el-card>

      <el-card shadow="never" class="run-card">
        <template #header>
          <div class="card-head">
            <span>运行状态</span>
            <el-tag :type="statusType" size="small">{{ job.status || 'idle' }}</el-tag>
          </div>
        </template>
        <div class="run-meta">
          <div><span>当前阶段</span><b>{{ stageLabel }}</b></div>
          <div v-if="job.email"><span>隐藏邮箱</span><b>{{ job.email }}</b></div>
          <div v-if="job.run_id"><span>主任务 Run ID</span><b>{{ job.run_id }}</b></div>
          <div v-if="job.relay_url"><span>取件链接</span><b class="break">{{ job.relay_url }}</b></div>
          <div v-if="job.error"><span>错误</span><b class="error">{{ job.error }}</b></div>
        </div>
        <el-scrollbar height="220px" class="logs">
          <div v-for="(line, i) in job.logs || []" :key="i" class="log-line">
            <time>{{ fmtTime(line.at) }}</time><span>{{ line.message }}</span>
          </div>
          <div v-if="!(job.logs || []).length" class="hint">启动后这里显示阶段日志。</div>
        </el-scrollbar>
      </el-card>
    </div>

    <div class="actions">
      <el-button :loading="saving" @click="save()">保存全部配置</el-button>
      <el-button type="primary" size="large" :loading="running" :disabled="running" @click="start">
        一键启动完整流程
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.integration-page { max-width: 1280px; margin: 0 auto; }
.hero {
  display: flex; justify-content: space-between; align-items: center; gap: 24px;
  padding: 26px 30px; border-radius: 16px; color: white;
  background: radial-gradient(circle at 90% 20%, #31589c 0, transparent 35%),
              linear-gradient(135deg, #10172b 0%, #19294f 100%);
}
.hero h1 { margin: 6px 0 8px; font-size: 28px; }
.hero p { margin: 0; max-width: 720px; color: #b9c7e8; }
.eyebrow { color: #7da9ff; font-size: 12px; letter-spacing: 1.8px; text-transform: uppercase; }
.steps { margin: 24px 0; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.config-card.wide { grid-column: 1 / -1; }
.card-head { display: flex; justify-content: space-between; align-items: center; font-weight: 600; }
.account-form { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0 16px; }
.account-form :deep(.el-form-item:nth-child(6)) { grid-column: 1 / -1; }
.switches { display: flex; gap: 10px; flex-wrap: wrap; }
.run-card { border: 1px solid var(--el-color-primary-light-7); }
.run-meta { display: grid; gap: 10px; margin-bottom: 16px; }
.run-meta div { display: grid; grid-template-columns: 100px 1fr; gap: 10px; }
.run-meta span { color: var(--el-text-color-secondary); }
.break { overflow-wrap: anywhere; }
.error { color: var(--el-color-danger); }
.logs { background: var(--el-fill-color-lighter); border-radius: 8px; padding: 8px 12px; }
.log-line { display: flex; gap: 12px; padding: 5px 0; font-size: 13px; }
.log-line time { color: var(--el-text-color-secondary); white-space: nowrap; }
.actions {
  position: sticky; bottom: 12px; display: flex; justify-content: flex-end; gap: 12px;
  margin-top: 18px; padding: 14px 18px; border: 1px solid var(--el-border-color);
  border-radius: 12px; background: var(--el-bg-color); box-shadow: var(--el-box-shadow-light);
}
.hint { color: var(--el-text-color-secondary); font-size: 13px; }
.cookie-actions { display: flex; align-items: center; gap: 12px; margin-top: -6px; }
code { color: var(--el-color-primary); }
@media (max-width: 900px) {
  .grid { grid-template-columns: 1fr; }
  .config-card.wide { grid-column: auto; }
  .account-form { grid-template-columns: 1fr 1fr; }
  .hero { align-items: flex-start; flex-direction: column; }
}
</style>

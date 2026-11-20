<script setup>
import { computed, onActivated, onDeactivated, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createICloudAccount,
  deleteICloudAlias,
  getIntegrationConfig,
  getIntegrationStatus,
  getMailHubInfo,
  listICloudAliases,
  listICloudAccounts,
  normalizeCookie,
  saveIntegrationConfig,
  setICloudAliasState,
  startIntegration,
  testICloud,
  testMailHub,
  testReceiver,
} from '@/api/integration'
import { copyText, fmtTime } from '@/api/request'

const loading = ref(true)
const saving = ref(false)
const testing = ref('')
const mailHubInfo = ref({})
const running = ref(false)
const setupBusy = ref(false)
const activeStep = ref(0)
const accounts = ref([])
const aliases = ref([])
const aliasesBusy = ref(false)
const aliasSearch = ref('')
const aliasStatus = ref('all')
const aliasPage = ref(1)
const aliasPageSize = ref(20)
const aliasActionBusy = ref('')
const job = ref({ status: 'idle', stage: 'idle', logs: [] })
let timer = null

const form = ref({
  mailhub_url: 'http://mail-hub:8090',
  mailhub_token: '',
  icloud_url: 'http://icloud-prime:8081',
  icloud_token: '',
  icloud_account_id: '',
  receiver_mode: 'icloud',
  imap_host: '',
  imap_port: 993,
  imap_email: '',
  imap_password: '',
  imap_folder: 'INBOX',
  imap_ssl: true,
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

const filteredAliases = computed(() => {
  const keyword = aliasSearch.value.trim().toLowerCase()
  return aliases.value.filter((item) => {
    if (aliasStatus.value === 'active' && item.active === false) return false
    if (aliasStatus.value === 'inactive' && item.active !== false) return false
    if (!keyword) return true
    return `${item.email || ''} ${item.label || ''}`.toLowerCase().includes(keyword)
  })
})

const pagedAliases = computed(() => {
  const start = (aliasPage.value - 1) * aliasPageSize.value
  return filteredAliases.value.slice(start, start + aliasPageSize.value)
})

const activeAliasCount = computed(
  () => aliases.value.filter((item) => item.active !== false).length,
)
const inactiveAliasCount = computed(
  () => aliases.value.filter((item) => item.active === false).length,
)
const smtpPublicAddress = computed(() => {
  if (!mailHubInfo.value.public_url || !mailHubInfo.value.smtp_port) return '启动后显示'
  try {
    const url = new URL(mailHubInfo.value.public_url)
    return `${url.hostname}:${mailHubInfo.value.smtp_port}`
  } catch (_) {
    return `${mailHubInfo.value.public_url}:${mailHubInfo.value.smtp_port}`
  }
})

function aliasId(alias) {
  return alias?.anonymousId || alias?.anonymous_id || alias?.id || ''
}

function aliasTime(value) {
  if (!value) return '—'
  const numeric = Number(value)
  const date = Number.isFinite(numeric)
    ? new Date(numeric > 10_000_000_000 ? numeric : numeric * 1000)
    : new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false })
}

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
  try {
    const info = await getMailHubInfo()
    mailHubInfo.value = info.info || {}
  } catch (_) {
    mailHubInfo.value = {}
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

async function testImapReceiver() {
  testing.value = 'receiver'
  try {
    await save({ quiet: true })
    const result = await testReceiver()
    ElMessage.success(result.message || 'IMAP 收件箱连接正常')
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
    aliasPage.value = 1
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    aliasesBusy.value = false
  }
}

async function removeAlias(alias) {
  const id = aliasId(alias)
  if (!id) return
  try {
    await ElMessageBox.confirm(
      `确定永久删除 ${alias.email}？删除后这个隐藏邮箱地址不能恢复。`,
      '删除隐藏邮箱',
      { type: 'warning', confirmButtonText: '永久删除', cancelButtonText: '取消' },
    )
  } catch (_) {
    return
  }
  aliasActionBusy.value = `delete:${id}`
  try {
    await deleteICloudAlias({
      account_id: form.value.icloud_account_id,
      anonymous_id: id,
    })
    aliases.value = aliases.value.filter((item) => aliasId(item) !== id)
    ElMessage.success('iCloud 隐藏邮箱已删除')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    aliasActionBusy.value = ''
  }
}

async function toggleAlias(alias) {
  const id = aliasId(alias)
  if (!id) return
  const nextActive = alias.active === false
  if (!nextActive) {
    try {
      await ElMessageBox.confirm(
        `停用后，发送到 ${alias.email} 的新邮件将停止转发。地址会保留，可随时重新启用。`,
        '停用隐藏邮箱',
        { type: 'warning', confirmButtonText: '停用', cancelButtonText: '取消' },
      )
    } catch (_) {
      return
    }
  }
  aliasActionBusy.value = `state:${id}`
  try {
    await setICloudAliasState({
      account_id: form.value.icloud_account_id,
      anonymous_id: id,
      active: nextActive,
    })
    alias.active = nextActive
    ElMessage.success(nextActive ? '已恢复邮件转发' : '已暂停邮件转发')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    aliasActionBusy.value = ''
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
          <el-form-item label="实际收件箱">
            <el-radio-group v-model="form.receiver_mode">
              <el-radio-button value="icloud">iCloud 主收件箱</el-radio-button>
              <el-radio-button value="imap">外部 IMAP</el-radio-button>
            </el-radio-group>
          </el-form-item>
        </el-form>
        <div v-if="form.receiver_mode === 'imap'" class="receiver-form">
          <el-form label-position="top">
            <el-form-item label="IMAP 主机">
              <el-input v-model="form.imap_host" placeholder="例如 imap.qq.com" />
            </el-form-item>
            <el-form-item label="端口">
              <el-input-number v-model="form.imap_port" :min="1" :max="65535" />
            </el-form-item>
            <el-form-item label="实际收件邮箱">
              <el-input v-model="form.imap_email" placeholder="例如 123456@qq.com" />
            </el-form-item>
            <el-form-item label="IMAP 授权码 / App 密码">
              <el-input v-model="form.imap_password" type="password" show-password
                placeholder="已设置时留空不修改" />
            </el-form-item>
            <el-form-item label="收件文件夹">
              <el-input v-model="form.imap_folder" placeholder="INBOX" />
            </el-form-item>
            <el-form-item label="SSL">
              <el-switch v-model="form.imap_ssl" active-text="993 SSL" inactive-text="明文端口" />
            </el-form-item>
          </el-form>
          <div class="receiver-actions">
            <el-button size="small" :loading="testing === 'receiver'" @click="testImapReceiver">
              测试 IMAP 收件箱
            </el-button>
            <span class="hint">QQ/Gmail 需使用邮箱服务商生成的 IMAP 授权码，不是登录密码。</span>
          </div>
        </div>
        <el-alert v-else type="info" :closable="false" show-icon
          title="iCloud 主收件箱在下方“iCloud 接收邮箱”和“App 专用密码”中配置。" />
        <div class="mailhub-facts">
          <div>
            <span>SMTP 入站（备用）</span>
            <b>{{ mailHubInfo.smtp_enabled ? smtpPublicAddress : '启动后显示' }}</b>
          </div>
          <div>
            <span>Mail Hub 对外地址</span>
            <b>{{ mailHubInfo.public_url || '启动后显示' }}</b>
          </div>
          <div>
            <span>默认接收模式</span>
            <b>{{ form.receiver_mode === 'imap' ? '外部 IMAP 收件箱' : 'iCloud Prime 主收件箱' }}</b>
          </div>
        </div>
        <el-alert type="info" :closable="false" show-icon>
          <template #title>
            SMTP 是内建备用进站服务，不需要登录用户名和密码。当前 iCloud 流程直接读取下方配置的 iCloud 主收件箱。
          </template>
        </el-alert>
        <div class="hint mailhub-hint">
          系统会自动生成 <code>email ---- relay_url</code> 并写入邮箱池，无需手工登记收件邮箱。
        </div>
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
          <el-form-item label="iCloud 接收邮箱">
            <el-input v-model="accountForm.icloud_email" placeholder="例如 name@icloud.com" />
          </el-form-item>
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

      <el-card shadow="never" class="config-card wide alias-card">
        <template #header>
          <div class="alias-card-head">
            <div>
              <div class="alias-title">
                <span>iCloud 隐藏邮箱</span>
                <el-tag type="info" effect="plain" size="small">{{ aliases.length }} 个</el-tag>
              </div>
              <p>可搜索、复制、暂停转发或永久删除；备注来自创建时的标签。</p>
            </div>
            <el-button type="primary" plain :loading="aliasesBusy" @click="loadAliases">
              刷新列表
            </el-button>
          </div>
        </template>

        <div class="alias-stats">
          <div class="alias-stat">
            <span>全部地址</span>
            <strong>{{ aliases.length }}</strong>
          </div>
          <div class="alias-stat active">
            <span>转发中</span>
            <strong>{{ activeAliasCount }}</strong>
          </div>
          <div class="alias-stat inactive">
            <span>已停用</span>
            <strong>{{ inactiveAliasCount }}</strong>
          </div>
          <div class="alias-account">
            当前账号
            <b>{{ accounts.find((item) => item.id === form.icloud_account_id)?.name || form.icloud_account_id || '未选择' }}</b>
          </div>
        </div>

        <div class="alias-toolbar">
          <el-input
            v-model="aliasSearch" clearable
            placeholder="搜索邮箱或备注"
            class="alias-search"
            @input="aliasPage = 1"
          />
          <el-select v-model="aliasStatus" class="alias-filter" @change="aliasPage = 1">
            <el-option label="全部状态" value="all" />
            <el-option label="转发中" value="active" />
            <el-option label="已停用" value="inactive" />
          </el-select>
          <span class="alias-tip">停用仅暂停转发，地址仍保留；删除不可恢复。</span>
        </div>

        <el-table
          :data="pagedAliases"
          row-key="email"
          size="small"
          max-height="560"
          empty-text="没有可显示的隐藏邮箱"
          class="alias-table"
        >
          <el-table-column label="隐藏邮箱" min-width="270">
            <template #default="{ row }">
              <div class="alias-email-cell">
                <code>{{ row.email }}</code>
                <el-button link type="primary" @click="copyText(row.email)">复制</el-button>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="备注" min-width="190">
            <template #default="{ row }">
              <span v-if="row.label" class="alias-note">{{ row.label }}</span>
              <span v-else class="alias-note empty">未备注</span>
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="180">
            <template #default="{ row }">
              <span class="alias-time">{{ aliasTime(row.createdAt || row.created_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="转发状态" width="110" align="center">
            <template #default="{ row }">
              <span class="alias-status" :class="{ off: row.active === false }">
                <i></i>{{ row.active === false ? '已停用' : '转发中' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="155" align="right" fixed="right">
            <template #default="{ row }">
              <el-button
                link
                :type="row.active === false ? 'success' : 'warning'"
                :loading="aliasActionBusy === `state:${aliasId(row)}`"
                @click="toggleAlias(row)"
              >
                {{ row.active === false ? '启用' : '停用' }}
              </el-button>
              <el-button
                link type="danger"
                :loading="aliasActionBusy === `delete:${aliasId(row)}`"
                @click="removeAlias(row)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="alias-footer">
          <el-pagination
            v-model:current-page="aliasPage"
            v-model:page-size="aliasPageSize"
            :page-sizes="[10, 20, 50, 100]"
            :total="filteredAliases.length"
            layout="total, sizes, prev, pager, next"
            @size-change="aliasPage = 1"
          />
        </div>
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
.mailhub-facts {
  display: grid; grid-template-columns: 0.8fr 1.4fr 1fr; gap: 8px;
  margin: 12px 0;
}
.mailhub-facts div {
  padding: 9px 11px; border-radius: 8px;
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-lighter);
}
.mailhub-facts span { display: block; color: var(--el-text-color-secondary); font-size: 11px; }
.mailhub-facts b {
  display: block; margin-top: 4px; font-size: 12px;
  color: var(--el-text-color-primary); overflow-wrap: anywhere;
}
.mailhub-hint { margin-top: 10px; }
.receiver-form {
  padding: 14px; margin-bottom: 12px; border-radius: 10px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
}
.receiver-form :deep(.el-form) {
  display: grid; grid-template-columns: 1.35fr .7fr 1.2fr 1.2fr;
  gap: 0 12px;
}
.receiver-form :deep(.el-form-item:nth-child(4)) { grid-column: span 2; }
.receiver-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.alias-card {
  border: 1px solid var(--el-color-primary-light-8);
  background:
    radial-gradient(circle at 100% 0, var(--el-color-primary-light-9), transparent 28%),
    var(--el-bg-color);
}
.alias-card :deep(.el-card__header) { padding: 18px 20px; }
.alias-card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
.alias-title { display: flex; align-items: center; gap: 10px; font-size: 16px; font-weight: 700; }
.alias-card-head p { margin: 6px 0 0; color: var(--el-text-color-secondary); font-size: 13px; }
.alias-stats {
  display: grid; grid-template-columns: repeat(3, minmax(120px, 170px)) 1fr;
  gap: 10px; margin-bottom: 14px;
}
.alias-stat {
  padding: 11px 14px; border-radius: 10px; border: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-lighter);
  display: flex; justify-content: space-between; align-items: baseline;
}
.alias-stat span { color: var(--el-text-color-secondary); font-size: 12px; }
.alias-stat strong { font-size: 20px; color: var(--el-text-color-primary); }
.alias-stat.active strong { color: var(--el-color-success); }
.alias-stat.inactive strong { color: var(--el-text-color-secondary); }
.alias-account {
  align-self: center; justify-self: end; color: var(--el-text-color-secondary);
  font-size: 12px; overflow-wrap: anywhere;
}
.alias-account b { display: block; margin-top: 3px; color: var(--el-text-color-primary); }
.alias-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.alias-search { width: 280px; }
.alias-filter { width: 130px; }
.alias-tip { color: var(--el-text-color-secondary); font-size: 12px; }
.alias-table {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px; overflow: hidden;
}
.alias-table :deep(.el-table__header th) {
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
  font-weight: 600;
}
.alias-table :deep(.el-table__row) { transition: background-color .16s ease; }
.alias-table :deep(.el-table__row:hover > td.el-table__cell) {
  background: var(--el-color-primary-light-9) !important;
}
.alias-email-cell { display: flex; align-items: center; gap: 6px; }
.alias-email-cell code {
  color: var(--el-text-color-primary);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.alias-note { display: inline-block; max-width: 100%; color: var(--el-text-color-regular); overflow-wrap: anywhere; }
.alias-note.empty { color: var(--el-text-color-placeholder); font-style: italic; }
.alias-time { color: var(--el-text-color-secondary); font-size: 12px; white-space: nowrap; }
.alias-status {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 9px; border-radius: 999px;
  color: var(--el-color-success); background: var(--el-color-success-light-9);
  font-size: 12px; white-space: nowrap;
}
.alias-status i {
  width: 6px; height: 6px; border-radius: 50%;
  background: currentColor; box-shadow: 0 0 0 3px var(--el-color-success-light-8);
}
.alias-status.off { color: var(--el-text-color-secondary); background: var(--el-fill-color); }
.alias-status.off i { box-shadow: none; }
.alias-footer { display: flex; justify-content: flex-end; margin-top: 14px; }
@media (max-width: 900px) {
  .grid { grid-template-columns: 1fr; }
  .config-card.wide { grid-column: auto; }
  .account-form { grid-template-columns: 1fr 1fr; }
  .hero { align-items: flex-start; flex-direction: column; }
  .alias-stats { grid-template-columns: repeat(3, 1fr); }
  .alias-account { grid-column: 1 / -1; justify-self: start; }
  .alias-search { width: 100%; }
  .alias-card-head { flex-direction: column; }
  .mailhub-facts { grid-template-columns: 1fr; }
  .receiver-form :deep(.el-form) { grid-template-columns: 1fr 1fr; }
  .receiver-form :deep(.el-form-item:nth-child(4)) { grid-column: auto; }
}
</style>

<script setup>
import { onActivated, ref } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import {
  startRegister, startRecovery, getRegistered,
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
const recoveryEmail = ref('')
const recoverySource = ref('pool')
const recoveryRelay = ref('')
const recoveryPassword = ref('')
const recoveryWant2fa = ref(true)
const recoveryScope = ref('legacy')
const recoveryAccounts = ref([])
const recoveryBusy = ref(false)
// 2FA 默认开（主人要求每个号都绑）。绑定不可逆，所以留开关。
// 放在 form store（localStorage 持久化）而不是组件局部 ref —— 组件是
// keep-alive 的，切页不丢，但刷新页面会重建，关了就白关。

// 从「邮箱列表 → 使用」跳转过来时，带上指定邮箱
onActivated(() => {
  if (route.query.email) {
    regEmail.value = String(route.query.email)
    recoveryEmail.value = String(route.query.email)
  }
  loadRecoveryAccounts()
})

async function loadRecoveryAccounts() {
  try {
    const result = await listAccounts({
      limit: 200,
      offset: 0,
      workflow_scope: '',
    })
    recoveryAccounts.value = (result.items || []).filter((item) => item.relay_url)
  } catch (_) {
    recoveryAccounts.value = []
  }
}

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
  const email = recoveryEmail.value.trim()
  if (!email) {
    ElMessage.warning('请输入要找回的邮箱')
    return
  }
  recoveryBusy.value = true
  runtime.clearLogs()
  lastRunResult.value = null
  try {
    const result = await startRecovery({
      email,
      relay_url: recoverySource.value === 'manual'
        ? recoveryRelay.value.trim()
        : '',
      proxy: proxyText(form.value),
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 180,
      want_2fa: recoveryWant2fa.value,
      new_password: recoveryPassword.value.trim(),
      workflow_scope: recoveryScope.value,
    })
    runtime.addLog(
      `[client] 启动账号找回 run_id=${result.run_id} email=${result.email}`,
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
            <el-form-item label="要找回的邮箱">
              <el-input
                v-model="recoveryEmail"
                clearable
                placeholder="输入账号邮箱，或从下方隐私邮箱池选择"
              />
            </el-form-item>
            <el-form-item label="邮箱池快捷选择">
              <el-select
                v-model="recoveryEmail"
                filterable clearable allow-create
                placeholder="选择带隐私邮箱取件链接的账号"
                style="width: 100%"
              >
                <el-option
                  v-for="item in recoveryAccounts"
                  :key="item.email"
                  :label="`${item.email} · ${item.workflow_scope === 'integration' ? '一体化' : '原有'}`"
                  :value="item.email"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="接码来源">
              <el-radio-group v-model="recoverySource">
                <el-radio-button value="pool">使用号池隐私邮箱</el-radio-button>
                <el-radio-button value="manual">手填接码地址</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="recoverySource === 'manual'" label="邮箱接码地址">
              <el-input
                v-model="recoveryRelay"
                clearable
                placeholder="https://... 隐私邮箱取件链接"
              />
            </el-form-item>
            <div v-else class="hint" style="margin: -8px 0 14px">
              后端会从该邮箱的号池记录读取 <code>email----取件链接</code>。
            </div>
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
              @click="runRecovery"
            >
              通过邮箱验证码找回
            </el-button>
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
            type="error" :closable="false" style="margin-top: 14px" :title="lastRunResult.error"
          />
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

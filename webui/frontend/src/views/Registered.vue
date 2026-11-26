<script setup>
import { computed, onActivated, onDeactivated, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listRegistered, listUncheckedEmails, listRetestEmails, listTeamPromoEmails,
  getRegistered, deleteRegistered,
  bulkDeleteRegistered, bulkDeleteAccounts, checkPlus, checkTeamPromo, refreshAccessTokens,
  listExportFormats, exportRegistered, updateCredentials,
  getPlusSchedule, updatePlusSchedule, runPlusScheduleNow as runPlusScheduleNowApi,
} from '@/api/register'
import { copyText, fmtTime } from '@/api/request'
import { useFormStore } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'

const { form } = storeToRefs(useFormStore())
// 检测用的代理必须能从代理池里挑：以前这页只在代码里读 form.proxy，页面上
// 连个输入框都没有，主人在代理池换了密码，这里还在用 localStorage 里的旧值，
// 结果是 curl:(97) 代理鉴权被拒 → 静默降级直连 → 拿真实 IP 打 chatgpt.com。
const { list: proxyList } = storeToRefs(useProxyStore())
const runtime = useRuntimeStore()
// dataVersion 要走 storeToRefs 才保持响应（watch 用）；bumpData 是 action，直接从
// store 实例上取 —— storeToRefs 只转 state/getter，把 action 解构出来会丢 this。
const { dataVersion } = storeToRefs(runtime)

const PAGE_SIZE = 20
const rows = ref([])
const total = ref(0)
const page = ref(1)
const filter = ref('successful')
const workflowFilter = ref('integration')
const selected = ref([])
const loading = ref(false)
const checking = ref(false)
const teamChecking = ref(false)
const refreshingAT = ref(false)
const checkResult = ref('')
const teamResult = ref('')
const currentPlusTotal = ref(null)
const currentTeamTotal = ref(null)
const plusSchedule = ref({
  enabled: true,
  interval_hours: 3,
  scope: 'integration',
  egress_mode: 'direct',
  batch_size: 25,
  workers: 3,
  running: false,
  last_status: 'idle',
  last_counts: {},
  next_run_at: 0,
})
const scheduleSaving = ref(false)
const scheduleStarting = ref(false)
let scheduleTimer = null

const PLUS_TYPE = {
  plus_eligible: 'success', plus_active: 'primary', free: 'warning',
  // token_invalid（401 且响应体没有封号措辞）仍与 banned 分开显示——判据不同，
  // 不能混成一个。但配色从橙改红：AT 未到期却 401 = 被吊销，实测多半就是封号，
  // 橙色（=号还在）会让主人以为重新登录就能救回来。
  token_invalid: 'danger',
  banned: 'danger', error: 'danger',
}
const TEAM_TYPE = {
  team_eligible: 'success',
  team_ineligible: 'warning',
  banned: 'danger',
  token_invalid: 'danger',
  error: 'danger',
}
function plusOf(row) { return row.plus_check || null }
function teamOf(row) { return row.team_promo_check || null }
function teamReason(row) {
  const info = teamOf(row)
  if (!info) return ''
  if (info.redeemed) return '原因：该优惠已经兑换'
  return {
    prior_introductory_offer_redeemed: '原因：此前已兑换过入门优惠',
    promo_ineligible: '原因：不符合活动资格',
  }[info.ineligibility_reason] || (info.ineligibility_reason ? `原因：${info.ineligibility_reason}` : '')
}
function workflowLabel(scope) {
  return {
    legacy: '原有流程',
    integration: '一体化流程',
  }[scope] || '未分类'
}
function scheduleTime(value) {
  const timestamp = Number(value || 0)
  return timestamp > 0 ? fmtTime(timestamp) : '—'
}
const plusScheduleStatusText = computed(() => {
  if (plusSchedule.value.running) return '检测中'
  return {
    ok: '上次完成',
    failed: '上次失败',
    running: '检测中',
    idle: '等待首次运行',
  }[plusSchedule.value.last_status] || plusSchedule.value.last_status || '等待首次运行'
})
function credentialSourceLabel(row) {
  return row.credential_source_label || (
    row.credential_source === 'recovery' ? '邮箱验证码找回' : '自动注册'
  )
}

async function load(resetPage) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    let response
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        response = await listRegistered({
          limit: PAGE_SIZE,
          offset: (page.value - 1) * PAGE_SIZE,
          filter: filter.value,
          workflow_scope: workflowFilter.value,
        })
        break
      } catch (error) {
        // A service restart/update can briefly produce 502 before the new
        // worker is ready. Retry once instead of surfacing a spurious error.
        if (attempt > 0 || error.response?.status !== 502) throw error
        await new Promise((resolve) => setTimeout(resolve, 800))
      }
    }
    const { items, total: t } = response
    rows.value = items
    total.value = t
    await Promise.all([loadPlusTotal(), loadTeamTotal()])
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

async function loadPlusTotal() {
  try {
    const result = await listRegistered({
      limit: 1,
      offset: 0,
      filter: 'plus',
      workflow_scope: workflowFilter.value,
    })
    currentPlusTotal.value = Number(result.total || 0)
  } catch (_) {
    currentPlusTotal.value = null
  }
}

async function loadTeamTotal() {
  try {
    const result = await listRegistered({
      limit: 1,
      offset: 0,
      filter: 'team_eligible',
      workflow_scope: workflowFilter.value,
    })
    currentTeamTotal.value = Number(result.total || 0)
  } catch (_) {
    currentTeamTotal.value = null
  }
}

async function loadPlusSchedule() {
  try {
    const result = await getPlusSchedule()
    plusSchedule.value = { ...plusSchedule.value, ...(result.schedule || {}) }
  } catch (_) {
    // The table remains usable even if schedule status is temporarily unavailable.
  }
}

async function savePlusSchedule() {
  scheduleSaving.value = true
  try {
    const result = await updatePlusSchedule({
      enabled: plusSchedule.value.enabled,
      interval_hours: plusSchedule.value.interval_hours,
      scope: plusSchedule.value.scope,
      egress_mode: plusSchedule.value.egress_mode,
      batch_size: plusSchedule.value.batch_size,
      workers: plusSchedule.value.workers,
    })
    plusSchedule.value = { ...plusSchedule.value, ...(result.schedule || {}) }
    ElMessage.success(plusSchedule.value.enabled ? '定时 Plus 检测已保存' : '定时 Plus 检测已关闭')
  } catch (e) {
    ElMessage.error('保存定时检测失败: ' + e.message)
  } finally {
    scheduleSaving.value = false
  }
}

async function runPlusScheduleNow() {
  scheduleStarting.value = true
  try {
    const result = await runPlusScheduleNowApi()
    ElMessage.success(result.message || 'Plus 检测已启动')
    await loadPlusSchedule()
  } catch (e) {
    ElMessage.error('启动检测失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    scheduleStarting.value = false
  }
}

async function collectEmails(mode) {
  if (mode === 'selected') return selected.value.map((r) => r.email)
  if (mode === 'unchecked') {
    const { emails } = await listUncheckedEmails(workflowFilter.value)
    return emails
  }
  if (mode === 'all') {
    const { emails } = await listRetestEmails(workflowFilter.value)
    return emails
  }
  return rows.value.map((r) => r.email)
}

async function collectTeamEmails(mode) {
  if (mode === 'selected') return selected.value.map((row) => row.email)
  const { emails } = await listTeamPromoEmails(
    workflowFilter.value,
    mode === 'unchecked' ? 'unchecked' : 'all',
  )
  return emails
}

async function doCheck(mode) {
  checking.value = true
  checkResult.value = '正在准备待检测账号...'
  try {
    const emails = await collectEmails(mode)
    if (!emails.length) {
      ElMessage.info(mode === 'unchecked' ? '当前流程没有未检测账号' : '当前页没有可检测的号')
      return
    }
    // Keep every request below the API's 60s timeout. The backend checks one
    // account at a time, so sending hundreds in one request can look like a 502.
    const batchSize = 20
    const merged = {}
    let note = ''
    for (let start = 0; start < emails.length; start += batchSize) {
      const batch = emails.slice(start, start + batchSize)
      checkResult.value = `检查中... ${Math.min(start + batch.length, emails.length)}/${emails.length}`
      const response = await checkPlus(batch, '')
      Object.assign(merged, response.results || {})
      note = response.note || note
    }
    let plus = 0, free = 0, banned = 0, failed = 0, badToken = 0
    let noAt = 0, notFound = 0
    for (const [email, info] of Object.entries(merged)) {
      const row = rows.value.find((r) => r.email === email)
      if (row) row.plus_check = info
      if (info.status === 'plus_eligible' || info.status === 'plus_active') plus++
      else if (info.status === 'banned') banned++
      else if (info.status === 'free') free++
      else if (info.status === 'token_invalid') badToken++
      else if (info.status === 'no_at') noAt++
      else if (info.status === 'not_found') notFound++
      else if (info.status === 'error') failed++
    }
    // failed / note 不入库，只是这一次的现场说明：
    // 以前网络/代理挂了这里只会显示「0 可用Plus, 0 Free, 0 封号」，看不出是没检测成。
    // badToken 从 2026-08-10 起是**会入库**的结论，措辞也跟着改：
    // AT 没过期却 401 = 被吊销，大概率就是封号，不该再说得像只是要重新登录。
    const parts = [`本次检测: ${plus} 可领Plus, ${free} Free, ${banned} 封号`]
    if (badToken) parts.push(`${badToken} 个凭证失效（AT 被吊销，多半已封）`)
    if (noAt) parts.push(`${noAt} 个无AT，已跳过`)
    if (notFound) parts.push(`${notFound} 个记录已不存在`)
    if (failed) parts.push(`${failed} 个没检测成`)
    if (note) parts.push(note)
    checkResult.value = parts.join(' · ')
    await load()
  } catch (e) {
    checkResult.value = ''
    ElMessage.error('检查失败: ' + e.message)
  } finally { checking.value = false }
}

async function doTeamCheck(mode) {
  teamChecking.value = true
  teamResult.value = '正在准备待检测账号...'
  try {
    const emails = await collectTeamEmails(mode)
    if (!emails.length) {
      ElMessage.info(mode === 'unchecked' ? '当前流程没有未检测 Team 的账号' : '当前没有可检测的号')
      return
    }
    const batchSize = 20
    const merged = {}
    let note = ''
    for (let start = 0; start < emails.length; start += batchSize) {
      const batch = emails.slice(start, start + batchSize)
      teamResult.value = `检查 Team... ${Math.min(start + batch.length, emails.length)}/${emails.length}`
      const response = await checkTeamPromo(batch)
      Object.assign(merged, response.results || {})
      note = response.note || note
    }
    let eligible = 0, ineligible = 0, banned = 0, failed = 0, badToken = 0
    let noAt = 0, notFound = 0
    for (const [email, info] of Object.entries(merged)) {
      const row = rows.value.find((item) => item.email === email)
      if (row) row.team_promo_check = info
      if (info.status === 'team_eligible') eligible++
      else if (info.status === 'team_ineligible') ineligible++
      else if (info.status === 'banned') banned++
      else if (info.status === 'token_invalid') badToken++
      else if (info.status === 'no_at') noAt++
      else if (info.status === 'not_found') notFound++
      else if (info.status === 'error') failed++
    }
    const parts = [`本次 Team 检测: ${eligible} 可领, ${ineligible} 不符合`]
    if (banned) parts.push(`${banned} 个封号`)
    if (badToken) parts.push(`${badToken} 个凭证失效`)
    if (noAt) parts.push(`${noAt} 个无AT，已跳过`)
    if (notFound) parts.push(`${notFound} 个记录已不存在`)
    if (failed) parts.push(`${failed} 个没检测成`)
    if (note) parts.push(note)
    teamResult.value = parts.join(' · ')
    await load()
  } catch (error) {
    teamResult.value = ''
    ElMessage.error('Team 优惠检测失败: ' + error.message)
  } finally {
    teamChecking.value = false
  }
}

async function refreshSelectedAT() {
  const emails = selected.value.map((row) => row.email)
  if (!emails.length) return
  refreshingAT.value = true
  try {
    const response = await refreshAccessTokens(emails, '')
    ElMessage.success(
      `AT 刷新完成：成功 ${response.success} 个，失败 ${response.failed} 个`,
    )
    await load()
  } catch (error) {
    ElMessage.error('刷新 AT 失败: ' + error.message)
  } finally {
    refreshingAT.value = false
  }
}

// customClass 里的 pre-line 让消息里的 \n 真的换行。
// 不用 dangerouslyUseHTMLString：消息里会拼邮箱、文件名这些数据，走 HTML 等于开 XSS 口子。
async function confirm(msg) {
  try {
    await ElMessageBox.confirm(msg, '确认', {
      type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消',
      customClass: 'confirm-multiline',
    })
    return true
  }
  catch (_) { return false }
}
async function deleteOne(email) {
  if (!(await confirm(`删除 ${email} 的凭证？`))) return
  try { await deleteRegistered(email); ElMessage.success('已删除'); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`确定删除选中的 ${emails.length} 条凭证？(不可恢复)`))) return
  try { const r = await bulkDeleteRegistered({ emails }); ElMessage.success(`已删除 ${r.deleted} 条`); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteAll() {
  const scopeName = workflowFilter.value === 'integration'
    ? '一体化流程'
    : workflowFilter.value === 'legacy' ? '原有流程' : '全部流程'
  if (!(await confirm(`这会清空“${scopeName}”注册结果里的所有凭证！邮箱列表不受影响，确定？`))) return
  if (!(await confirm('再次确认：真的要删除全部凭证吗？此操作不可恢复！'))) return
  try {
    const r = await bulkDeleteRegistered({
      all: true,
      workflow_scope: workflowFilter.value,
    })
    ElMessage.success(`已清空 ${r.deleted} 条`)
    load()
  }
  catch (e) { ElMessage.error(e.message) }
}

// ──────────── 批量导出 ────────────
// 格式清单来自后端 export_formats.py，下拉菜单是 v-for 出来的：
// 以后加格式只改后端那一个文件，这里一行都不用动。
const exportFormats = ref([])
const exporting = ref(false)
const exportVisible = ref(false)
const exportText = ref('')
const exportCount = ref(0)
const exportFilename = ref('')
const exportLabel = ref('')
// 这一批导出的到底是哪些号 —— 「下载并删除」照着它删，来自后端 r.emails。
// 为什么要后端给、为什么在导出那一刻就存下来：
//   · 「导出全部」是跨页的，前端手里只有当前页 20 行，自己凑必漏；
//   · 弹窗开着的时候主人可能改勾选、翻页，后台自动跑号还会插进新号进来，
//     那时再去读 selected/表格，删的就不是刚下载的那批了。
const exportedEmails = ref([])
const deletingExported = ref(false)

const exportBtnText = computed(() =>
  selected.value.length ? `导出选中 (${selected.value.length})` : '导出全部',
)

async function loadExportFormats() {
  if (exportFormats.value.length) return
  try {
    const { formats } = await listExportFormats()
    exportFormats.value = formats || []
  } catch (e) { ElMessage.error('加载导出格式失败: ' + e.message) }
}

async function doExport(fmt) {
  const emails = selected.value.map((r) => r.email)
  // 没勾选 = 导出全部（跨页，不只当前页）
  const payload = emails.length
    ? { format: fmt.id, emails }
    : { format: fmt.id, all: true, workflow_scope: workflowFilter.value }
  exporting.value = true
  try {
    const r = await exportRegistered(payload)
    exportedEmails.value = (r.emails || []).filter(Boolean)
    // download 模式（CPA zip / SUB2API json）：不弹预览，直接落盘
    if (r.mode === 'download') {
      saveBlob(b64ToBytes(r.b64), r.filename, r.mime)
      ElMessage.success(`已下载 ${r.filename}（${r.count} 个号）`)
      return
    }
    exportText.value = r.text || ''
    exportCount.value = r.count || 0
    exportFilename.value = r.filename || 'export.txt'
    exportLabel.value = r.label || fmt.label
    exportVisible.value = true
  } catch (e) { ElMessage.error('导出失败: ' + e.message) }
  finally { exporting.value = false }
}

function b64ToBytes(b64) {
  const bin = atob(b64 || '')
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}

function saveBlob(data, filename, mime) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function downloadExport() {
  saveBlob(exportText.value, exportFilename.value, 'text/plain;charset=utf-8')
}

// ──────────── 下载并删除 ────────────
// 主人的原话：「不然分不清楚越堆越多」。导出的 txt 里邮箱/密码/2FA/取件url 都齐了，
// 这两张表就没有留存价值了，一起清掉。
//
// ⚠️ 顺序**必须**是「先下载、再确认、最后删」：
//    删库是不可恢复的，而浏览器下载可能被拦（弹窗拦截 / 用户点了取消 / 磁盘满）。
//    先把文件落盘再问，主人是在**手里已经有 txt** 的前提下点的确认。
//    确认框里再报一遍将要删的两张表各多少条，删完之前还有最后一次反悔机会。
async function downloadAndDelete() {
  downloadExport()

  const emails = exportedEmails.value
  if (!emails.length) {
    ElMessage.warning('这批导出没有拿到 email 列表，只下载不删除')
    return
  }

  const ok = await confirm(
    `已下载 ${exportFilename.value}。\n\n` +
    `现在删除这 ${emails.length} 个号：\n` +
    `  · 注册结果（凭证、2FA secret）\n` +
    `  · 邮箱列表（号池那一行，含取件链接）\n\n` +
    `删掉后只剩刚下载的 txt 这一份，不可恢复。确定？`,
  )
  if (!ok) return

  deletingExported.value = true
  try {
    // 两张表分别删。先删注册结果：它是主人真正在看的那张表，
    // 万一号池那边报错（比如这批号根本不是号池导入的、压根没有对应行），
    // 至少结果表已经清干净了，不会出现"删了一半还看得见"。
    const r1 = await bulkDeleteRegistered({ emails })
    let poolDeleted = 0
    try {
      const r2 = await bulkDeleteAccounts({ emails })
      poolDeleted = r2.deleted || 0
    } catch (e) {
      // 号池删失败不算整体失败：凭证已经清掉了，主人该知道的是号池还剩着
      ElMessage.warning('注册结果已删，但邮箱列表删除失败: ' + e.message)
    }
    ElMessage.success(`已删除：注册结果 ${r1.deleted} 条 / 邮箱列表 ${poolDeleted} 条`)
    exportVisible.value = false
    exportedEmails.value = []
    selected.value = []
    load(true)          // 回第一页：这一批没了，停在旧页码多半是空页
    runtime.bumpData()  // 通知「邮箱列表」那一页也刷新，否则主人切过去还看得到已删的号
  } catch (e) {
    ElMessage.error('删除失败: ' + e.message)
  } finally {
    deletingExported.value = false
  }
}

// 凭证弹窗
const credVisible = ref(false)
const credEmail = ref('')
const credData = ref(null)
// totp_secret 放最前：它是唯一「服务端取不回」的字段，弹窗一打开就要能看到
const CRED_KEYS = ['totp_secret', 'totp_factor_id', 'access_token', 'session_token', 'refresh_token', 'id_token', 'device_id', 'csrf_token', 'cookie_header', 'password']
const credRows = computed(() => {
  if (!credData.value) return []
  return CRED_KEYS.filter((k) => credData.value[k]).map((k) => ({ key: k, val: credData.value[k] }))
})
async function viewCred(email) {
  try {
    const { data } = await getRegistered(email)
    credData.value = data
    credEmail.value = email
    credVisible.value = true
  } catch (e) { ElMessage.error('加载凭证失败: ' + e.message) }
}
async function copyCell(email, field) {
  try {
    const { data } = await getRegistered(email)
    const val = data[field] || ''
    if (!val) { ElMessage.warning(`${field} 为空`); return }
    await copyText(val)
  } catch (e) { ElMessage.error('加载凭证失败: ' + e.message) }
}
function copyAllJson() {
  if (credData.value) copyText(JSON.stringify(credData.value, null, 2))
}

// ── 手动编辑凭证 ──
// 只改本地库，不同步 OpenAI。改完的值会被登录流程直接用上
// （registrar 的 account_callback 走 db.get_registered，不区分数据来源）。
const editVisible = ref(false)
const editSaving = ref(false)
const editEmail = ref('')
const editPassword = ref('')
const editSecret = ref('')
// 打开弹窗时的原值，用来判断哪些字段真被改过（没改的不传，后端就不碰）
const editOrigPassword = ref('')
const editOrigSecret = ref('')

function openEdit(row) {
  editEmail.value = row.email
  editPassword.value = row.password || ''
  editSecret.value = row.totp_secret || ''
  editOrigPassword.value = row.password || ''
  editOrigSecret.value = row.totp_secret || ''
  editVisible.value = true
}

async function saveEdit() {
  const pw = editPassword.value
  const sec = editSecret.value.trim()
  const payload = { email: editEmail.value }
  // 只把真正改动过的字段传给后端 —— 没动的字段不传，后端就不会碰它
  if (pw !== editOrigPassword.value) payload.password = pw
  if (sec !== editOrigSecret.value) payload.totp_secret = sec
  if (payload.password === undefined && payload.totp_secret === undefined) {
    ElMessage.info('没有改动')
    editVisible.value = false
    return
  }
  // secret 是唯一「服务端取不回」的凭证：覆盖掉原值 = 该号 2FA 永久锁死。
  // 只在「原本就有 secret」且「确实要改」时拦一道，新填不打扰。
  if (payload.totp_secret !== undefined && editOrigSecret.value) {
    try {
      await ElMessageBox.confirm(
        `该账号已有 2FA secret：\n${editOrigSecret.value}\n\n` +
        '覆盖后原 secret 将永久丢失，服务端取不回。\n' +
        '若原 secret 仍是账号上生效的那个，覆盖会导致该号 2FA 永远登不上。',
        '确认覆盖 2FA secret？',
        { type: 'warning', confirmButtonText: '确认覆盖', cancelButtonText: '取消' },
      )
    } catch { return }
  }
  editSaving.value = true
  try {
    const r = await updateCredentials(payload)
    ElMessage.success(`已保存：${(r.changed || []).join(' + ') || '无改动'}`)
    editVisible.value = false
    await load()
  } catch (e) {
    // 后端 400 会带具体原因（如「TOTP secret 含非法字符」），原样透出
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally { editSaving.value = false }
}

watch(page, () => load())
watch(dataVersion, () => load())
onActivated(() => {
  load()
  loadPlusSchedule()
  if (scheduleTimer) clearInterval(scheduleTimer)
  scheduleTimer = setInterval(loadPlusSchedule, 5000)
})
onDeactivated(() => {
  if (scheduleTimer) clearInterval(scheduleTimer)
  scheduleTimer = null
})
</script>
<template>
  <div class="page">
    <el-card shadow="never">
      <template #header><span class="section-title" style="margin: 0">注册结果</span></template>

      <div class="plus-schedule-panel">
        <div class="plus-schedule-main">
          <div>
            <b>定时 Plus 检测</b>
            <span class="hint">只读查询账号状态和 Plus 优惠券；默认走服务器直连，不消耗注册代理。</span>
          </div>
          <el-tag :type="plusSchedule.running ? 'warning' : (plusSchedule.last_status === 'failed' ? 'danger' : 'success')">
            {{ plusScheduleStatusText }}
          </el-tag>
          <el-switch v-model="plusSchedule.enabled" @change="savePlusSchedule" />
        </div>
        <el-space wrap :size="12">
          <span>周期</span>
          <el-input-number
            v-model="plusSchedule.interval_hours"
            :min="0.25" :max="168" :step="0.5" :precision="2"
            style="width: 130px" @change="savePlusSchedule"
          />
          <span>小时</span>
          <el-select v-model="plusSchedule.scope" style="width: 140px" @change="savePlusSchedule">
            <el-option label="一体化流程" value="integration" />
            <el-option label="原有流程" value="legacy" />
            <el-option label="全部流程" value="all" />
          </el-select>
          <span>出口</span>
          <el-select v-model="plusSchedule.egress_mode" style="width: 170px" @change="savePlusSchedule">
            <el-option label="服务器直连（省流量）" value="direct" />
            <el-option label="注册代理池" value="proxy" />
          </el-select>
          <span>并发</span>
          <el-input-number
            v-model="plusSchedule.workers"
            :min="1" :max="6"
            style="width: 100px" @change="savePlusSchedule"
          />
          <el-button :loading="scheduleStarting" @click="runPlusScheduleNow">立即检测</el-button>
        </el-space>
        <div class="hint plus-schedule-meta">
          下次 {{ scheduleTime(plusSchedule.next_run_at) }}
          · 上次完成 {{ scheduleTime(plusSchedule.last_finished_at) }}
          <template v-if="plusSchedule.last_counts?.checked">
            · 已查 {{ plusSchedule.last_counts.checked }}，Plus {{ plusSchedule.last_counts.plus || 0 }}，失败 {{ plusSchedule.last_counts.errors || 0 }}
          </template>
          <span v-if="plusSchedule.last_error" class="schedule-error"> · {{ plusSchedule.last_error }}</span>
        </div>
      </div>

      <el-space wrap style="margin-bottom: 12px">
        <el-button @click="load(false)"><el-icon><Refresh /></el-icon>刷新</el-button>
        <el-select v-model="workflowFilter" style="width: 150px" @change="load(true)">
          <el-option label="原有流程" value="legacy" />
          <el-option label="一体化流程" value="integration" />
          <el-option label="全部流程" value="" />
        </el-select>
        <el-select v-model="filter" style="width: 130px" @change="load(true)">
          <el-option label="完整凭证" value="successful" />
          <el-option label="全部" value="all" />
          <el-option label="有 RT" value="has_rt" />
          <el-option label="无 RT" value="no_rt" />
          <el-option label="未检测" value="unchecked" />
          <el-option label="Free" value="free" />
          <el-option label="可领Plus" value="plus" />
          <el-option label="可领Team" value="team_eligible" />
          <el-option label="已封号" value="banned" />
          <el-option label="凭证失效" value="token_invalid" />
        </el-select>
        <el-select
          v-model="form.proxy" filterable clearable allow-create default-first-option
          :reserve-keyword="false" placeholder="检测代理（留空直连）"
          style="width: 260px"
        >
          <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
        </el-select>
        <el-button :loading="checking" @click="doCheck('unchecked')">检查未检测</el-button>
        <el-button :loading="checking" @click="doCheck('all')">重新检查</el-button>
        <el-button :loading="checking" :disabled="!selected.length" @click="doCheck('selected')">
          检测选中 ({{ selected.length }})
        </el-button>
        <el-button :loading="teamChecking" @click="doTeamCheck('unchecked')">检查Team未检测</el-button>
        <el-button :loading="teamChecking" :disabled="!selected.length" @click="doTeamCheck('selected')">
          检测选中Team ({{ selected.length }})
        </el-button>
          <el-button :loading="refreshingAT" :disabled="!selected.length" @click="refreshSelectedAT">
            刷新选中 AT ({{ selected.length }})
          </el-button>
        <el-divider direction="vertical" />
        <el-dropdown trigger="click" @command="doExport" @visible-change="(v) => v && loadExportFormats()">
          <el-button :loading="exporting">
            <el-icon><Download /></el-icon>{{ exportBtnText }}
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="f in exportFormats" :key="f.id" :command="f" :divided="f.mode === 'download' && f.id === 'cpa'">
                {{ f.label }}
                <span v-if="f.note" class="hint" style="margin-left: 6px">{{ f.note }}</span>
              </el-dropdown-item>
              <el-dropdown-item v-if="!exportFormats.length" disabled>加载中...</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-divider direction="vertical" />
        <el-button type="danger" plain :disabled="!selected.length" @click="deleteSelected">
          删除选中 ({{ selected.length }})
        </el-button>
        <el-button type="danger" plain @click="deleteAll">清空全部</el-button>
        <el-tag v-if="currentPlusTotal !== null" type="success" effect="plain">
          当前可领Plus {{ currentPlusTotal }}
        </el-tag>
        <el-tag v-if="currentTeamTotal !== null" type="success" effect="plain">
          当前可领Team {{ currentTeamTotal }}
        </el-tag>
        <span class="hint">{{ checkResult }}</span>
        <span v-if="teamResult" class="hint">{{ teamResult }}</span>
      </el-space>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table
        v-else
        v-loading="loading" :data="rows" size="small" stripe
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column prop="email" label="邮箱" min-width="200" show-overflow-tooltip />
        <el-table-column label="流程" width="120">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.workflow_scope === 'integration' ? 'primary' : 'info'"
            >
              {{ workflowLabel(row.workflow_scope) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="140">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.credential_source === 'recovery' ? 'warning' : 'success'"
              effect="plain"
            >
              {{ credentialSourceLabel(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <!-- 密码直接明文列出：随机 16 位，是登录账号的必需品，
             藏进「查看凭证」弹窗每次都要多点两下。列表接口本来就在返回它。
             图标放在文字**后面**：放前面会把值整体右推 27px（见 .cell-copy 注释）。 -->
        <el-table-column label="密码" min-width="170">
          <template #default="{ row }">
            <el-button
              v-if="row.password" size="small" text type="primary"
              class="cell-copy mono" @click="copyText(row.password)"
            >
              {{ row.password }}<el-icon class="ico"><CopyDocument /></el-icon>
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <!-- 2FA secret 同样明文列出：它是唯一「服务端取不回」的凭证，
             丢了这个号就永久锁死，必须一眼看见、一点就能复制。
             min-width 必须装得下 32 位 base32：.cell 带 overflow:hidden，
             宽度不够会**无声截断**，肉眼核对时看到的是残缺值。实测需 ~250px。 -->
        <el-table-column label="2FA" min-width="260">
          <template #default="{ row }">
            <el-button
              v-if="row.totp_secret" size="small" text type="warning"
              class="cell-copy mono" @click="copyText(row.totp_secret)"
            >
              {{ row.totp_secret }}<el-icon class="ico"><CopyDocument /></el-icon>
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="Plus状态" width="120">
          <template #default="{ row }">
            <StatusDot v-if="plusOf(row)" :type="PLUS_TYPE[plusOf(row).status] || 'info'" :text="plusOf(row).label" />
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="Team优惠" width="130">
          <template #default="{ row }">
            <el-tooltip v-if="teamOf(row) && teamReason(row)" :content="teamReason(row)" placement="top">
              <span><StatusDot :type="TEAM_TYPE[teamOf(row).status] || 'info'" :text="teamOf(row).label" /></span>
            </el-tooltip>
            <StatusDot v-else-if="teamOf(row)" :type="TEAM_TYPE[teamOf(row).status] || 'info'" :text="teamOf(row).label" />
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="access" width="100" align="center">
          <template #default="{ row }">
            <el-button v-if="row.at_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'access_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.at_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="session" width="100" align="center">
          <template #default="{ row }">
            <el-button v-if="row.st_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'session_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.st_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="refresh" width="100" align="center">
          <template #default="{ row }">
            <el-button v-if="row.rt_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'refresh_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.rt_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text @click="viewCred(row.email)">查看凭证</el-button>
            <el-button size="small" text type="warning" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" text type="danger" @click="deleteOne(row.email)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无注册结果，去「单次注册」或「全自动批量」跑号" :image-size="70" />
        </template>
      </el-table>
      <div style="display: flex; justify-content: center; margin-top: 14px">
        <el-pagination
          v-model:current-page="page" :page-size="PAGE_SIZE" :total="total"
          layout="prev, pager, next, total" background
        />
      </div>

      <el-dialog v-model="exportVisible" width="720px" top="8vh">
        <template #header>
          <div style="display: flex; align-items: center; gap: 12px">
            <span style="font-weight: 600">导出 · {{ exportLabel }}</span>
            <el-tag size="small" type="info">共 {{ exportCount }} 行</el-tag>
          </div>
        </template>
        <el-input
          :model-value="exportText" type="textarea" :rows="14" readonly
          class="mono export-area"
        />
        <template #footer>
          <el-button @click="copyText(exportText)">
            <el-icon><CopyDocument /></el-icon>复制全部
          </el-button>
          <el-button type="primary" @click="downloadExport">
            <el-icon><Download /></el-icon>下载 {{ exportFilename }}
          </el-button>
          <!-- 危险动作放最右、danger 色，和左边的纯下载拉开距离，避免手滑。
               先下载文件、再弹二次确认，确认框里会报清楚要删哪两张表各多少条。 -->
          <el-button
            type="danger" plain
            :loading="deletingExported"
            :disabled="!exportedEmails.length"
            @click="downloadAndDelete"
          >
            <el-icon><Delete /></el-icon>下载并删除这 {{ exportedEmails.length }} 个号
          </el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="credVisible" :title="credEmail" width="760px" top="6vh">
        <template #header>
          <div style="display: flex; align-items: center; gap: 12px">
            <span class="mono" style="font-weight: 600">{{ credEmail }}</span>
            <el-button size="small" @click="copyAllJson">复制全部 JSON</el-button>
          </div>
        </template>
        <div v-for="r in credRows" :key="r.key" style="margin-bottom: 12px">
          <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 4px">
            <span class="mono" style="font-weight: 600; color: var(--dango-pink-dark)">{{ r.key }}</span>
            <el-tag size="small" type="info">len={{ r.val.length }}</el-tag>
            <el-button size="small" @click="copyText(r.val)">复制</el-button>
          </div>
          <el-input :model-value="r.val" type="textarea" :rows="2" readonly class="mono" />
        </div>
        <el-empty v-if="!credRows.length" description="无凭证字段" />
      </el-dialog>

      <!-- 手动编辑凭证：把外部已知的密码/2FA 补进来，或修正记录错误 -->
      <el-dialog v-model="editVisible" title="编辑凭证" width="560px" top="10vh">
        <el-alert
          type="warning" :closable="false" show-icon style="margin-bottom: 16px"
          title="仅修改本地记录，不会同步到 OpenAI"
          description="这里改密码不等于改了账号密码。填入的值会被登录流程直接使用。"
        />
        <el-form label-position="top">
          <el-form-item label="邮箱">
            <el-input :model-value="editEmail" class="mono" disabled />
          </el-form-item>
          <el-form-item label="密码">
            <el-input v-model="editPassword" class="mono" placeholder="留空表示该号无密码" />
          </el-form-item>
          <el-form-item label="2FA Secret">
            <el-input
              v-model="editSecret" class="mono"
              placeholder="base32，支持带空格/小写/otpauth:// 链接，会自动规范化"
            />
            <div class="hint" style="margin-top: 6px; line-height: 1.6">
              服务端取不回此值，覆盖后原 secret 永久丢失。清空则该号按无 2FA 处理。
            </div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="editVisible = false">取消</el-button>
          <el-button type="primary" :loading="editSaving" @click="saveEdit">保存</el-button>
        </template>
      </el-dialog>
    </el-card>
  </div>
</template>

<style scoped>
/* 表格里「点一下就复制」的明文单元格（密码 / 2FA secret）。
   :deep 是必需的：.el-button 由 Element Plus 渲染，scoped 的属性选择器打不到它。

   为什么要重置 padding —— Element Plus 有两个长得很像的类：
     .el-button--text  （旧版 type="text"）  padding 左右为 0
     .el-button.is-text（新版 text 属性）    继承 --small 的 5px 11px
   我们用的是后者，于是 11px padding + 12px 图标 + 4px 间隙 = 值被整体右推 27px，
   同列的表头和空值「—」都贴着 cell 左沿，一眼就看出错位。 */
:deep(.el-button.cell-copy.el-button--small) {
  padding: 0 6px 0 0;
  height: 20px;
  font-size: 12px;
}
/* 图标默认透明但**保留占位**：用 opacity 而不是 display:none，
   否则 hover 时图标撑开宽度会把文字挤得左右抖。 */
:deep(.cell-copy .ico) {
  margin-left: 5px;
  opacity: 0;
  transition: opacity 0.12s;
}
:deep(.cell-copy:hover .ico) { opacity: 0.65; }
.plus-schedule-panel {
  margin-bottom: 14px;
  padding: 13px 15px;
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 10px;
  background: var(--el-color-primary-light-9);
}
.plus-schedule-main {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.plus-schedule-main > div {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 3px;
}
.plus-schedule-meta { margin-top: 9px; }
.schedule-error { color: var(--el-color-danger); }
</style>

<!-- 非 scoped：ElMessageBox 是挂到 body 上的，不在本组件的 scope 属性范围内，
     scoped 样式打不到它。只作用在自家 customClass 上，不会污染别处的确认框。 -->
<style>
.confirm-multiline .el-message-box__message { white-space: pre-line; }
</style>

import http from './request'

// ──────────────── 单个注册 ────────────────
export const startRegister = (payload) => http.post('/api/register', payload)
export const startRecovery = (payload) => http.post('/api/recovery/start', payload)
export const startRecoveryBatch = (payload) =>
  http.post('/api/recovery/batch/start', payload)
export const getRecoveryBatch = (batchId) =>
  http.get(`/api/recovery/batch/status/${encodeURIComponent(batchId)}`)
export const getLatestRecoveryBatch = () =>
  http.get('/api/recovery/batch/latest')
export const scanICloudRecovery = (payload) =>
  http.post('/api/recovery/scan_icloud', payload)

// ──────────────── 运行记录 ────────────────
export const listRuns = (limit = 50) => http.get('/api/runs', { params: { limit } })

// ──────────────── 注册结果 registered ────────────────
export const listRegistered = (params) =>
  http.get('/api/registered', { params }) // { limit, offset, filter }

export const listUncheckedEmails = (workflowScope = '') =>
  http.get('/api/registered/unchecked_emails', {
    params: { workflow_scope: workflowScope },
  })

export const listRetestEmails = (workflowScope = '') =>
  http.get('/api/registered/retest_emails', {
    params: { workflow_scope: workflowScope },
  })

export const getRegistered = (email) =>
  http.get(`/api/registered/${encodeURIComponent(email)}`)

export const deleteRegistered = (email) =>
  http.delete(`/api/registered/${encodeURIComponent(email)}`)

// 手填凭证：不传的字段后端不动，传空串才是清空
export const updateCredentials = (payload) =>
  http.post('/api/registered/update_credentials', payload)

export const bulkDeleteRegistered = (payload) =>
  http.post('/api/registered/bulk_delete', payload) // { emails } 或 { all: true }

// 导出后清理用：把号池那一行也删掉。
// 从 accounts.js 转出来一份，省得 Registered.vue 同时 import 两个 api 模块。
export { bulkDeleteAccounts } from './accounts'

// 批量导出：格式清单由后端 export_formats.py 提供，加格式前端不用改
export const listExportFormats = () => http.get('/api/registered/export/formats')
export const exportRegistered = (payload) => http.post('/api/registered/export', payload)

export const checkPlus = (emails, proxy = '') =>
  http.post('/api/registered/check_plus_fast', { emails, proxy })

export const refreshAccessTokens = (emails, proxy = '') =>
  http.post('/api/registered/refresh_at', { emails, proxy })

export const exportToPanel = (email, targets) =>
  http.post('/api/registered/export_to_panel', { email, targets })

// ──────────────── 自动跑号 auto-loop ────────────────
export const autoStart = (payload) => http.post('/api/auto/start', payload)
export const autoPause = () => http.post('/api/auto/pause')
export const autoResume = () => http.post('/api/auto/resume')
export const autoStop = () => http.post('/api/auto/stop')
export const autoStatus = () => http.get('/api/auto/status')

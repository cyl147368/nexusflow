import http from './request'

export const checkUpdate = () => http.get('/api/system/update/check', { timeout: 180000 })
export const applyUpdate = () => http.post('/api/system/update/apply', {}, { timeout: 30000 })
export const getUpdateStatus = () => http.get('/api/system/update/status')

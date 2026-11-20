import http from './request'

export const getPaymentHealth = () => http.get('/api/payment/health')
export const listPaymentCandidates = () => http.get('/api/payment/candidates')
export const listPaymentResults = () => http.get('/api/payment/results')
export const startPaymentCheck = (payload) => http.post('/api/payment/check', payload)
export const getPaymentRun = (runId) => http.get(`/api/payment/runs/${encodeURIComponent(runId)}`)

import http from './request'

export const getIntegrationConfig = () => http.get('/api/integration/config')
export const saveIntegrationConfig = (payload) => http.post('/api/integration/config', payload)
export const testMailHub = () => http.post('/api/integration/test/mailhub')
export const getMailHubInfo = () => http.get('/api/integration/mailhub/info')
export const testICloud = () => http.post('/api/integration/test/icloud')
export const listICloudAccounts = () => http.get('/api/integration/icloud/accounts')
export const createICloudAccount = (payload) => http.post('/api/integration/icloud/account', payload)
export const normalizeCookie = (text) => http.post('/api/integration/cookies/normalize', { text })
export const listICloudAliases = (accountId = '') =>
  http.get('/api/integration/icloud/aliases', { params: { account_id: accountId } })
export const deleteICloudAlias = (payload) =>
  http.post('/api/integration/icloud/aliases/delete', payload)
export const setICloudAliasState = (payload) =>
  http.post('/api/integration/icloud/aliases/state', payload)
export const startIntegration = () => http.post('/api/integration/start')
export const getIntegrationStatus = () => http.get('/api/integration/status')

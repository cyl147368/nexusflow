<script setup>
import { computed, onActivated, onDeactivated, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { applyUpdate, checkUpdate, getUpdateStatus } from '@/api/system'
import { fmtTime } from '@/api/request'

const loading = ref(false)
const applying = ref(false)
const info = ref({})
const state = ref({ status: 'idle' })
let timer = null

const statusType = computed(() => ({
  completed: 'success',
  running: 'warning',
  failed: 'danger',
}[state.value.status] || 'info'))

async function check() {
  loading.value = true
  try {
    info.value = await checkUpdate()
    if (info.value.behind) ElMessage.warning('GitHub 上有新版本')
    else ElMessage.success('当前已是最新版本')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function loadStatus() {
  try {
    const response = await getUpdateStatus()
    state.value = response.state || state.value
    if (state.value.status !== 'running') stopPolling()
  } catch (_) {
    // 更新容器重建互联网时短暂请求失败是正常现象。
  }
}

function startPolling() {
  stopPolling()
  timer = setInterval(loadStatus, 2000)
}

function stopPolling() {
  if (timer) clearInterval(timer)
  timer = null
}

async function update() {
  if (!info.value.behind) return
  try {
    await ElMessageBox.confirm(
      `将更新到 GitHub origin/main（${info.value.remote?.slice(0, 12)}），期间本项目容器会重启。`,
      '确认更新',
      { type: 'warning', confirmButtonText: '立即更新', cancelButtonText: '取消' },
    )
  } catch (_) {
    return
  }
  applying.value = true
  try {
    const response = await applyUpdate()
    state.value = response.state || { status: 'running' }
    ElMessage.success(response.message || '更新任务已启动')
    startPolling()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    applying.value = false
  }
}

onActivated(() => {
  check()
  loadStatus()
})
onDeactivated(stopPolling)
check()
loadStatus()
</script>

<template>
  <div class="page update-page" v-loading="loading">
    <el-card shadow="never" class="update-card">
      <template #header>
        <div class="head">
          <div>
            <h2>GitHub 自动更新</h2>
            <p>从 cyl147368/nexusflow 的 main 分支检查并部署最新版本。</p>
          </div>
          <el-tag :type="statusType">{{ state.status || 'idle' }}</el-tag>
        </div>
      </template>

      <div class="commits">
        <div>
          <span>当前版本</span>
          <code>{{ info.current?.slice(0, 12) || '读取中' }}</code>
        </div>
        <el-icon><Right /></el-icon>
        <div>
          <span>GitHub 最新</span>
          <code>{{ info.remote?.slice(0, 12) || '读取中' }}</code>
        </div>
      </div>

      <el-alert
        :type="info.behind ? 'warning' : 'success'"
        :closable="false" show-icon
        :title="info.behind ? `发现 ${info.changed_files?.length || 0} 个文件更新` : '当前代码已是最新'"
      />

      <el-collapse v-if="info.changed_files?.length" class="files">
        <el-collapse-item title="查看变更文件" name="files">
          <div v-for="file in info.changed_files" :key="file" class="file-row">{{ file }}</div>
        </el-collapse-item>
      </el-collapse>

      <div class="state-box">
        <span>更新进度</span>
        <b>{{ state.stage || 'idle' }}</b>
        <p>{{ state.message || '尚未执行更新' }}</p>
        <time v-if="state.updated_at">{{ fmtTime(state.updated_at) }}</time>
      </div>

      <div class="actions">
        <el-button :loading="loading" @click="check">重新检查</el-button>
        <el-button
          type="primary" :loading="applying" :disabled="!info.behind || state.status === 'running'"
          @click="update"
        >
          从 GitHub 更新并重启
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.update-page { max-width: 900px; margin: 0 auto; }
.update-card { border: 1px solid var(--el-color-primary-light-8); }
.head { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
.head h2 { margin: 0 0 6px; }
.head p { margin: 0; color: var(--el-text-color-secondary); }
.commits {
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center;
  gap: 20px; margin: 22px 0;
}
.commits div {
  padding: 16px; border-radius: 10px; background: var(--el-fill-color-lighter);
  display: flex; flex-direction: column; gap: 8px;
}
.commits span { color: var(--el-text-color-secondary); font-size: 12px; }
.commits code { font-size: 16px; color: var(--el-color-primary); }
.files { margin-top: 12px; }
.file-row { padding: 5px 0; font-family: ui-monospace, monospace; font-size: 12px; }
.state-box {
  margin: 18px 0; padding: 16px; border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px; background: var(--el-fill-color-lighter);
}
.state-box span { color: var(--el-text-color-secondary); font-size: 12px; }
.state-box b { margin-left: 10px; }
.state-box p { margin: 8px 0 4px; }
.state-box time { color: var(--el-text-color-secondary); font-size: 12px; }
.actions { display: flex; justify-content: flex-end; gap: 10px; }
@media (max-width: 700px) {
  .commits { grid-template-columns: 1fr; }
  .commits .el-icon { display: none; }
}
</style>

const bridge = window.AstrBotPluginPage
await bridge.ready()

const state = { query: '', status: 'active', page: 1, pageSize: 24, pages: 1, loading: false }
const list = document.querySelector('#entry-list')
const notice = document.querySelector('#notice')
const searchInput = document.querySelector('#search-input')
const syncButton = document.querySelector('#sync-button')
const refreshButton = document.querySelector('#refresh-button')
const confirmDialog = document.querySelector('#confirm-dialog')
const confirmSubmit = document.querySelector('#confirm-submit')
let confirmationResolver = null

const text = (tag, className, value) => {
  const node = document.createElement(tag)
  if (className) node.className = className
  node.textContent = value ?? ''
  return node
}

const setNotice = (message, error = false) => {
  notice.textContent = message
  notice.classList.toggle('error', error)
}

const setBusy = (busy) => {
  state.loading = busy
  syncButton.disabled = busy
  refreshButton.disabled = busy
}

const closeConfirmation = (confirmed) => {
  confirmDialog.hidden = true
  const resolve = confirmationResolver
  confirmationResolver = null
  if (resolve) resolve(confirmed)
}

const requestConfirmation = (entry) => {
  const restoring = entry.deleted
  document.querySelector('#confirm-title').textContent = restoring ? '恢复这条知识？' : '删除这条知识？'
  document.querySelector('#confirm-message').textContent = restoring
    ? '恢复后，这条内容会重新进入 AstrBot 内容检索。'
    : '删除后，这条内容会立即从 AstrBot 内容检索中移除。'
  document.querySelector('#confirm-info-id').textContent = `InfoID · ${entry.info_id}`
  document.querySelector('#confirm-hint').textContent = restoring
    ? '系统会重新生成插件管理的知识文档。'
    : 'GitHub 原始资料不会被永久删除，之后可以在回收站恢复。'
  confirmSubmit.textContent = restoring ? '确认恢复' : '确认删除'
  confirmSubmit.className = `button ${restoring ? 'primary' : 'danger'}`
  confirmDialog.hidden = false
  confirmSubmit.focus()
  return new Promise((resolve) => { confirmationResolver = resolve })
}

document.querySelector('#confirm-cancel').addEventListener('click', () => closeConfirmation(false))
confirmSubmit.addEventListener('click', () => closeConfirmation(true))
confirmDialog.addEventListener('click', (event) => {
  if (event.target === confirmDialog) closeConfirmation(false)
})
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !confirmDialog.hidden) closeConfirmation(false)
})

const renderStats = (stats) => {
  document.querySelector('#source-count').textContent = String(stats.source)
  document.querySelector('#active-count').textContent = String(stats.active)
  document.querySelector('#deleted-count').textContent = String(stats.deleted)
  document.querySelector('#source-sha').textContent = String(stats.sha || '—').slice(0, 12)
}

const createEntry = (entry) => {
  const article = text('article', `entry${entry.deleted ? ' deleted' : ''}`)
  const head = text('div', 'entry-head')
  const identity = text('div')
  identity.append(text('span', 'entry-id', `InfoID · ${entry.info_id}`), text('h2', '', entry.title))

  const actions = text('div', 'entry-actions')
  const edit = text('a', 'button ghost edit-link', '编辑')
  edit.href = entry.edit_url
  edit.target = '_blank'
  edit.rel = 'noreferrer'

  const action = text('button', `button ${entry.deleted ? 'primary' : 'danger'}`, entry.deleted ? '恢复' : '删除')
  action.type = 'button'
  action.addEventListener('click', async () => {
    const verb = entry.deleted ? '恢复' : '删除'
    if (!await requestConfirmation(entry)) return
    action.disabled = true
    setNotice(`正在${verb} ${entry.info_id} 并重建 AstrBot 检索内容…`)
    try {
      await bridge.apiPost(`entries/${encodeURIComponent(entry.info_id)}/${entry.deleted ? 'restore' : 'delete'}`, {
        confirm_info_id: entry.info_id
      })
      setNotice(`${verb}完成：${entry.info_id}`)
      await loadEntries(true)
    } catch (error) {
      setNotice(`${verb}失败：${error.message}`, true)
      action.disabled = false
    }
  })
  actions.append(edit, action)
  head.append(identity, actions)

  const body = text('p', 'entry-text', entry.text)
  const meta = text('div', 'meta')
  meta.append(
    text('span', '', `可信度 · ${entry.confidence || '未标注'}`),
    text('span', '', `来源类型 · ${entry.source_type || '未标注'}`),
    text('span', '', `上传者 · ${entry.uploaded_by || '未知'}`),
    text('span', '', `核验日期 · ${entry.verified_at || '未标注'}`)
  )
  if (entry.source_url) {
    const source = text('a', '', '查看来源 ↗')
    source.href = entry.source_url
    source.target = '_blank'
    source.rel = 'noreferrer'
    meta.append(source)
  }
  const tags = text('div', 'tags')
  ;(entry.tags || []).forEach((tag) => tags.append(text('span', '', tag)))
  article.append(head, body, meta, tags)
  if (entry.deleted) {
    article.append(text('p', 'deleted-note', `已由 ${entry.deleted_by || '管理员'} 于 ${entry.deleted_at || '未知时间'} 移入回收站`))
  }
  return article
}

const renderPager = (pagination) => {
  state.pages = pagination.pages
  const pager = document.querySelector('#pager')
  pager.hidden = pagination.total === 0
  document.querySelector('#page-label').textContent = `第 ${pagination.page} / ${pagination.pages} 页，共 ${pagination.total} 条`
  document.querySelector('#previous-page').disabled = pagination.page <= 1
  document.querySelector('#next-page').disabled = pagination.page >= pagination.pages
}

async function loadEntries (refresh = false) {
  if (state.loading) return
  setBusy(true)
  setNotice(refresh ? '正在刷新 GitHub 源数据并读取条目…' : '正在读取知识条目…')
  try {
    const result = await bridge.apiGet('entries', {
      q: state.query,
      status: state.status,
      page: state.page,
      page_size: state.pageSize,
      refresh: refresh ? 1 : 0
    })
    renderStats(result.stats)
    list.replaceChildren(...result.entries.map(createEntry))
    if (!result.entries.length) list.append(text('div', 'empty', '没有符合当前条件的知识条目。'))
    renderPager(result.pagination)
    setNotice(`当前显示 ${result.entries.length} 条；删除操作会同步更新配置中的 AstrBot 目标知识库。`)
  } catch (error) {
    setNotice(`读取失败：${error.message}`, true)
  } finally {
    setBusy(false)
  }
}

let searchTimer
searchInput.addEventListener('input', () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    state.query = searchInput.value.trim()
    state.page = 1
    loadEntries()
  }, 250)
})

document.querySelectorAll('[data-status]').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('[data-status]').forEach((item) => item.classList.toggle('active', item === button))
    state.status = button.dataset.status
    state.page = 1
    loadEntries()
  })
})

document.querySelector('#previous-page').addEventListener('click', () => {
  if (state.page > 1) { state.page -= 1; loadEntries() }
})
document.querySelector('#next-page').addEventListener('click', () => {
  if (state.page < state.pages) { state.page += 1; loadEntries() }
})
refreshButton.addEventListener('click', () => loadEntries(true))
syncButton.addEventListener('click', async () => {
  setBusy(true)
  setNotice('正在同步 AstrBot 知识库…')
  try {
    const result = await bridge.apiPost('sync', {})
    setNotice(`同步完成：启用 ${result.entry_count} 条，更新 ${result.updated_targets} 个目标知识库。`)
    setBusy(false)
    await loadEntries(true)
  } catch (error) {
    setNotice(`同步失败：${error.message}`, true)
    setBusy(false)
  }
})

bridge.onContext(() => {
  document.documentElement.lang = bridge.getLocale() || 'zh-CN'
})
await loadEntries(true)

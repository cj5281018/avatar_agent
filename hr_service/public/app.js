const statusLabels = {
  pending: '待审批',
  approved: '已通过',
  rejected: '已拒绝',
  cancelled: '已撤销',
};

const workStatusLabels = {
  working: '在岗',
  on_leave: '休假中',
  inactive: '已停用',
};

const leaveTypeLabels = {
  annual: '年假',
  sick: '病假',
  personal: '事假',
  compensatory: '调休',
  other: '其他',
};

const byId = (id) => document.getElementById(id);
const state = { csrf: '', data: null, editingHandle: null, deletingId: null, loadVersion: 0 };

async function bootstrap() {
  try {
    const session = await api('/api/admin/session');
    state.csrf = session.csrf;
    showApp();
    await load();
  } catch {
    showLogin();
  }
}

async function load() {
  if (state.deletingId) return;
  const version = ++state.loadVersion;
  byId('refresh').disabled = true;
  try {
    const data = await api('/api/admin/dashboard');
    if (version !== state.loadVersion) return;
    state.data = data;
    render(data);
  } catch (error) {
    if (version !== state.loadVersion) return;
    renderIntegration({ connectionStatus: 'unknown' });
    handleError(error);
  } finally {
    if (version === state.loadVersion) byId('refresh').disabled = false;
  }
}

async function api(url, options = {}) {
  const headers = { accept: 'application/json', ...(options.headers || {}) };
  if (options.body !== undefined) headers['content-type'] = 'application/json';
  if (options.method && options.method !== 'GET' && state.csrf) {
    headers['x-hrdaily-csrf'] = state.csrf;
  }
  const response = await fetch('.' + url, {
    ...options,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    const error = new Error(data.message || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function renderIntegration(integration = {}) {
  const status = integration.connectionStatus || 'unknown';
  const labels = {
    unpaired: '等待 Avatar 配对',
    connected: 'Avatar 已连接',
    pending: 'Avatar 配对未完成',
    invalid: 'Avatar 授权已失效',
    unreachable: '暂时无法连接 Avatar',
    unknown: 'Avatar 状态未确认',
  };
  const integrationElement = byId('integration-status');
  integrationElement.className = `integration-status ${status === 'connected' ? 'ready' : status === 'invalid' ? 'invalid' : 'waiting'}`;
  const text = labels[status] || labels.unknown;
  integrationElement.textContent = text;
  integrationElement.title = [text, integration.service?.name,
    integration.checkedAt ? `核验于 ${new Date(integration.checkedAt).toLocaleString('zh-CN', { hour12: false })}` : '',
  ].filter(Boolean).join(' · ');
}

function render(data) {
  renderIntegration(data.integration);

  const counts = data.counts || {};
  byId('metric-total').textContent = String(counts.total || 0);
  byId('metric-pending').textContent = String(counts.pending || 0);
  byId('metric-approved').textContent = String(counts.approved || 0);
  byId('metric-rejected').textContent = String(counts.rejected || 0);
  const onLeave = (data.employees || []).filter((item) => item.workStatus === 'on_leave').length;
  byId('overview-line').textContent = `${data.employees?.filter((item) => item.active).length || 0} 位在册成员 · ${onLeave} 人休假 · ${counts.pending || 0} 项等待审批`;
  byId('updated-at').textContent = data.updatedAt
    ? `更新于 ${new Date(data.updatedAt).toLocaleString('zh-CN', { hour12: false })}`
    : '';

  const leaves = Array.isArray(data.leaves) ? data.leaves : [];
  byId('leave-empty').hidden = leaves.length > 0;
  byId('leave-rows').innerHTML = leaves.map(leaveRow).join('');

  const employees = Array.isArray(data.employees) ? data.employees : [];
  byId('employee-grid').innerHTML = employees.map(employeeCard).join('');
}

function leaveRow(leave) {
  const requester = leave.requester || {};
  const disabled = state.deletingId ? ' disabled' : '';
  const approvalText = leave.manuallyDecided
    ? `HR 管理员 · ${statusLabels[leave.status] || leave.status}`
    : (leave.approvals || [])
        .map((item) => `${item.displayName || item.handle} · ${statusLabels[item.status] || item.status}`)
        .join(' / ');
  const actions = leave.status === 'pending'
    ? `<button class="row-action approve" data-leave-id="${escapeHtml(leave.id)}" data-status="approved"${disabled}>通过</button>
       <button class="row-action reject" data-leave-id="${escapeHtml(leave.id)}" data-status="rejected"${disabled}>拒绝</button>
       <button class="row-action" data-leave-id="${escapeHtml(leave.id)}" data-status="cancelled"${disabled}>撤销</button>`
    : `<button class="row-action" data-leave-id="${escapeHtml(leave.id)}" data-status="pending"${disabled}>恢复待审批</button>`;
  const deleteAction = `<button type="button" class="row-action delete" data-delete-leave-id="${escapeHtml(leave.id)}" title="删除请假记录"${disabled}>${state.deletingId === leave.id ? '删除中…' : '删除'}</button>`;
  const decided = leave.manuallyDecided ? ' · HR 手工处理' : '';
  return `<tr>
    <td class="person-cell"><strong>${escapeHtml(requester.displayName || requester.handle || '-')}</strong><span>@${escapeHtml(requester.handle || '-')}</span></td>
    <td>${escapeHtml(leave.startDate || '-')} - ${escapeHtml(leave.endDate || '-')}</td>
    <td class="detail-cell"><strong>${escapeHtml(leaveTypeLabels[leave.leaveType] || leave.leaveType || '-')}</strong><span title="${escapeHtml(leave.reason || '')}">${escapeHtml(leave.reason || '未填写原因')}</span></td>
    <td title="${escapeHtml(approvalText)}">${escapeHtml(approvalText || '-')}</td>
    <td><span class="status-pill status-${escapeHtml(leave.status || 'pending')}">${escapeHtml(statusLabels[leave.status] || leave.status || '-')}</span><small class="manual-label">${escapeHtml(decided)}</small></td>
    <td><div class="row-actions">${actions}${deleteAction}</div></td>
  </tr>`;
}

function employeeCard(item) {
  const approvers = (item.approvers || [])
    .map((approver) => approver.displayName || `@${approver.handle}`)
    .join('、');
  const first = String(item.displayName || item.handle || '?').slice(0, 1);
  const status = workStatusLabels[item.workStatus] || item.workStatus;
  return `<article class="employee ${item.active ? '' : 'employee-inactive'}">
    <span class="employee-avatar">${escapeHtml(first)}</span>
    <span class="employee-copy"><strong>${escapeHtml(item.displayName || item.handle || '-')}</strong><small>@${escapeHtml(item.handle || '-')} · ${escapeHtml(item.jobTitle || '-')} ${escapeHtml(item.rank || '')}</small></span>
    <span class="work-status work-${escapeHtml(item.workStatus || 'working')}">${escapeHtml(status || '-')}</span>
    <button class="icon-button employee-edit" type="button" data-edit-handle="${escapeHtml(item.handle)}" title="编辑人员" aria-label="编辑 ${escapeHtml(item.displayName || item.handle)}">✎</button>
    <em>审批人：${escapeHtml(approvers || '未配置')}${item.pendingApprovals ? ` · ${item.pendingApprovals} 项待审批` : ''}</em>
  </article>`;
}

function openEmployee(handle) {
  const employees = state.data?.employees || [];
  const current = handle ? employees.find((item) => item.handle === handle) : null;
  state.editingHandle = current?.handle || null;
  byId('employee-dialog-title').textContent = current ? '编辑人员' : '新增人员';
  byId('employee-handle').value = current?.handle || '';
  byId('employee-handle').disabled = Boolean(current);
  byId('employee-name').value = current?.displayName || '';
  byId('employee-title').value = current?.jobTitle || '';
  byId('employee-rank').value = current?.rank || '';
  byId('employee-active').checked = current ? current.active : true;
  byId('employee-error').textContent = '';
  const selected = new Set((current?.approvers || []).map((item) => item.handle));
  byId('approver-options').innerHTML = employees
    .filter((item) => item.active && item.handle !== current?.handle)
    .map(
      (item) => `<label><input type="checkbox" value="${escapeHtml(item.handle)}" ${selected.has(item.handle) ? 'checked' : ''} /><span>${escapeHtml(item.displayName)} <small>@${escapeHtml(item.handle)}</small></span></label>`,
    )
    .join('') || '<p class="muted">暂无可选审批人</p>';
  byId('employee-dialog').showModal();
}

function openLeave() {
  const employees = (state.data?.employees || []).filter(
    (item) => item.active && Array.isArray(item.approvers) && item.approvers.length,
  );
  byId('leave-requester').innerHTML = employees
    .map((item) => `<option value="${escapeHtml(item.handle)}">${escapeHtml(item.displayName)} (@${escapeHtml(item.handle)})</option>`)
    .join('');
  const today = state.data?.today || new Date().toISOString().slice(0, 10);
  byId('leave-start').value = today;
  byId('leave-end').value = today;
  byId('leave-type').value = 'annual';
  byId('leave-reason').value = '';
  byId('leave-error').textContent = employees.length ? '' : '请先为启用中的人员配置审批人';
  byId('leave-form').querySelector('button[type="submit"]').disabled = !employees.length;
  byId('leave-dialog').showModal();
}

async function saveEmployee(event) {
  event.preventDefault();
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  byId('employee-error').textContent = '';
  const body = {
    handle: byId('employee-handle').value,
    displayName: byId('employee-name').value,
    jobTitle: byId('employee-title').value,
    rank: byId('employee-rank').value,
    approvers: [...byId('approver-options').querySelectorAll('input:checked')].map((item) => item.value),
    active: byId('employee-active').checked,
  };
  try {
    await api(
      state.editingHandle
        ? `/api/admin/employees/${encodeURIComponent(state.editingHandle)}`
        : '/api/admin/employees',
      { method: state.editingHandle ? 'PUT' : 'POST', body },
    );
    byId('employee-dialog').close();
    toast('人员信息已保存', 'success');
    await load();
  } catch (error) {
    byId('employee-error').textContent = error.message;
    handleAuthError(error);
  } finally {
    submit.disabled = false;
  }
}

async function saveLeave(event) {
  event.preventDefault();
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  byId('leave-error').textContent = '';
  try {
    await api('/api/admin/leaves', {
      method: 'POST',
      body: {
        requesterHandle: byId('leave-requester').value,
        startDate: byId('leave-start').value,
        endDate: byId('leave-end').value,
        leaveType: byId('leave-type').value,
        reason: byId('leave-reason').value,
      },
    });
    byId('leave-dialog').close();
    toast('请假申请已创建', 'success');
    await load();
  } catch (error) {
    byId('leave-error').textContent = error.message;
    handleAuthError(error);
  } finally {
    submit.disabled = false;
  }
}

async function setLeaveStatus(requestId, status) {
  if (state.deletingId) return;
  const verb = statusLabels[status] || status;
  if (!window.confirm(`确认将这张请假单设为“${verb}”？`)) return;
  try {
    await api(`/api/admin/leaves/${encodeURIComponent(requestId)}`, {
      method: 'PATCH',
      body: { status },
    });
    toast(`请假单已更新为${verb}`, 'success');
    await load();
  } catch (error) {
    handleError(error);
  }
}

async function deleteLeave(requestId) {
  if (state.deletingId) return;
  const leave = state.data?.leaves.find((item) => item.id === requestId);
  if (!leave) return;
  const name = leave.requester?.displayName || leave.requester?.handle || '该人员';
  if (!window.confirm(`确认永久删除 ${name} 的请假记录（${leave.startDate} 至 ${leave.endDate}）？\n相关审批明细也会删除，此操作无法恢复。`)) return;

  state.deletingId = requestId;
  // Ignore dashboard responses started before deletion; they may contain the removed row.
  state.loadVersion++;
  byId('refresh').disabled = true;
  render(state.data);
  try {
    const result = await api(`/api/admin/leaves/${encodeURIComponent(requestId)}`, { method: 'DELETE' });
    if (!state.csrf) return;
    state.data = { ...state.data, ...result };
    toast('请假记录已删除', 'success');
  } catch (error) {
    handleError(error);
  } finally {
    state.deletingId = null;
    byId('refresh').disabled = false;
    if (state.data) render(state.data);
  }
}

function showApp() {
  byId('login-view').hidden = true;
  byId('app-view').hidden = false;
}

function showLogin() {
  state.loadVersion++;
  state.csrf = '';
  state.data = null;
  byId('app-view').hidden = true;
  byId('login-view').hidden = false;
  byId('login-password').value = '';
}

function handleAuthError(error) {
  if (error?.status === 401) {
    showLogin();
    toast('管理登录已过期，请重新登录');
  }
}

function handleError(error) {
  handleAuthError(error);
  toast(error instanceof Error ? error.message : String(error));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

let toastTimer;
function toast(message, kind = 'error') {
  const element = byId('toast');
  element.textContent = message;
  element.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('show'), 4000);
}

byId('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  submit.disabled = true;
  byId('login-error').textContent = '';
  try {
    const session = await api('/api/admin/login', {
      method: 'POST',
      body: { username: byId('login-username').value, password: byId('login-password').value },
    });
    state.csrf = session.csrf;
    showApp();
    await load();
  } catch (error) {
    byId('login-error').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

byId('logout').addEventListener('click', async () => {
  try {
    await api('/api/admin/logout', { method: 'POST' });
  } catch {
    // Clearing the local view is enough if the server session already expired.
  }
  showLogin();
});

byId('refresh').addEventListener('click', load);
byId('new-employee').addEventListener('click', () => openEmployee());
byId('new-leave').addEventListener('click', openLeave);
byId('employee-form').addEventListener('submit', saveEmployee);
byId('leave-form').addEventListener('submit', saveLeave);
byId('employee-grid').addEventListener('click', (event) => {
  const button = event.target.closest('[data-edit-handle]');
  if (button) openEmployee(button.dataset.editHandle);
});
byId('leave-rows').addEventListener('click', (event) => {
  const deleteButton = event.target.closest('[data-delete-leave-id]');
  if (deleteButton) {
    void deleteLeave(deleteButton.dataset.deleteLeaveId);
    return;
  }
  const button = event.target.closest('[data-leave-id][data-status]');
  if (button) void setLeaveStatus(button.dataset.leaveId, button.dataset.status);
});
for (const button of document.querySelectorAll('.close-dialog')) {
  button.addEventListener('click', () => button.closest('dialog').close());
}

void bootstrap();
setInterval(() => {
  if (state.csrf && !document.hidden && !document.querySelector('dialog[open]')) void load();
}, 30000);

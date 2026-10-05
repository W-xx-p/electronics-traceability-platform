const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const roleNames = { admin: '系统管理员', quality_engineer: '质量工程师', operator: '产线操作员', viewer: '只读访客' };
let token = localStorage.getItem('electrotrace_token');
let signedInUser = null;

const escapeHTML = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
}[char]));

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 3200);
}

function showLogin(message = '') {
  $('#login-screen').hidden = false;
  $('#login-error').textContent = message;
  $('#user-chip').hidden = true;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    if (response.status === 401 && path !== '/api/v1/auth/login') {
      token = null; localStorage.removeItem('electrotrace_token'); showLogin(message);
    }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function applyPermissions(user) {
  const canReview = ['admin', 'quality_engineer'].includes(user.role);
  const canInspect = ['admin', 'quality_engineer', 'operator'].includes(user.role);
  $('[data-page="reviews"]').hidden = !canReview;
  $('[data-page="operations"]').hidden = user.role !== 'admin';
  $('#export-data').hidden = !canReview;
  $('#scan-form button[type="submit"]').disabled = !canInspect;
  $('#user-name').textContent = user.display_name;
  $('#user-role').textContent = roleNames[user.role] || user.role;
  $('#user-chip').hidden = false;
}

$$('.tab').forEach((button) => button.addEventListener('click', () => {
  $$('.tab').forEach((item) => item.classList.toggle('active', item === button));
  $$('.page').forEach((page) => page.classList.toggle('active', page.id === button.dataset.page));
  if (button.dataset.page === 'reviews') loadReviews();
  if (button.dataset.page === 'records') loadRecords();
  if (button.dataset.page === 'dashboard') loadDashboard();
  if (button.dataset.page === 'operations') loadOperations();
}));

async function loadOrders() {
  const orders = await api('/api/v1/orders');
  $('#order-select').innerHTML = orders.map((order) =>
    `<option value="${Number(order.id)}">${escapeHTML(order.order_number)} · ${escapeHTML(order.product_code)} · ${escapeHTML(order.batch_number)}</option>`
  ).join('');
}

async function loadWorkstations() {
  const stations = await api('/api/v1/workstations');
  $('#workstation-select').innerHTML = stations.filter((item) => item.active).map((item) =>
    `<option value="${Number(item.id)}">${escapeHTML(item.code)} · ${escapeHTML(item.name)}</option>`
  ).join('');
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  $('#login-error').textContent = '';
  try {
    const result = await api('/api/v1/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: $('#login-username').value.trim(), password: $('#login-password').value })
    });
    token = result.access_token;
    localStorage.setItem('electrotrace_token', token);
    signedInUser = result.user;
    applyPermissions(signedInUser);
    $('#login-screen').hidden = true;
    await Promise.all([loadOrders(), loadWorkstations()]);
    toast(`欢迎，${signedInUser.display_name}`);
  } catch (error) { $('#login-error').textContent = error.message; }
  finally { button.disabled = false; }
});

$('#logout-button').addEventListener('click', async () => {
  try { await api('/api/v1/auth/logout', { method: 'POST' }); } catch (_) {}
  token = null; signedInUser = null; localStorage.removeItem('electrotrace_token'); showLogin('已安全退出');
});

$('#image-input').addEventListener('change', (event) => {
  $('#file-name').textContent = event.target.files[0]?.name || '支持 PNG / JPEG / WebP，最大 10 MiB';
});

$('#scan-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData();
  form.append('order_id', $('#order-select').value);
  form.append('image', $('#image-input').files[0]);
  form.append('workstation_id', $('#workstation-select').value);
  const button = event.currentTarget.querySelector('button');
  button.disabled = true; button.textContent = '识别与校验中…';
  try {
    const result = await api('/api/v1/inspections', {
      method: 'POST', body: form, headers: { 'Idempotency-Key': crypto.randomUUID() }
    });
    renderInspection(result, $('#inspection-result'));
    toast(`质检完成：${result.status.toUpperCase()}`);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = '开始质检'; }
});

function renderInspection(item, target) {
  const labels = { serial_number: '序列号', product_model: '产品型号', batch_number: '批次号', manufacturing_date: '生产日期', rohs: '合规标识' };
  target.className = 'panel result';
  target.innerHTML = `
    <div><span class="status ${escapeHTML(item.status)}">${escapeHTML(item.status)}</span></div>
    <h3>${escapeHTML(item.serial_number || '未识别序列号')}</h3>
    <p>订单 ${escapeHTML(item.order_number || item.order_id)} · 平均置信度 ${(Number(item.overall_confidence) * 100).toFixed(1)}%</p>
    <div class="field-grid">${Object.entries(item.fields).map(([key, field]) => `
      <div class="field"><small>${escapeHTML(labels[key] || key)}</small><strong>${escapeHTML(field.value ?? '—')}</strong><small>置信度 ${(Number(field.confidence || 0) * 100).toFixed(1)}%</small></div>
    `).join('')}</div>
    <h4>判定原因</h4><ul class="reasons">${item.reasons.map((reason) => `<li>${escapeHTML(reason.message)}</li>`).join('')}</ul>`;
}

async function loadReviews() {
  const target = $('#review-list');
  target.innerHTML = '<div class="panel">正在加载…</div>';
  try {
    const items = await api('/api/v1/reviews');
    target.innerHTML = items.length ? items.map((item) => `
      <article class="panel review-card">
        <div><span class="status review">review</span><h3>${escapeHTML(item.serial_number || '未识别序列号')}</h3><p>${escapeHTML(item.order_number)} · 置信度 ${(Number(item.overall_confidence) * 100).toFixed(1)}%</p><p>${item.reasons.map((reason) => escapeHTML(reason.message)).join('；')}</p></div>
        <div class="review-actions"><button class="primary" onclick="resolveReview(${Number(item.id)}, 'pass')">确认放行</button><button class="primary danger" onclick="resolveReview(${Number(item.id)}, 'reject')">确认拒绝</button></div>
      </article>`).join('') : '<div class="panel empty-state"><h3>没有待复核任务</h3></div>';
  } catch (error) { target.textContent = error.message; }
}

window.resolveReview = async (id, resolution) => {
  const reason = prompt('请输入复核原因：', resolution === 'pass' ? '人工确认标签内容正确' : '人工确认标签不合格');
  if (!reason) return;
  try {
    await api(`/api/v1/reviews/${id}/claim`, { method: 'POST' });
    await api(`/api/v1/reviews/${id}/resolve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resolution, reviewer: signedInUser.username, reason, corrected_fields: {} })
    });
    toast('复核完成'); loadReviews();
  } catch (error) { toast(error.message); }
};

$('#trace-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const target = $('#trace-result');
  try {
    const data = await api(`/api/v1/trace/${encodeURIComponent($('#trace-serial').value.trim())}`);
    target.innerHTML = '';
    data.inspections.forEach((item) => { const node = document.createElement('article'); renderInspection(item, node); target.appendChild(node); });
  } catch (error) { target.textContent = error.message; }
});

async function loadDashboard() {
  try {
    const data = await api('/api/v1/dashboard/summary');
    const cards = [['检测总数', data.total], ['自动放行率', `${(data.pass_rate * 100).toFixed(1)}%`], ['待复核率', `${(data.review_rate * 100).toFixed(1)}%`], ['拒绝率', `${(data.reject_rate * 100).toFixed(1)}%`], ['平均置信度', `${(data.avg_confidence * 100).toFixed(1)}%`], ['P95 耗时', `${data.latency_ms.p95} ms`]];
    $('#metric-cards').innerHTML = cards.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${escapeHTML(value)}</strong></div>`).join('');
    const max = Math.max(...data.top_reasons.map((item) => item.count), 1);
    $('#reason-list').innerHTML = data.top_reasons.length ? data.top_reasons.map((item) => `<div class="reason-row"><span>${escapeHTML(item.code)}</span><div class="bar"><i style="width:${Number(item.count) / max * 100}%"></i></div><strong>${Number(item.count)}</strong></div>`).join('') : '<p class="empty-state">暂无异常数据</p>';
    const trendMax = Math.max(...data.daily_trend.map((item) => item.total), 1);
    $('#daily-trend').innerHTML = data.daily_trend.length ? data.daily_trend.map((item) => `
      <div class="trend-day" title="${Number(item.total)} 次检测，${Number(item.rejected || 0)} 次拒绝">
        <div class="trend-bar"><i style="height:${Number(item.passed || 0) / trendMax * 100}%"></i><i class="reject" style="height:${Number(item.rejected || 0) / trendMax * 100}%"></i></div>
        <span>${escapeHTML(item.day.slice(5))}</span>
      </div>`).join('') : '<p class="empty-state">暂无趋势数据</p>';
  } catch (error) { toast(error.message); }
}

async function loadRecords() {
  const query = new URLSearchParams();
  const serial = $('#record-serial').value.trim();
  const status = $('#record-status').value;
  if (serial) query.set('serial', serial);
  if (status) query.set('inspection_status', status);
  query.set('limit', '100');
  try {
    const data = await api(`/api/v1/inspections?${query}`);
    $('#record-rows').innerHTML = data.items.length ? data.items.map((item) => `<tr>
      <td>${escapeHTML(new Date(item.created_at).toLocaleString())}</td><td>${escapeHTML(item.serial_number || '—')}</td>
      <td>${escapeHTML(item.order_number)}</td><td>${escapeHTML(item.workstation_code || '未指定')}</td>
      <td><span class="status ${escapeHTML(item.status)}">${escapeHTML(item.status)}</span></td>
      <td>${(Number(item.overall_confidence) * 100).toFixed(1)}%</td><td>${Number(item.processing_ms)} ms</td>
      <td><button class="mini-button" onclick="viewEvidence('${escapeHTML(item.id)}')">查看</button></td>
    </tr>`).join('') : '<tr><td colspan="8">没有匹配记录</td></tr>';
  } catch (error) { toast(error.message); }
}

async function loadOperations() {
  try {
    const [users, stations, audits, integrity] = await Promise.all([
      api('/api/v1/users'), api('/api/v1/workstations'), api('/api/v1/audit-logs?limit=50'),
      api('/api/v1/audit-logs/verify')
    ]);
    $('#audit-integrity').textContent = integrity.valid ? `哈希链完整 · ${integrity.records} 条` : `完整性异常 · #${integrity.broken_at_id}`;
    $('#user-rows').innerHTML = users.map((user) => `<tr><td>${escapeHTML(user.username)}</td><td>${escapeHTML(user.display_name)}</td><td>${escapeHTML(roleNames[user.role] || user.role)}</td><td>${user.active ? '启用' : '停用'}</td></tr>`).join('');
    $('#workstation-rows').innerHTML = stations.map((station) => `<tr><td>${escapeHTML(station.code)}</td><td>${escapeHTML(station.location || '—')}</td><td>${escapeHTML(station.last_seen_at ? new Date(station.last_seen_at).toLocaleString() : '尚未连接')}</td></tr>`).join('');
    $('#audit-rows').innerHTML = audits.map((item) => `<tr><td>${escapeHTML(new Date(item.created_at).toLocaleString())}</td><td>${escapeHTML(`${item.entity_type} / ${item.entity_id}`)}</td><td>${escapeHTML(item.action)}</td><td><code>${escapeHTML(JSON.stringify(item.details))}</code></td></tr>`).join('');
  } catch (error) { toast(error.message); }
}

window.viewEvidence = async (inspectionId) => {
  try {
    const response = await fetch(`/api/v1/inspections/${encodeURIComponent(inspectionId)}/image`, {
      headers: { Authorization: `Bearer ${token}` }
    });
    if (!response.ok) throw new Error((await response.json()).detail || '证据读取失败');
    const url = URL.createObjectURL(await response.blob());
    window.open(url, '_blank', 'noopener,noreferrer');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) { toast(error.message); }
};

$('#export-data').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/v1/exports/inspections.csv', { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error('导出失败');
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = 'electrotrace-inspections.csv'; link.click();
    URL.revokeObjectURL(url);
  } catch (error) { toast(error.message); }
});

$('#refresh-reviews').addEventListener('click', loadReviews);
$('#refresh-dashboard').addEventListener('click', loadDashboard);
$('#refresh-records').addEventListener('click', loadRecords);
$('#record-filter').addEventListener('submit', (event) => { event.preventDefault(); loadRecords(); });
$('#refresh-operations').addEventListener('click', loadOperations);

async function bootstrap() {
  if (!token) return showLogin();
  try {
    signedInUser = await api('/api/v1/auth/me');
    applyPermissions(signedInUser);
    await Promise.all([loadOrders(), loadWorkstations()]);
  } catch (_) { showLogin(); }
}
bootstrap();

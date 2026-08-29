/* Operational pages: members / plans / invites / stats / storage / audit.
   Relies on helpers declared by app.js (api, toast, esc, PAGES, …). */

function q(value) {
  return encodeURIComponent(String(value == null ? '' : value));
}
function uq(value) {
  try { return decodeURIComponent(String(value == null ? '' : value)); }
  catch (e) { return String(value == null ? '' : value); }
}
function daysLeftHtml(m) {
  if (m.days_remaining == null) return '<span class="muted">不限期</span>';
  const n = Number(m.days_remaining);
  const expired = m.state === 'expired' || (m.expires_at && m.expires_at * 1000 < Date.now());
  if (expired) return '<span class="danger-text">已过期</span>';
  return `<span class="${n <= 3 ? 'danger-text' : ''}">${esc(n)} 天</span>`;
}
function billingLabel(t) {
  return ({ unlimited: '不限', traffic: '流量计费', duration: '到期计费',
    traffic_duration: '流量+到期' })[t] || t || '-';
}
function periodLabel(p) {
  return ({ daily: '每日 0 点重置', weekly: '每周一重置', monthly: '每月 1 日重置',
    total: '累计不重置' })[p] || p || '-';
}
function fmtExpiry(ts) {
  if (!ts) return '不限期';
  const d = Math.round((Number(ts) * 1000 - Date.now()) / 86400000);
  if (d < 0) return '已过期';
  if (d === 0) return '今天到期';
  return d + ' 天后';
}

/* ---------------- storage ---------------- */
PAGES.storage = async () => {
  $('#view').innerHTML = pageLoading();
  const [remotes, mounts] = await Promise.all([
    api('/api/storage/remotes'), api('/api/storage/mounts'),
  ]);
  const healthy = mounts.filter((m) => m.status === 'active').length;
  const unhealthy = mounts.length - healthy;
  const usedRemotes = new Set(mounts.map((m) => m.remote));
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('☁', remotes.length, '远程账号', 'rclone remote')}
      ${stat('⛃', mounts.length, '挂载点', '全局挂载')}
      ${stat('✓', healthy, '运行中', 'active')}
      ${stat('⚠', unhealthy, '未运行', 'inactive / 异常')}
    </div>
    ${card('添加远程账号', '全局一份，节点从挂载列表勾选，不必每台机器再填',
      `<div class="card-body"><div class="toolbar">
        <input id="sr-name" placeholder="名称 mock-drive" style="width:140px">
        <input id="sr-type" placeholder="类型 drive / s3 / alias" style="width:160px">
        <input id="sr-opt" placeholder='选项 JSON 如 {"token":"***"}' style="flex:1;min-width:180px">
        <button class="btn primary" id="sr-add">添加</button>
      </div></div>`)}
    ${tableCard('远程账号', `${remotes.length} 个`, ['名称', '类型', '状态', '测试', ''],
      remotes.map((r) => `<tr>
        <td>${esc(r.name)}</td><td>${esc(r.type)}</td>
        <td>${usedRemotes.has(r.name)
          ? '<span class="tag idle">被挂载引用</span>'
          : '<span class="tag ok">已配置</span>'}</td>
        <td><button class="btn sm" onclick="testRemote('${q(r.name)}')">测试</button>
            <span class="inline-result" id="sr-res-${esc(r.name)}"></span></td>
        <td><button class="btn sm danger" onclick="deleteRemote('${q(r.name)}')">删除</button></td>
      </tr>`).join(''))}
    ${card('添加挂载点', '目标限制在面板配置的挂载根目录内',
      `<div class="card-body"><div class="toolbar">
        <input id="sm-name" placeholder="名称 media-main" style="width:140px">
        <select id="sm-remote">${remotes.map((r) => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join('')}</select>
        <input id="sm-path" placeholder="远端路径 media" style="width:140px">
        <input id="sm-target" placeholder="目标 media-main" style="width:140px">
        <button class="btn primary" id="sm-add">添加</button>
      </div></div>`)}
    ${tableCard('挂载点', `${mounts.length} 个`, ['名称', '远程', '目标', '状态', ''],
      mounts.map((m) => `<tr>
        <td>${esc(m.name)}</td><td>${esc(m.remote)}</td>
        <td><code>${esc(m.target)}</code></td>
        <td><span class="tag ${m.status === 'active' ? 'ok' : 'idle'}">${esc(m.status)}</span></td>
        <td class="row-actions">
          <button class="btn sm" onclick="ctlMount('${q(m.name)}','start')">启动</button>
          <button class="btn sm" onclick="ctlMount('${q(m.name)}','stop')">停止</button>
          <button class="btn sm danger" onclick="deleteMount('${q(m.name)}')">删除</button>
        </td></tr>`).join(''))}`;
  $('#sr-add').onclick = addRemote;
  $('#sm-add').onclick = addMount;
};
async function addRemote() {
  let options = {};
  const raw = ($('#sr-opt').value || '').trim();
  if (raw) {
    try { options = JSON.parse(raw); } catch (e) { return toast('选项必须是 JSON 对象', 1); }
  }
  try {
    await api('/api/storage/remotes', { method: 'POST', body: JSON.stringify({
      name: $('#sr-name').value.trim(), type: $('#sr-type').value.trim(), options }) });
    toast('已添加'); renderPage('storage');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function testRemote(name) {
  name = uq(name);
  const el = document.getElementById('sr-res-' + name) || $(`#sr-res-${CSS.escape(name)}`);
  if (el) el.textContent = '测试中…';
  try {
    const r = await api(`/api/storage/remotes/${encodeURIComponent(name)}/test`, { method: 'POST' });
    if (el) el.innerHTML = r.ok
      ? `<span class="tag ok">${esc(r.message || 'ok')}</span>`
      : `<span class="tag bad">${esc(r.message || '失败')}</span>`;
  } catch (e) {
    if (el) el.innerHTML = `<span class="tag bad">${esc(e.message)}</span>`;
  }
}
async function deleteRemote(name) {
  name = uq(name);
  if (!confirm(`删除远程账号 ${name}？仍被挂载引用时会被拒绝。`)) return;
  try {
    await api(`/api/storage/remotes/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast('已删除'); renderPage('storage');
  } catch (e) { toast('无法删除: ' + e.message, 1); }
}
async function addMount() {
  try {
    await api('/api/storage/mounts', { method: 'POST', body: JSON.stringify({
      name: $('#sm-name').value.trim(), remote: $('#sm-remote').value,
      remote_path: $('#sm-path').value.trim(), target: $('#sm-target').value.trim(),
    }) });
    toast('挂载已添加'); renderPage('storage');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function ctlMount(name, action) {
  name = uq(name);
  try {
    await api(`/api/storage/mounts/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
    toast(action === 'start' ? '已启动' : '已停止'); renderPage('storage');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function deleteMount(name) {
  name = uq(name);
  if (!confirm(`删除挂载点 ${name}？`)) return;
  try {
    await api(`/api/storage/mounts/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast('已删除'); renderPage('storage');
  } catch (e) { toast('失败: ' + e.message, 1); }
}

/* ---------------- members ---------------- */
PAGES.members = async () => {
  $('#view').innerHTML = pageLoading();
  const [listing, plans] = await Promise.all([api('/api/members'), api('/api/plans')]);
  state.memberListing = listing;
  state.plans = plans;
  const members = listing.members || [];
  const now = Date.now() / 1000;
  const soon = members.filter((m) => m.expires_at && m.expires_at > now && m.expires_at - now <= 7 * 86400).length;
  const blocked = members.filter((m) => m.state === 'suspended' || m.state === 'exhausted' || m.state === 'expired').length;
  const ok = members.filter((m) => m.state === 'active').length;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('☺', members.length, '成员总数', listing.truncated ? `列表被截断（limit ${listing.limit}）` : '已纳入套餐管理')}
      ${stat('✓', ok, '正常', '可播放')}
      ${stat('⏳', soon, '即将到期', '7 天内')}
      ${stat('⊘', blocked, '已停用或超额', '含过期')}
    </div>
    <div class="filter-bar">
      <input id="mf-q" placeholder="搜索用户名 / 备注" style="min-width:180px">
      <select id="mf-plan"><option value="">全部套餐</option>${plans.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select>
      <select id="mf-state">
        <option value="">全部状态</option>
        <option value="active">正常</option><option value="expired">已过期</option>
        <option value="exhausted">已超额</option><option value="suspended">已停用</option>
        <option value="pending">待开通</option>
      </select>
      <button class="btn" id="mf-apply">筛选</button>
      <button class="btn" id="mf-create">新建 Emby 账号</button>
      <button class="btn" id="mf-preview">预览变更</button>
      <button class="btn danger" id="mf-apply-enf">应用策略</button>
    </div>
    ${listing.truncated ? '<div class="help">后端返回已达上限，计数可能不完整。筛选只作用于当前这一页。</div>' : ''}
    ${tableCard('成员', `${members.length} 人`, ['用户名', '套餐', '状态', '到期', '流量', '设备', '最后活跃', ''],
      members.map((m) => memberRow(m)).join(''))}
    ${card('纳入现有 Emby 账号', '未纳入的账号不会被套餐限制',
      `<div class="card-body">${enrolmentTable(listing.unmanaged || [], plans)}</div>`)}`;
  const memberCard = [...document.querySelectorAll('#view .card')].find((c) => {
    const h = c.querySelector('h3');
    return h && h.textContent === '成员';
  });
  if (memberCard) {
    const table = memberCard.querySelector('table');
    if (table) table.id = 'member-table';
  }
  $('#mf-apply').onclick = filterMembers;
  $('#mf-q').onkeydown = (e) => { if (e.key === 'Enter') filterMembers(); };
  $('#mf-create').onclick = createEmbyUser;
  $('#mf-preview').onclick = () => enforcementPreview();
  $('#mf-apply-enf').onclick = () => enforcementApply();
};
function memberRow(m) {
  const cap = (m.plan && m.plan.max_devices) || 0;
  const devices = cap ? `${m.device_count || 0}/${cap}` : `${m.device_count || 0}/不限`;
  const id = q(m.emby_user_id);
  const tog = m.state === 'suspended' ? 'active' : 'suspended';
  const togLabel = m.state === 'suspended' ? '启用' : '停用';
  return `<tr>
    <td>${esc(m.username)}</td><td>${esc(m.plan_name)}</td>
    <td>${stateTag(m.state)}</td><td>${daysLeftHtml(m)}</td>
    <td>${trafficBar(m.traffic_used_bytes, m.traffic_quota_bytes)}</td>
    <td>${esc(devices)}</td><td>${esc(fmtAgeTs(m.last_seen_at))}</td>
    <td class="row-actions">
      <button class="btn sm" onclick="memberDetail('${id}')">详情</button>
      <button class="btn sm" onclick="memberRenew('${id}')">续期</button>
      <button class="btn sm" onclick="memberReset('${id}')">重置流量</button>
      <button class="btn sm" onclick="memberStatus('${id}','${tog}')">${togLabel}</button>
      <button class="btn sm" onclick="memberKick('${id}')">踢下线</button>
      <button class="btn sm" onclick="memberPassword('${id}')">改密</button>
      <button class="btn sm danger" onclick="memberDelete('${id}','${q(m.username)}')">删除</button>
    </td></tr>`;
}
function enrolmentTable(unmanaged, plans) {
  if (!unmanaged.length) return '<div class="empty">所有 Emby 账号都已纳入</div>';
  const opts = plans.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  return `<table><thead><tr><th>用户</th><th>身份</th><th>套餐</th><th></th></tr></thead><tbody>
    ${unmanaged.map((u) => `<tr>
      <td>${esc(u.username)}</td>
      <td>${u.is_admin ? '<span class="tag warn">管理员</span>' : (u.disabled ? '<span class="tag idle">已禁用</span>' : '<span class="tag ok">普通</span>')}</td>
      <td><select id="enroll-plan-${esc(u.emby_user_id)}">${opts}</select></td>
      <td><button class="btn sm primary" onclick="enrolMember('${q(u.emby_user_id)}','${q(u.username)}')">纳入</button></td>
    </tr>`).join('')}
  </tbody></table>`;
}
function filterMembers() {
  const listing = state.memberListing || { members: [] };
  const qv = (($('#mf-q') || {}).value || '').trim().toLowerCase();
  const plan = (($('#mf-plan') || {}).value || '');
  const st = (($('#mf-state') || {}).value || '');
  const rows = (listing.members || []).filter((m) => {
    if (plan && m.plan_id !== plan) return false;
    if (st && m.state !== st) return false;
    if (qv && !(`${m.username} ${m.note || ''} ${m.contact || ''}`).toLowerCase().includes(qv)) return false;
    return true;
  });
  const tbody = document.querySelector('#member-table tbody');
  if (tbody) tbody.innerHTML = rows.map(memberRow).join('') || '<tr><td colspan="8" class="empty">没有匹配的成员</td></tr>';
}
async function createEmbyUser() {
  const name = prompt('新 Emby 用户名', '');
  if (!name) return;
  try {
    await api('/api/emby/users', { method: 'POST', body: JSON.stringify({ name: name.trim() }) });
    toast('账号已创建，请在下方选择套餐纳入');
    renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function enrolMember(id, username) {
  id = uq(id); username = uq(username);
  const sel = document.getElementById('enroll-plan-' + id) || $(`#enroll-plan-${CSS.escape(id)}`);
  try {
    await api(`/api/members/${encodeURIComponent(id)}`, {
      method: 'PUT', body: JSON.stringify({ username, plan_id: sel ? sel.value : '' }) });
    toast('已纳入'); renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberRenew(id) {
  id = uq(id);
  const days = prompt('续期天数（留空则按套餐默认）', '');
  if (days === null) return;
  try {
    await api(`/api/members/${encodeURIComponent(id)}/renew`, {
      method: 'POST', body: JSON.stringify(days ? { days: parseInt(days, 10) } : {}) });
    toast('已续期'); renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberReset(id) {
  id = uq(id);
  if (!confirm('确认重置该用户本周期已用流量？')) return;
  try {
    await api(`/api/members/${encodeURIComponent(id)}/reset-traffic`, { method: 'POST' });
    toast('已重置'); renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberStatus(id, status) {
  id = uq(id);
  const label = status === 'suspended' ? '停用' : '启用';
  if (!confirm(`确认${label}该用户？`)) return;
  try {
    await api(`/api/members/${encodeURIComponent(id)}/status`, {
      method: 'POST', body: JSON.stringify({ status }) });
    toast('已更新'); renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberKick(id) {
  id = uq(id);
  if (!confirm('结束该用户当前所有播放？')) return;
  try {
    const r = await api(`/api/members/${encodeURIComponent(id)}/kick`, { method: 'POST', body: '{}' });
    toast(`已踢下线 ${r.stopped || 0} 路`);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberPassword(id) {
  id = uq(id);
  const pw = prompt('新密码（至少 6 位；留空则随机生成）', '');
  if (pw === null) return;
  try {
    const r = await api(`/api/members/${encodeURIComponent(id)}/password`, {
      method: 'POST', body: JSON.stringify(pw ? { password: pw } : {}) });
    toast(r.password ? `新密码：${r.password}` : '已改密');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberDelete(id, username) {
  id = uq(id); username = uq(username);
  if (!confirm(`取消纳入「${username}」？\n\n默认只解除托管，Emby 账号会保留。`)) return;
  const wipe = confirm('同时删除 Emby 账号？此操作不可恢复。\n选「取消」则只取消纳入。');
  try {
    await api(`/api/members/${encodeURIComponent(id)}${wipe ? '?delete_emby=true' : ''}`, { method: 'DELETE' });
    toast(wipe ? '已删除纳入记录和 Emby 账号' : '已取消纳入，Emby 账号保留');
    renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberDetail(id) {
  id = uq(id);
  openModal('成员详情', '<div class="muted">加载中…</div>', { drawer: true });
  try {
    const d = await api(`/api/members/${encodeURIComponent(id)}?days=30`);
    const m = d.member || {};
    const plan = m.plan || {};
    const series = d.series || [];
    const devices = d.devices || [];
    const plays = d.recent_plays || [];
    const audit = d.audit || [];
    const body = document.querySelector('#modal-root .modal-body');
    if (!body) return;
    const bitrate = plan.max_bitrate_kbps ? ((plan.max_bitrate_kbps / 1000) + ' Mbps') : '不限';
    const deviceCap = plan.max_devices ? (plan.max_devices + ' 台') : '不限';
    body.innerHTML = `
      <div class="help">${esc(m.username)} · ${esc(m.plan_name)} · ${stateTag(m.state)}</div>
      <div class="help">限制：${esc(plan.max_streams || 1)} 路 · 码率 ${esc(bitrate)} · 设备 ${esc(deviceCap)} · ${plan.allow_transcode ? '允许转码' : '禁止转码'}</div>
      ${trafficBar(m.traffic_used_bytes, m.traffic_quota_bytes)}
      <h3 style="font-size:13px;margin:16px 0 6px">近 30 天流量</h3>
      ${sparkline(series)}
      <h3 style="font-size:13px;margin:16px 0 6px">设备</h3>
      ${devices.length ? `<table><thead><tr><th>设备</th><th>客户端</th><th>最近</th><th></th></tr></thead><tbody>
        ${devices.map((dev) => `<tr>
          <td>${esc(dev.device_name || dev.device_id)}${dev.blocked ? ' <span class="tag bad">已封</span>' : ''}</td>
          <td>${esc(dev.client)}</td><td>${esc(fmtAgeTs(dev.last_seen_at))}</td>
          <td class="row-actions">
            <button class="btn sm" onclick="blockDevice('${q(id)}','${q(dev.device_id)}',${!dev.blocked})">${dev.blocked ? '解封' : '封禁'}</button>
            <button class="btn sm danger" onclick="forgetDevice('${q(id)}','${q(dev.device_id)}')">移除</button>
          </td></tr>`).join('')}</tbody></table>` : '<div class="empty">暂无设备</div>'}
      <h3 style="font-size:13px;margin:16px 0 6px">最近播放</h3>
      ${plays.length ? `<table><thead><tr><th>内容</th><th>方式</th><th>时长</th></tr></thead><tbody>
        ${plays.slice(0, 20).map((p) => `<tr><td>${esc(p.series_name || p.item_name || '(未命名)')}</td>
          <td>${esc(p.play_method || '-')}</td><td>${esc(Math.round((p.seconds || 0) / 60))} 分</td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">暂无播放</div>'}
      <h3 style="font-size:13px;margin:16px 0 6px">审计</h3>
      ${audit.length ? audit.slice(0, 20).map((a) => `<div class="list-row"><div>
        <div class="t">${esc(a.action)}</div><div class="s">${esc(a.detail)}</div></div>
        <span class="muted">${esc(fmtAgeTs(a.ts))}</span></div>`).join('') : '<div class="empty">暂无记录</div>'}`;
  } catch (e) {
    const body = document.querySelector('#modal-root .modal-body');
    if (body) body.innerHTML = `<div class="page-error">${esc(e.message)}</div>`;
  }
}
function sparkline(series) {
  if (!series.length) return '<div class="empty">暂无数据</div>';
  const max = Math.max(...series.map((p) => p.bytes || 0), 1);
  const w = 320; const h = 64; const step = w / Math.max(series.length - 1, 1);
  const pts = series.map((p, i) => `${(i * step).toFixed(1)},${(h - (p.bytes || 0) / max * (h - 4) - 2).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" class="chart-svg" style="height:64px"><polyline fill="none" stroke="#3b6ef5" stroke-width="2" points="${pts}"/></svg>`;
}
async function blockDevice(userId, deviceId, blocked) {
  userId = uq(userId); deviceId = uq(deviceId);
  try {
    await api(`/api/members/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}/block`, {
      method: 'POST', body: JSON.stringify({ blocked }) });
    toast(blocked ? '已封禁' : '已解封'); memberDetail(userId);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function forgetDevice(userId, deviceId) {
  userId = uq(userId); deviceId = uq(deviceId);
  try {
    await api(`/api/members/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
    toast('已移除'); memberDetail(userId);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function enforcementPreview() {
  try {
    const r = await api('/api/enforcement/preview');
    const changes = r.changes || [];
    const skipped = r.skipped || [];
    openModal('策略预览', `
      <div class="help">默认只预览，不会写入 Emby。跳过原因是安全轨：管理员账号和已消失的账号不会被改。</div>
      ${tableCard('将变更', `${changes.length} 个`, ['用户', '状态', '字段'],
        changes.map((c) => `<tr><td>${esc(c.username)}</td><td>${stateTag(c.state)}</td>
          <td>${esc(Object.keys(c.changes || {}).join(', ') || '-')}</td></tr>`).join(''))}
      ${tableCard('已跳过', `${skipped.length} 个`, ['用户', '原因'],
        skipped.map((s) => `<tr><td>${esc(s.username)}</td><td>${esc(s.reason)}</td></tr>`).join(''))}
    `, { wide: true });
  } catch (e) { toast('预览失败: ' + e.message, 1); }
}
async function enforcementApply() {
  if (!confirm('确认把预览中的变更写入 Emby？此操作会改账号策略。')) return;
  try {
    const r = await api('/api/enforcement/apply', { method: 'POST', body: '{}' });
    toast(`已应用 ${r.applied || 0} 个`);
  } catch (e) { toast('失败: ' + e.message, 1); }
}

/* ---------------- plans ---------------- */
PAGES.plans = async () => {
  $('#view').innerHTML = pageLoading();
  const [plans, libs] = await Promise.all([
    api('/api/plans'), api('/api/emby/libraries').catch(() => []),
  ]);
  state.plans = plans;
  state.libraries = libs;
  const libName = (id) => {
    const hit = (libs || []).find((l) => (l.id || l.name) === id);
    return hit ? hit.name : id;
  };
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('▣', plans.length, '套餐', '用户组模板')}
      ${stat('☺', plans.reduce((a, p) => a + (p.member_count || 0), 0), '覆盖用户', '已分配套餐的成员')}
    </div>
    ${card('新建套餐', '0 在限制字段里表示不限', `<div class="card-body">${planForm('new', {}, libs)}
      <div class="toolbar"><button class="btn primary" id="plan-create">创建</button></div></div>`)}
    ${tableCard('套餐列表', `${plans.length} 个`, ['名称', '计费', '价格', '限制', '媒体库', '用户', ''],
      plans.map((p) => `<tr>
        <td>${esc(p.name)}${p.is_default ? ' <span class="tag idle">默认</span>' : ''}<div class="s muted">${esc(p.description)}</div></td>
        <td>${esc(billingLabel(p.billing_type))}<div class="s muted">${needsTraffic(p) ? periodLabel(p.traffic_period) : ''}</div></td>
        <td>${p.price_cents ? fmtMoney(p.price_cents, p.currency) : '免费'}</td>
        <td>${esc(planLimitsText(p))}</td>
        <td>${(p.libraries || []).length ? esc((p.libraries || []).map(libName).join('、')) : '全部'}</td>
        <td>${esc(p.member_count || 0)}</td>
        <td class="row-actions">
          <button class="btn sm" onclick="editPlan('${q(p.id)}')">编辑</button>
          <button class="btn sm danger" onclick="deletePlan('${q(p.id)}',${p.member_count || 0})">删除</button>
        </td></tr>`).join(''))}`;
  $('#plan-create').onclick = () => submitPlan('new');
};
function needsTraffic(p) { return p.billing_type === 'traffic' || p.billing_type === 'traffic_duration'; }
function planLimitsText(p) {
  const bits = [`${p.max_streams} 路`];
  bits.push(p.max_bitrate_kbps ? (p.max_bitrate_kbps / 1000) + ' Mbps' : '码率不限');
  bits.push(p.max_devices ? p.max_devices + ' 设备' : '设备不限');
  bits.push(p.allow_transcode ? '转码' : '禁止转码');
  if (needsTraffic(p) && p.traffic_quota_bytes) bits.push(fmtBytes(p.traffic_quota_bytes));
  if ((p.billing_type === 'duration' || p.billing_type === 'traffic_duration') && p.duration_days) {
    bits.push(p.duration_days + ' 天');
  }
  return bits.join(' · ');
}
function planForm(prefix, p, libs) {
  const sel = new Set(p.libraries || []);
  const libOpts = (libs || []).map((l) => {
    const id = l.id || l.name;
    return `<label style="margin-right:10px"><input type="checkbox" class="plib-${esc(prefix)}" value="${esc(id)}" ${sel.has(id) ? 'checked' : ''}> ${esc(l.name)}</label>`;
  }).join('') || '<span class="muted">无法读取媒体库，留空=全部</span>';
  const v = (k, d) => esc(p[k] != null ? p[k] : d);
  const gib = p.traffic_quota_bytes ? (p.traffic_quota_bytes / (1024 ** 3)).toFixed(0) : '';
  const yuan = p.price_cents != null ? (p.price_cents / 100).toFixed(2) : '0';
  return `
    <div class="help">基本</div>
    <div class="form-row"><label>ID</label><input id="${prefix}-id" value="${v('id', '')}" ${prefix === 'new' ? '' : 'disabled'} placeholder="monthly"></div>
    <div class="form-row"><label>名称</label><input id="${prefix}-name" value="${v('name', '')}"></div>
    <div class="form-row"><label>描述</label><input id="${prefix}-description" value="${v('description', '')}"></div>
    <div class="form-row"><label>排序</label><input id="${prefix}-priority" type="number" value="${v('priority', 0)}" style="width:90px"></div>
    <div class="form-row"><label>默认</label><input id="${prefix}-default" type="checkbox" ${p.is_default ? 'checked' : ''}></div>
    <div class="help">计费。流量周期：每日 0 点 / 每周一 / 每月 1 日 UTC 重置；total 表示累计不重置。</div>
    <div class="form-row"><label>类型</label>
      <select id="${prefix}-billing">
        ${['unlimited', 'traffic', 'duration', 'traffic_duration'].map((t) =>
          `<option value="${t}" ${p.billing_type === t ? 'selected' : ''}>${esc(billingLabel(t))}</option>`).join('')}
      </select></div>
    <div class="form-row"><label>流量 GiB</label><input id="${prefix}-gib" type="number" min="0" value="${esc(gib)}" style="width:110px">
      <span class="muted">流量计费必填且大于 0；不限套餐留空</span></div>
    <div class="form-row"><label>周期</label>
      <select id="${prefix}-period">
        ${['monthly', 'weekly', 'daily', 'total'].map((t) =>
          `<option value="${t}" ${p.traffic_period === t ? 'selected' : ''}>${esc(periodLabel(t))}</option>`).join('')}
      </select></div>
    <div class="form-row"><label>天数</label><input id="${prefix}-days" type="number" min="0" value="${v('duration_days', 0)}" style="width:110px">
      <span class="muted">到期计费必填且大于 0；0 在这里不是「不限」</span></div>
    <div class="form-row"><label>价格</label><input id="${prefix}-yuan" type="number" min="0" step="0.01" value="${esc(yuan)}" style="width:110px">
      <input id="${prefix}-currency" value="${v('currency', 'CNY')}" style="width:80px"></div>
    <div class="help">限制。0 = 不限（并发路数除外，最少 1）。</div>
    <div class="form-row"><label>并发</label><input id="${prefix}-streams" type="number" min="1" value="${v('max_streams', 1)}" style="width:90px"></div>
    <div class="form-row"><label>码率 kbps</label><input id="${prefix}-bitrate" type="number" min="0" value="${v('max_bitrate_kbps', 0)}" style="width:110px">
      <span class="muted">0 = 不限</span></div>
    <div class="form-row"><label>设备</label><input id="${prefix}-devices" type="number" min="0" value="${v('max_devices', 0)}" style="width:90px">
      <span class="muted">0 = 不限</span></div>
    <div class="form-row"><label>权限</label>
      <label><input id="${prefix}-transcode" type="checkbox" ${p.allow_transcode ? 'checked' : ''}> 转码</label>
      <label><input id="${prefix}-download" type="checkbox" ${p.allow_download ? 'checked' : ''}> 下载</label>
      <label><input id="${prefix}-sync" type="checkbox" ${p.allow_sync ? 'checked' : ''}> 同步</label></div>
    <div class="help">媒体库（空 = 全部）。显示名称，写入的是库 ID。</div>
    <div class="form-row"><label>媒体库</label><div>${libOpts}</div></div>`;
}
function planPayload(prefix) {
  const gib = parseFloat($(`#${prefix}-gib`).value) || 0;
  const yuan = parseFloat($(`#${prefix}-yuan`).value) || 0;
  const libraries = [...document.querySelectorAll(`.plib-${CSS.escape(prefix)}:checked`)].map((x) => x.value);
  return {
    id: $(`#${prefix}-id`).value.trim(),
    name: $(`#${prefix}-name`).value.trim(),
    description: $(`#${prefix}-description`).value.trim(),
    priority: parseInt($(`#${prefix}-priority`).value, 10) || 0,
    is_default: $(`#${prefix}-default`).checked,
    billing_type: $(`#${prefix}-billing`).value,
    traffic_quota_bytes: Math.round(gib * 1024 ** 3),
    traffic_period: $(`#${prefix}-period`).value,
    duration_days: parseInt($(`#${prefix}-days`).value, 10) || 0,
    price_cents: Math.round(yuan * 100),
    currency: $(`#${prefix}-currency`).value.trim() || 'CNY',
    max_streams: parseInt($(`#${prefix}-streams`).value, 10) || 1,
    max_bitrate_kbps: parseInt($(`#${prefix}-bitrate`).value, 10) || 0,
    max_devices: parseInt($(`#${prefix}-devices`).value, 10) || 0,
    allow_transcode: $(`#${prefix}-transcode`).checked,
    allow_download: $(`#${prefix}-download`).checked,
    allow_sync: $(`#${prefix}-sync`).checked,
    libraries,
  };
}
async function submitPlan(prefix, existingId) {
  const payload = planPayload(prefix);
  try {
    if (existingId) {
      if (!confirm('保存后会在下一次策略下发时改写该套餐下所有用户的限制。可先预览。')) return;
      await api(`/api/plans/${encodeURIComponent(existingId)}`, { method: 'PUT', body: JSON.stringify(payload) });
      toast('已保存'); closeModal();
    } else {
      await api('/api/plans', { method: 'POST', body: JSON.stringify(payload) });
      toast('已创建');
    }
    renderPage('plans');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
function editPlan(id) {
  id = uq(id);
  const p = (state.plans || []).find((x) => x.id === id);
  if (!p) return;
  openModal('编辑套餐 ' + p.name, `${planForm('edit', p, state.libraries || [])}
    <div class="toolbar">
      <button class="btn" id="plan-preview">预览变更</button>
      <button class="btn primary" id="plan-save">保存</button>
    </div>`, { wide: true });
  $('#plan-save').onclick = () => submitPlan('edit', id);
  $('#plan-preview').onclick = () => enforcementPreview();
}
async function deletePlan(id, count) {
  id = uq(id);
  if (count) return toast(`仍有 ${count} 个用户在使用该套餐，请先转移他们`, 1);
  if (!confirm(`删除套餐 ${id}？`)) return;
  try {
    await api(`/api/plans/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('已删除'); renderPage('plans');
  } catch (e) { toast('无法删除: ' + e.message, 1); }
}

/* ---------------- invites ---------------- */
PAGES.invites = async () => {
  $('#view').innerHTML = pageLoading();
  const [invites, plans] = await Promise.all([api('/api/invites'), api('/api/plans')]);
  const rank = (r) => (r.usable ? 0 : 1);
  const rows = invites.slice().sort((a, b) => rank(a) - rank(b) || b.created_at - a.created_at);
  $('#view').innerHTML = `
    ${card('发放邀请码', '用户用公开页面自行开通，不必口头传密码',
      `<div class="card-body"><div class="toolbar">
        <select id="iv-plan">${plans.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select>
        <input id="iv-uses" type="number" min="1" value="1" style="width:80px" title="可用次数">
        <input id="iv-days" type="number" min="0" value="7" style="width:80px" title="有效天数，0=永久">
        <input id="iv-count" type="number" min="1" value="1" style="width:80px" title="生成个数">
        <input id="iv-note" placeholder="备注（可选）" style="flex:1;min-width:140px">
        <button class="btn primary" id="iv-go">生成</button>
      </div></div>`)}
    ${tableCard('邀请码', `${rows.length} 个`, ['邀请码', '套餐', '用量', '到期', '状态', ''],
      rows.map((r) => {
        const st = r.revoked ? ['revoked', '已作废'] : r.expired ? ['expired', '已过期']
          : r.exhausted ? ['exhausted', '已用完'] : ['ok', '可用'];
        const link = (location.origin || '') + '/invite/' + encodeURIComponent(r.code);
        return `<tr>
          <td><code>${esc(r.code)}</code></td><td>${esc(r.plan_name)}</td>
          <td>${esc(r.used_count)}/${esc(r.max_uses)}</td>
          <td>${esc(fmtExpiry(r.expires_at))}</td>
          <td><span class="tag ${st[0]}">${esc(st[1])}</span></td>
          <td class="row-actions">
            <button class="btn sm" onclick="copyText(uq('${q(r.code)}'))">复制码</button>
            <button class="btn sm" onclick="copyText(uq('${q(link)}'))">复制链接</button>
            <button class="btn sm" onclick="revokeInvite('${q(r.code)}')">作废</button>
            <button class="btn sm danger" onclick="deleteInvite('${q(r.code)}')">删除</button>
          </td></tr>`;
      }).join(''))}`;
  $('#iv-go').onclick = issueInvites;
};
async function issueInvites() {
  try {
    await api('/api/invites', { method: 'POST', body: JSON.stringify({
      plan_id: $('#iv-plan').value, max_uses: parseInt($('#iv-uses').value, 10) || 1,
      valid_days: parseInt($('#iv-days').value, 10) || 0,
      count: parseInt($('#iv-count').value, 10) || 1, note: $('#iv-note').value.trim(),
    }) });
    toast('已生成'); renderPage('invites');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function revokeInvite(code) {
  code = uq(code);
  if (!confirm('作废后该码立即不可兑换。继续？')) return;
  try { await api(`/api/invites/${encodeURIComponent(code)}/revoke`, { method: 'POST' }); toast('已作废'); renderPage('invites'); }
  catch (e) { toast('失败: ' + e.message, 1); }
}
async function deleteInvite(code) {
  code = uq(code);
  if (!confirm('删除邀请码记录？')) return;
  try { await api(`/api/invites/${encodeURIComponent(code)}`, { method: 'DELETE' }); toast('已删除'); renderPage('invites'); }
  catch (e) { toast('失败: ' + e.message, 1); }
}

/* ---------------- stats ---------------- */
PAGES.stats = async () => {
  const days = state.statsDays || 30;
  $('#view').innerHTML = pageLoading();
  const [overview, prev, daily, users, titles, clients, nodes, methods] = await Promise.all([
    api(`/api/stats/overview?days=${days}`),
    api(`/api/stats/overview?days=${days * 2}`).catch(() => null),
    api(`/api/stats/daily?days=${days}`),
    api(`/api/stats/top-users?days=${days}`),
    api(`/api/stats/top-titles?days=${days}`),
    api(`/api/stats/clients?days=${days}`),
    api(`/api/stats/nodes?days=${days}`),
    api(`/api/stats/play-methods?days=${days}`).catch(() => ({ total: 0, methods: [] })),
  ]);
  const trafficNow = (overview.traffic || {}).window_bytes || 0;
  const hoursNow = (overview.traffic || {}).window_hours || 0;
  const playsNow = (overview.playback || {}).window_plays || 0;
  const activeNow = (overview.members || {}).active || 0;
  const usersByHours = (users || []).slice().sort((a, b) => (b.hours || 0) - (a.hours || 0));
  $('#view').innerHTML = `
    <div class="filter-bar">
      ${[7, 30, 90].map((n) => `<button class="btn ${n === days ? 'primary' : ''}" onclick="statsRange(${n})">${n} 天</button>`).join('')}
    </div>
    <div class="stat-grid">
      ${stat('⇅', fmtBytes(trafficNow), '总流量', deltaText(trafficNow, earlier(prev, overview, 'traffic', 'window_bytes')))}
      ${stat('▶', hoursNow + ' 小时', '总观看时长', deltaText(hoursNow, earlier(prev, overview, 'traffic', 'window_hours')))}
      ${stat('☺', activeNow, '活跃用户', '当前正常成员')}
      ${stat('▣', playsNow, '播放次数', deltaText(playsNow, earlier(prev, overview, 'playback', 'window_plays')))}
    </div>
    ${card('每日趋势', '流量与观看时长', `<div class="chart-wrap">${trendChart(daily)}<div class="chart-tip" id="chart-tip"></div></div>
      <div class="chart-legend"><span><i class="swatch" style="background:#3b6ef5"></i>流量</span>
        <span><i class="swatch" style="background:#12b76a"></i>观看时长</span></div>`)}
    ${card('转码占比', '直通越多，CPU 越省', playMethodPanel(methods, overview))}
    <div class="grid-2">
      ${tableCard('热门内容', `${titles.length} 条`, ['内容', '播放', '分钟'],
        titles.map((t) => `<tr><td>${esc(t.title || '(未命名)')}</td><td>${esc(t.plays)}</td>
          <td>${esc(Math.round((t.hours || 0) * 60))}</td></tr>`).join(''))}
      ${tableCard('用户排行 · 流量', '按消耗流量', ['用户', '流量', '时长'],
        users.map((u) => `<tr><td>${esc(u.username)}</td><td>${fmtBytes(u.bytes)}</td>
          <td>${esc(u.hours)} 小时</td></tr>`).join(''))}
    </div>
    <div class="grid-2">
      ${tableCard('用户排行 · 时长', '按观看时长', ['用户', '时长', '流量'],
        usersByHours.map((u) => `<tr><td>${esc(u.username)}</td><td>${esc(u.hours)} 小时</td>
          <td>${fmtBytes(u.bytes)}</td></tr>`).join(''))}
      ${card('客户端分布', '', barList((clients || []).map((c) => ({ label: c.client, pct: c.percent, extra: c.plays + ' 次' }))))}
    </div>
    ${card('节点分布', '', barList((nodes || []).map((n) => ({ label: n.node, pct: n.percent, extra: fmtBytes(n.bytes) }))))}`;
  bindChartHover(daily);
};
function statsRange(days) { state.statsDays = days; renderPage('stats'); }
function earlier(prev, now, group, key) {
  if (!prev || !prev[group] || !now || !now[group]) return null;
  return Math.max(0, Number(prev[group][key] || 0) - Number(now[group][key] || 0));
}
function deltaText(now, prev) {
  /* stat() escapes `sub`, so this must be plain text. */
  if (prev == null) return '—';
  if (!prev && !now) return '持平';
  if (!prev) return '↑ 新数据';
  const pct = Math.round((now - prev) / prev * 100);
  const arrow = pct > 0 ? '↑' : pct < 0 ? '↓' : '→';
  return `${arrow} ${Math.abs(pct)}%`;
}
function playMethodPanel(methods, overview) {
  const ratio = (overview.playback || {}).direct_ratio;
  const t = methods || {};
  if (!t.total) {
    return '<div class="empty">还没有播放记录</div>';
  }
  return `<div class="card-body">
    <div class="stat-grid">
      ${stat('▷', (t.direct_ratio == null ? (ratio == null ? '-' : ratio + '%') : t.direct_ratio + '%'), '直通占比', `${t.direct || 0} 次`)}
      ${stat('⚙', (t.transcode_ratio == null ? '-' : t.transcode_ratio + '%'), '转码占比', `${t.transcode || 0} 次`)}
    </div>
    ${barList((t.methods || []).map((m) => ({
      label: m.method, pct: t.total ? Math.round(m.plays / t.total * 100) : 0, extra: m.plays + ' 次',
    })))}
  </div>`;
}
function barList(items) {
  if (!items.length) return '<div class="empty">暂无数据</div>';
  return items.map((it) => `<div class="hbar"><div class="lab" title="${esc(it.label)}">${esc(it.label)}</div>
    <div class="track"><i style="width:${Math.max(0, Math.min(100, it.pct || 0))}%"></i></div>
    <div class="pct">${esc(it.pct || 0)}% ${esc(it.extra || '')}</div></div>`).join('');
}
function trendChart(daily) {
  const w = 640; const h = 200; const pad = 28;
  if (!daily.length) {
    return `<svg viewBox="0 0 ${w} ${h}" class="chart-svg"><text x="20" y="100" fill="#8a93a3">暂无数据</text></svg>`;
  }
  const maxB = Math.max(...daily.map((d) => d.bytes || 0), 1);
  const maxH = Math.max(...daily.map((d) => d.hours || 0), 0.01);
  const innerW = w - pad * 2; const innerH = h - pad * 2;
  const x = (i) => pad + (daily.length === 1 ? innerW / 2 : i * innerW / (daily.length - 1));
  const yB = (v) => pad + innerH - (v / maxB) * innerH;
  const yH = (v) => pad + innerH - (v / maxH) * innerH;
  const line = (key, yn, color) => {
    const pts = daily.map((d, i) => `${x(i).toFixed(1)},${yn(d[key] || 0).toFixed(1)}`).join(' ');
    const area = `${pad},${pad + innerH} ${pts} ${x(daily.length - 1).toFixed(1)},${pad + innerH}`;
    return `<polygon fill="${color}" fill-opacity="0.12" points="${area}"/>
      <polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}"/>`;
  };
  const hits = daily.map((d, i) => `<circle class="chart-hit" data-i="${i}" cx="${x(i).toFixed(1)}" cy="${yB(d.bytes || 0).toFixed(1)}" r="8" fill="transparent"/>`).join('');
  return `<svg viewBox="0 0 ${w} ${h}" class="chart-svg" id="trend-svg">${line('bytes', yB, '#3b6ef5')}${line('hours', yH, '#12b76a')}${hits}</svg>`;
}
function bindChartHover(daily) {
  const svg = $('#trend-svg'); const tip = $('#chart-tip');
  if (!svg || !tip) return;
  svg.querySelectorAll('.chart-hit').forEach((el) => {
    el.addEventListener('mousemove', (ev) => {
      const d = daily[Number(el.dataset.i)];
      if (!d) return;
      tip.style.display = 'block';
      tip.style.left = (ev.offsetX + 12) + 'px';
      tip.style.top = (ev.offsetY + 8) + 'px';
      tip.textContent = `${d.day} · ${fmtBytes(d.bytes)} · ${d.hours} 小时`;
    });
    el.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  });
}

/* ---------------- audit ---------------- */
PAGES.audit = async () => {
  $('#view').innerHTML = pageLoading();
  const rows = await api('/api/audit?limit=200');
  state.audit = rows;
  $('#view').innerHTML = `
    <div class="filter-bar">
      <input id="au-actor" placeholder="操作者" style="width:120px">
      <input id="au-action" placeholder="动作" style="width:140px">
      <input id="au-subject" placeholder="对象" style="width:140px">
      <button class="btn" id="au-go">筛选</button>
    </div>
    ${tableCard('审计日志', `${rows.length} 条`, ['时间', '操作者', '动作', '对象', '详情', '结果'],
      auditRows(rows))}`;
  $('#au-go').onclick = filterAudit;
};
function auditRows(rows) {
  return rows.map((a) => `<tr>
    <td>${esc(fmtAgeTs(a.ts))}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td>
    <td>${esc(a.subject)}</td><td>${esc(a.detail)}</td>
    <td>${a.ok ? '<span class="tag ok">成功</span>' : '<span class="tag bad">失败</span>'}</td>
  </tr>`).join('');
}
async function filterAudit() {
  const qs = new URLSearchParams();
  qs.set('limit', '200');
  const actor = $('#au-actor').value.trim();
  const action = $('#au-action').value.trim();
  const subject = $('#au-subject').value.trim();
  if (actor) qs.set('actor', actor);
  if (action) qs.set('action', action);
  if (subject) qs.set('subject', subject);
  try {
    const rows = await api('/api/audit?' + qs.toString());
    const tbody = document.querySelector('#view table tbody');
    if (tbody) tbody.innerHTML = auditRows(rows) || '<tr><td colspan="6" class="empty">没有匹配记录</td></tr>';
  } catch (e) { toast('筛选失败: ' + e.message, 1); }
}

if (typeof bootPanel === 'function') bootPanel();

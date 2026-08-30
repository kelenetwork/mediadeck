/* Operational pages: members / groups / stats / storage / audit.
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
  return ({ none: '不计费', traffic: '仅流量', time: '仅时间',
    both: '时间+流量' })[t] || t || '-';
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
  const [listing, groups] = await Promise.all([api('/api/members'), api('/api/groups')]);
  state.memberListing = listing;
  state.groups = groups;
  const members = listing.members || [];
  const now = Date.now() / 1000;
  const soon = members.filter((m) => m.expires_at && m.expires_at > now && m.expires_at - now <= 7 * 86400).length;
  const blocked = members.filter((m) => m.state === 'suspended' || m.state === 'exhausted' || m.state === 'expired').length;
  const ok = members.filter((m) => m.state === 'active').length;
  const unmanagedN = (listing.unmanaged || []).filter((u) => !u.is_admin).length;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('☺', members.length, '成员总数', listing.truncated ? `列表被截断（limit ${listing.limit}）` : '已纳入管理')}
      ${stat('✓', ok, '正常', '可播放')}
      ${stat('⏳', soon, '即将到期', '7 天内')}
      ${stat('⊘', blocked, '已停用或超额', '含过期')}
    </div>
    <div class="filter-bar">
      <input id="mf-q" placeholder="搜索用户名 / 备注" style="min-width:180px">
      <select id="mf-group"><option value="">全部用户组</option>${groups.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('')}</select>
      <select id="mf-role">
        <option value="">全部角色</option>
        <option value="admin">管理员</option><option value="uploader">上片员</option>
      </select>
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
    ${tableCard('成员', `${members.length} 人`, ['用户', '用户组', '状态', '到期', '流量', '设备', '最后活跃', ''],
      members.map((m) => memberRow(m)).join(''))}
    ${card('纳入现有 Emby 账号', '未纳入的账号不受任何限制，也不计流量',
      `<div class="card-body">
        ${unmanagedN ? `<div class="toolbar"><button class="btn primary" id="mf-enroll-all">一键纳入默认组（${unmanagedN} 个）</button></div>` : ''}
        ${enrolmentTable(listing.unmanaged || [], groups)}</div>`)}`;
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
  if ($('#mf-enroll-all')) $('#mf-enroll-all').onclick = enrollAllDefaults;
};
async function enrollAllDefaults() {
  if (!confirm('把所有未纳入的普通 Emby 账号划进默认用户组？\n\n按组设置开始计时计流量。')) return;
  try {
    const r = await api('/api/members/enroll-defaults', { method: 'POST', body: '{}' });
    toast(`已纳入 ${r.enrolled || 0} 个`); renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
function roleTags(m) {
  return (m.roles || []).map((r) => r === 'admin'
    ? ' <span class="tag warn">管理员</span>'
    : ' <span class="tag idle">上片员</span>').join('');
}
function memberRow(m) {
  const devices = m.max_devices ? `${m.device_count || 0}/${m.max_devices}` : `${m.device_count || 0}/不限`;
  const id = q(m.emby_user_id);
  const tog = m.state === 'suspended' ? 'active' : 'suspended';
  const togLabel = m.state === 'suspended' ? '启用' : '停用';
  return `<tr>
    <td>${esc(m.username)}${roleTags(m)}</td><td>${esc(m.group_name)}</td>
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
function enrolmentTable(unmanaged, groups) {
  if (!unmanaged.length) return '<div class="empty">所有 Emby 账号都已纳入</div>';
  const opts = groups.map((g) => `<option value="${esc(g.id)}" ${g.is_default ? 'selected' : ''}>${esc(g.name)}</option>`).join('');
  return `<table><thead><tr><th>用户</th><th>身份</th><th>用户组</th><th></th></tr></thead><tbody>
    ${unmanaged.map((u) => `<tr>
      <td>${esc(u.username)}</td>
      <td>${u.is_admin ? '<span class="tag warn">Emby 管理员</span>' : (u.disabled ? '<span class="tag idle">已禁用</span>' : '<span class="tag ok">普通</span>')}</td>
      <td><select id="enroll-group-${esc(u.emby_user_id)}">${opts}</select></td>
      <td><button class="btn sm primary" onclick="enrolMember('${q(u.emby_user_id)}','${q(u.username)}')">纳入</button></td>
    </tr>`).join('')}
  </tbody></table>`;
}
function filterMembers() {
  const listing = state.memberListing || { members: [] };
  const qv = (($('#mf-q') || {}).value || '').trim().toLowerCase();
  const grp = (($('#mf-group') || {}).value || '');
  const role = (($('#mf-role') || {}).value || '');
  const st = (($('#mf-state') || {}).value || '');
  const rows = (listing.members || []).filter((m) => {
    if (grp && m.group_id !== grp) return false;
    if (role && !(m.roles || []).includes(role)) return false;
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
    toast('账号已创建，请在下方选择用户组纳入');
    renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function enrolMember(id, username) {
  id = uq(id); username = uq(username);
  const sel = document.getElementById('enroll-group-' + id) || $(`#enroll-group-${CSS.escape(id)}`);
  try {
    await api(`/api/members/${encodeURIComponent(id)}`, {
      method: 'PUT', body: JSON.stringify({ username, group_id: sel ? sel.value : '' }) });
    toast('已纳入'); renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberRenew(id) {
  id = uq(id);
  const days = prompt('续期天数（留空则按用户组默认）', '');
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
function kbpsToMBps(n) {
  n = Number(n || 0);
  if (!n) return 0;
  return Math.round((n * 125 / 1048576) * 10) / 10;
}
function mBpsToKbps(n) {
  n = Number(n || 0);
  if (!n) return 0;
  return Math.round(n * 1048576 / 125);
}
function fmtKbps(n) {
  n = Number(n || 0);
  if (!n) return '不限';
  const mb = kbpsToMBps(n);
  return (Number.isInteger(mb) ? String(mb) : mb.toFixed(1)) + ' MB/s';
}
function boolLabel(v) { return v ? '允许' : '禁止'; }
function localInputFromTs(ts) {
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function flagSelect(id, value) {
  const cur = (value === 0 || value === false) ? '0' : (value === 1 || value === true) ? '1' : '';
  return `<select id="${esc(id)}">
    <option value="" ${cur === '' ? 'selected' : ''}>继承</option>
    <option value="1" ${cur === '1' ? 'selected' : ''}>允许</option>
    <option value="0" ${cur === '0' ? 'selected' : ''}>禁止</option>
  </select>`;
}
function ovSource(m, key, groupVal, effVal, fmt) {
  const ov = (m.overrides || {});
  const hit = (m.overridden_keys || []).includes(key) || Object.prototype.hasOwnProperty.call(ov, key);
  const shown = fmt ? fmt(effVal) : String(effVal == null ? '-' : effVal);
  const inherited = fmt ? fmt(groupVal) : String(groupVal == null ? '-' : groupVal);
  if (hit) return `<span class="tag override">已覆盖(${esc(shown)})</span>`;
  return `<span class="tag inherit">继承用户组(${esc(inherited)})</span>`;
}
const BW_PRESETS = [
  { label: '不限速', mbps: 0 },
  { label: '5 MB/s', mbps: 5 },
  { label: '10 MB/s', mbps: 10 },
  { label: '15 MB/s', mbps: 15 },
  { label: '20 MB/s', mbps: 20 },
];
function bwPresetButtons(inputId) {
  return BW_PRESETS.map((p) =>
    `<button class="btn sm" type="button" onclick="document.getElementById('${inputId}').value='${p.mbps}'">${esc(p.label)}</button>`).join(' ');
}
function overrideEditor(m, libs) {
  const ov = m.overrides || {};
  const grp = m.group || {};
  const eff = m.effective || {};
  const sel = new Set(ov.libraries || []);
  const libOpts = (libs || []).map((l) => {
    const id = l.id || l.name;
    return `<label style="margin-right:10px"><input type="checkbox" class="ov-lib" value="${esc(id)}" ${sel.has(id) ? 'checked' : ''}> ${esc(l.name)}</label>`;
  }).join('') || '<span class="muted">无法读取媒体库</span>';
  const num = (k) => (ov[k] != null ? ov[k] : '');
  const mode = ov.libraries_mode || 'inherit';
  const exp = ov.expires_at_override ? localInputFromTs(ov.expires_at_override) : '';
  const extraGib = ov.extra_traffic_bytes ? (ov.extra_traffic_bytes / (1024 ** 3)).toFixed(2) : '';
  return `
    <div class="ov-row"><div class="ov-label">并发</div>
      <div class="ov-src">${ovSource(m, 'max_streams', grp.max_streams || 0, eff.max_streams, (v) => v ? v + ' 路' : '不限')}</div>
      <div class="ov-controls"><input id="ov-streams" type="number" min="0" placeholder="继承" value="${esc(num('max_streams'))}" style="width:90px">
        <span class="muted">0=不限</span>
        <button class="btn sm" type="button" onclick="clearOverrideField('max_streams')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">带宽限速</div>
      <div class="ov-src">${ovSource(m, 'bandwidth_limit_kbps', grp.bandwidth_limit_kbps || 0, eff.bandwidth_limit_kbps, fmtKbps)}</div>
      <div class="ov-controls">
        <div style="margin-bottom:4px">${bwPresetButtons('ov-bandwidth')}</div>
        <input id="ov-bandwidth" type="number" min="0" step="0.1" placeholder="继承" value="${esc(num('bandwidth_limit_kbps') === '' ? '' : kbpsToMBps(num('bandwidth_limit_kbps')))}" style="width:110px">
        <span class="muted">MB/s，0=不限速。保存后正在播放的人会重签限速。</span>
        <button class="btn sm" type="button" onclick="clearOverrideField('bandwidth_limit_kbps')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">设备</div>
      <div class="ov-src">${ovSource(m, 'max_devices', grp.max_devices || 0, eff.max_devices, (v) => v ? v + ' 台' : '不限')}</div>
      <div class="ov-controls"><input id="ov-devices" type="number" min="0" placeholder="继承" value="${esc(num('max_devices'))}" style="width:90px">
        <button class="btn sm" type="button" onclick="clearOverrideField('max_devices')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">转码</div>
      <div class="ov-src">${ovSource(m, 'allow_transcode', grp.allow_transcode, eff.allow_transcode, boolLabel)}</div>
      <div class="ov-controls">${flagSelect('ov-transcode', ov.allow_transcode)}
        <button class="btn sm" type="button" onclick="clearOverrideField('allow_transcode')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">下载</div>
      <div class="ov-src">${ovSource(m, 'allow_download', grp.allow_download, eff.allow_download, boolLabel)}</div>
      <div class="ov-controls">${flagSelect('ov-download', ov.allow_download)}
        <button class="btn sm" type="button" onclick="clearOverrideField('allow_download')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">媒体库</div>
      <div class="ov-src">${ovSource(m, 'libraries_mode', 'inherit', eff.libraries_mode || 'inherit')}</div>
      <div class="ov-controls">
        <select id="ov-libmode">
          ${['inherit', 'replace', 'extend'].map((x) => `<option value="${x}" ${mode === x ? 'selected' : ''}>${esc({ inherit: '继承', replace: '替换', extend: '追加' }[x])}</option>`).join('')}
        </select>
        <button class="btn sm" type="button" onclick="clearOverrideField('libraries')">还原</button>
        <div>${libOpts}</div></div></div>
    <div class="ov-row"><div class="ov-label">到期覆盖</div>
      <div class="ov-src">${ovSource(m, 'expires_at_override', grp.duration_days ? (grp.duration_days + ' 天') : '不限期', m.expires_at_effective || m.expires_at, (v) => v ? fmtExpiry(v) : '不限期')}</div>
      <div class="ov-controls"><input id="ov-exp" type="datetime-local" value="${esc(exp)}">
        <button class="btn sm" type="button" onclick="clearOverrideField('expires_at_override')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">额外流量</div>
      <div class="ov-src">${ovSource(m, 'extra_traffic_bytes', 0, (m.overrides || {}).extra_traffic_bytes || 0, fmtBytes)}</div>
      <div class="ov-controls"><input id="ov-extra" type="number" min="0" step="0.01" placeholder="0" value="${esc(extraGib)}" style="width:110px">
        <span class="muted">GiB，叠加在本月额度上，月初清零</span>
        <button class="btn sm" type="button" onclick="clearOverrideField('extra_traffic_bytes')">还原</button></div></div>
    <div class="toolbar" style="margin-top:10px">
      <button class="btn primary" type="button" id="ov-save">保存覆盖</button>
      <button class="btn" type="button" id="ov-clear">全部还原</button>
    </div>`;
}
function collectOverridesFromForm(existing) {
  const ov = Object.assign({}, existing || {});
  const streams = ($('#ov-streams') || {}).value;
  if (streams === '' || streams == null) delete ov.max_streams;
  else ov.max_streams = parseInt(streams, 10);
  const bandwidth = ($('#ov-bandwidth') || {}).value;
  if (bandwidth === '' || bandwidth == null) delete ov.bandwidth_limit_kbps;
  else ov.bandwidth_limit_kbps = mBpsToKbps(parseFloat(bandwidth));
  const devices = ($('#ov-devices') || {}).value;
  if (devices === '' || devices == null) delete ov.max_devices;
  else ov.max_devices = parseInt(devices, 10);
  const readFlag = (elId, key) => {
    const v = (($('#' + elId) || {}).value || '');
    if (v === '') delete ov[key];
    else ov[key] = v === '1' ? 1 : 0;
  };
  readFlag('ov-transcode', 'allow_transcode');
  readFlag('ov-download', 'allow_download');
  const mode = (($('#ov-libmode') || {}).value || 'inherit');
  const libs = [...document.querySelectorAll('.ov-lib:checked')].map((x) => x.value);
  if (mode === 'inherit') {
    delete ov.libraries_mode; delete ov.libraries;
  } else {
    ov.libraries_mode = mode;
    ov.libraries = libs;
  }
  const exp = (($('#ov-exp') || {}).value || '').trim();
  if (!exp) delete ov.expires_at_override;
  else ov.expires_at_override = Math.floor(new Date(exp).getTime() / 1000);
  const extra = (($('#ov-extra') || {}).value || '').trim();
  if (extra === '') delete ov.extra_traffic_bytes;
  else ov.extra_traffic_bytes = Math.round(parseFloat(extra) * 1024 ** 3);
  return ov;
}
async function saveOverrides(userId) {
  const m = (state.memberDetail && state.memberDetail.member) || {};
  try {
    await api(`/api/members/${encodeURIComponent(userId)}/overrides`, {
      method: 'PUT', body: JSON.stringify(collectOverridesFromForm(m.overrides || {})),
    });
    toast('已保存覆盖');
    memberDetail(userId);
    renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function clearOverrideField(key) {
  const m = (state.memberDetail && state.memberDetail.member) || {};
  const id = m.emby_user_id;
  if (!id) return;
  const ov = Object.assign({}, m.overrides || {});
  delete ov[key];
  if (key === 'libraries') { delete ov.libraries; delete ov.libraries_mode; }
  try {
    await api(`/api/members/${encodeURIComponent(id)}/overrides`, {
      method: 'PUT', body: JSON.stringify(ov),
    });
    toast('已还原该字段');
    memberDetail(id);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function clearAllOverrides(userId) {
  if (!confirm('还原全部覆盖，改回完全继承用户组？')) return;
  try {
    await api(`/api/members/${encodeURIComponent(userId)}/overrides`, {
      method: 'PUT', body: JSON.stringify({}),
    });
    toast('已全部还原');
    memberDetail(userId);
    renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberAddTraffic(userId) {
  const gib = prompt('本月加多少流量（GiB）？月初自动清零', '100');
  if (gib === null) return;
  const m = (state.memberDetail && state.memberDetail.member) || {};
  const ov = Object.assign({}, m.overrides || {});
  ov.extra_traffic_bytes = (ov.extra_traffic_bytes || 0) + Math.round((parseFloat(gib) || 0) * 1024 ** 3);
  try {
    await api(`/api/members/${encodeURIComponent(userId)}/overrides`, {
      method: 'PUT', body: JSON.stringify(ov) });
    toast('已加量'); memberDetail(userId);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberToggleRole(userId, role) {
  const m = (state.memberDetail && state.memberDetail.member) || {};
  const roles = new Set(m.roles || []);
  if (roles.has(role)) roles.delete(role); else roles.add(role);
  try {
    await api(`/api/members/${encodeURIComponent(userId)}/roles`, {
      method: 'POST', body: JSON.stringify({ roles: [...roles] }) });
    toast('已更新角色'); memberDetail(userId);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function memberDetail(id) {
  id = uq(id);
  openModal('成员详情', '<div class="muted">加载中…</div>', { drawer: true });
  try {
    const [d, libs] = await Promise.all([
      api(`/api/members/${encodeURIComponent(id)}?days=30`),
      api('/api/emby/libraries').catch(() => []),
    ]);
    state.memberDetail = d;
    const m = d.member || {};
    const usage = d.usage || {};
    const series = usage.series || d.series || [];
    const devices = d.devices || [];
    const plays = d.plays || d.recent_plays || [];
    const audit = d.audit || [];
    const body = document.querySelector('#modal-root .modal-body');
    if (!body) return;
    const tog = m.state === 'suspended' ? 'active' : 'suspended';
    const togLabel = m.state === 'suspended' ? '启用' : '停用';
    const hasAdmin = (m.roles || []).includes('admin');
    const hasUploader = (m.roles || []).includes('uploader');
    body.innerHTML = `
      <div class="help">${esc(m.username)} · ${esc(m.group_name)} · ${stateTag(m.state)}${roleTags(m)}
        <span class="muted">${esc(m.state_reason || '')}</span></div>
      <div class="help">到期 ${esc(fmtExpiry(m.expires_at_effective || m.expires_at))} · 流量 ${fmtBytes(m.traffic_used_bytes)} / ${fmtQuota(m.traffic_quota_bytes)}</div>
      <div class="detail-actions">
        <button class="btn sm" onclick="memberKick('${q(id)}')">踢下线</button>
        <button class="btn sm" onclick="memberPassword('${q(id)}')">重置密码</button>
        <button class="btn sm" onclick="memberStatus('${q(id)}','${tog}')">${togLabel}</button>
        <button class="btn sm" onclick="memberRenew('${q(id)}')">续期</button>
        <button class="btn sm" onclick="memberAddTraffic('${q(id)}')">加流量</button>
      </div>
      <h3 style="font-size:13px;margin:16px 0 6px">用户组与角色</h3>
      <div class="detail-actions">
        <select id="md-group">${(state.groups || []).map((g) => `<option value="${esc(g.id)}" ${g.id === m.group_id ? 'selected' : ''}>${esc(g.name)}</option>`).join('')}</select>
        <button class="btn sm" id="md-group-save">切换用户组</button>
        <label><input type="checkbox" ${hasAdmin ? 'checked' : ''} onchange="memberToggleRole('${q(id)}','admin')"> 管理员（可登录面板）</label>
        <label><input type="checkbox" ${hasUploader ? 'checked' : ''} onchange="memberToggleRole('${q(id)}','uploader')"> 上片员</label>
      </div>
      ${trafficBar(m.traffic_used_bytes, m.traffic_quota_bytes)}
      <h3 style="font-size:13px;margin:16px 0 6px">权限覆盖</h3>
      ${overrideEditor(m, libs)}
      <h3 style="font-size:13px;margin:16px 0 6px">近 30 天用量</h3>
      <div class="stat-grid">
        ${stat('⇅', fmtBytes(usage.bytes || 0), '流量', '30 天合计')}
        ${stat('▶', (usage.hours || 0) + ' 小时', '时长', '30 天合计')}
        ${stat('▣', usage.plays || 0, '播放', '30 天合计')}
      </div>
      ${sparkline(series)}
      <h3 style="font-size:13px;margin:16px 0 6px">设备</h3>
      ${devices.length ? `<table><thead><tr><th>设备</th><th>客户端</th><th>最近</th><th></th></tr></thead><tbody>
        ${devices.map((dev) => `<tr>
          <td>${esc(dev.device_name || dev.device_id)}${dev.blocked ? ' <span class="tag bad">已封</span>' : ''}</td>
          <td>${esc(dev.client)}</td><td>${esc(fmtAgeTs(dev.last_seen_at))}</td>
          <td class="row-actions">
            ${dev.blocked
              ? `<button class="btn sm" onclick="unblockDevice('${q(id)}','${q(dev.device_id)}')">解封</button>`
              : `<button class="btn sm" onclick="blockDevice('${q(id)}','${q(dev.device_id)}',true)">拉黑</button>`}
            <button class="btn sm danger" onclick="forgetDevice('${q(id)}','${q(dev.device_id)}')">解绑</button>
          </td></tr>`).join('')}</tbody></table>` : '<div class="empty">暂无设备</div>'}
      <h3 style="font-size:13px;margin:16px 0 6px">最近播放</h3>
      ${plays.length ? `<table><thead><tr><th>内容</th><th>方式</th><th>时长</th></tr></thead><tbody>
        ${plays.slice(0, 20).map((p) => `<tr><td>${esc(p.series_name || p.item_name || '(未命名)')}</td>
          <td>${esc(p.play_method || '-')}</td><td>${esc(Math.round((p.seconds || 0) / 60))} 分</td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">暂无播放</div>'}
      <h3 style="font-size:13px;margin:16px 0 6px">审计时间线</h3>
      ${audit.length ? audit.slice(0, 20).map((a) => `<div class="list-row"><div>
        <div class="t">${esc(a.action)}</div><div class="s">${esc(a.detail)}</div></div>
        <span class="muted">${esc(fmtAgeTs(a.ts))}</span></div>`).join('') : '<div class="empty">暂无记录</div>'}`;
    if ($('#ov-save')) $('#ov-save').onclick = () => saveOverrides(id);
    if ($('#ov-clear')) $('#ov-clear').onclick = () => clearAllOverrides(id);
    if ($('#md-group-save')) {
      $('#md-group-save').onclick = async () => {
        try {
          await api(`/api/members/${encodeURIComponent(id)}`, {
            method: 'PUT', body: JSON.stringify({ group_id: $('#md-group').value }) });
          toast('已切换用户组'); memberDetail(id); renderPage('members');
        } catch (e) { toast('失败: ' + e.message, 1); }
      };
    }
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
    toast(blocked ? '已拉黑' : '已解封'); memberDetail(userId);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function unblockDevice(userId, deviceId) {
  userId = uq(userId); deviceId = uq(deviceId);
  try {
    await api(`/api/members/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}/unblock`, {
      method: 'POST' });
    toast('已解封'); memberDetail(userId);
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

/* ---------------- user groups ---------------- */
PAGES.groups = async () => {
  $('#view').innerHTML = pageLoading();
  const groups = await api('/api/groups');
  state.groups = groups;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('▣', groups.length, '用户组', '计费与限制模板')}
      ${stat('☺', groups.reduce((a, g) => a + (g.member_count || 0), 0), '覆盖用户', '已分组的成员')}
    </div>
    ${card('新建用户组', '组决定计费方式和默认限制；成员可在详情里逐项覆盖', `<div class="card-body">${groupForm('new', {})}
      <div class="toolbar"><button class="btn primary" id="group-create">创建</button></div></div>`)}
    ${tableCard('用户组', `${groups.length} 个`, ['名称', '计费', '默认额度', '限制', '用户', ''],
      groups.map((g) => `<tr>
        <td>${esc(g.name)}${g.is_default ? ' <span class="tag idle">默认</span>' : ''}<div class="s muted">${esc(g.description)}</div></td>
        <td>${esc(billingLabel(g.billing_mode))}</td>
        <td>${esc(groupQuotaText(g))}</td>
        <td>${esc(groupLimitsText(g))}</td>
        <td>${esc(g.member_count || 0)}</td>
        <td class="row-actions">
          <button class="btn sm" onclick="editGroup('${q(g.id)}')">编辑</button>
          <button class="btn sm danger" onclick="deleteGroup('${q(g.id)}',${g.member_count || 0})">删除</button>
        </td></tr>`).join(''))}`;
  $('#group-create').onclick = () => submitGroup('new');
};
function groupNeedsTraffic(m) { return m === 'traffic' || m === 'both'; }
function groupNeedsTime(m) { return m === 'time' || m === 'both'; }
function groupQuotaText(g) {
  const bits = [];
  if (groupNeedsTime(g.billing_mode)) bits.push(g.duration_days + ' 天');
  if (groupNeedsTraffic(g.billing_mode)) bits.push(fmtBytes(g.traffic_quota_bytes) + '/月');
  return bits.join(' · ') || '-';
}
function groupLimitsText(g) {
  const bits = [];
  bits.push(g.max_streams ? g.max_streams + ' 路' : '并发不限');
  bits.push(g.bandwidth_limit_kbps ? fmtKbps(g.bandwidth_limit_kbps) : '不限速');
  bits.push(g.max_devices ? g.max_devices + ' 设备' : '设备不限');
  bits.push(g.allow_transcode ? '转码' : '禁转码');
  bits.push(g.allow_download ? '下载' : '禁下载');
  return bits.join(' · ');
}
function groupForm(prefix, g) {
  const v = (k, d) => esc(g[k] != null ? g[k] : d);
  const gib = g.traffic_quota_bytes ? (g.traffic_quota_bytes / (1024 ** 3)).toFixed(0) : '1024';
  const mode = g.billing_mode || 'both';
  return `
    <div class="form-row"><label>ID</label><input id="${prefix}-id" value="${v('id', '')}" ${prefix === 'new' ? '' : 'disabled'} placeholder="standard"></div>
    <div class="form-row"><label>名称</label><input id="${prefix}-name" value="${v('name', '')}" placeholder="普通用户"></div>
    <div class="form-row"><label>描述</label><input id="${prefix}-description" value="${v('description', '')}"></div>
    <div class="form-row"><label>默认组</label><input id="${prefix}-default" type="checkbox" ${g.is_default ? 'checked' : ''}>
      <span class="muted">新纳入的账号进这个组</span></div>
    <div class="help">计费方式。时间=有到期日；流量=每月 1 日重置额度；两者可同时启用。</div>
    <div class="form-row"><label>计费</label>
      <select id="${prefix}-billing">
        ${[['both', '时间+流量'], ['traffic', '仅流量'], ['time', '仅时间'], ['none', '不计费']].map(([val, label]) =>
          `<option value="${val}" ${mode === val ? 'selected' : ''}>${label}</option>`).join('')}
      </select></div>
    <div class="form-row"><label>默认时长</label><input id="${prefix}-days" type="number" min="0" value="${v('duration_days', 30)}" style="width:100px"><span class="muted">天，计时间的组必填</span></div>
    <div class="form-row"><label>月流量</label><input id="${prefix}-gib" type="number" min="0" value="${esc(gib)}" style="width:110px"><span class="muted">GiB，计流量的组必填</span></div>
    <div class="help">默认限制。0 = 不限。成员详情里可以逐个覆盖。</div>
    <div class="form-row"><label>带宽限速</label>
      <div><div style="margin-bottom:4px">${bwPresetButtons(prefix + '-bandwidth')}</div>
      <input id="${prefix}-bandwidth" type="number" min="0" step="0.1" value="${esc(kbpsToMBps(g.bandwidth_limit_kbps || 0))}" style="width:110px">
      <span class="muted">MB/s，0 = 不限速。保存后该组未覆盖成员会重签限速。</span></div></div>
    <div class="form-row"><label>并发</label><input id="${prefix}-streams" type="number" min="0" value="${v('max_streams', 2)}" style="width:90px"><span class="muted">路，0 = 不限</span></div>
    <div class="form-row"><label>设备</label><input id="${prefix}-devices" type="number" min="0" value="${v('max_devices', 3)}" style="width:90px"><span class="muted">台，0 = 不限</span></div>
    <div class="form-row"><label>权限</label>
      <label><input id="${prefix}-transcode" type="checkbox" ${g.allow_transcode == null || g.allow_transcode ? 'checked' : ''}> 转码</label>
      <label><input id="${prefix}-download" type="checkbox" ${g.allow_download ? 'checked' : ''}> 下载</label></div>`;
}
function groupPayload(prefix) {
  const gib = parseFloat($(`#${prefix}-gib`).value) || 0;
  return {
    id: $(`#${prefix}-id`).value.trim(),
    name: $(`#${prefix}-name`).value.trim(),
    description: $(`#${prefix}-description`).value.trim(),
    is_default: $(`#${prefix}-default`).checked,
    billing_mode: $(`#${prefix}-billing`).value,
    duration_days: parseInt($(`#${prefix}-days`).value, 10) || 0,
    traffic_quota_bytes: Math.round(gib * 1024 ** 3),
    bandwidth_limit_kbps: mBpsToKbps(parseFloat($(`#${prefix}-bandwidth`).value) || 0),
    max_streams: parseInt($(`#${prefix}-streams`).value, 10) || 0,
    max_devices: parseInt($(`#${prefix}-devices`).value, 10) || 0,
    allow_transcode: $(`#${prefix}-transcode`).checked,
    allow_download: $(`#${prefix}-download`).checked,
  };
}
async function submitGroup(prefix, existingId) {
  const payload = groupPayload(prefix);
  try {
    if (existingId) {
      if (!confirm('保存后会立即更新该组未单独覆盖限速的成员，正在播放的人会重签限速。')) return;
      await api(`/api/groups/${encodeURIComponent(existingId)}`, { method: 'PUT', body: JSON.stringify(payload) });
      toast('已保存'); closeModal();
    } else {
      await api('/api/groups', { method: 'POST', body: JSON.stringify(payload) });
      toast('已创建');
    }
    renderPage('groups');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
function editGroup(id) {
  id = uq(id);
  const g = (state.groups || []).find((x) => x.id === id);
  if (!g) return;
  openModal('编辑用户组 ' + g.name, `${groupForm('edit', g)}
    <div class="toolbar">
      <button class="btn" id="group-preview">预览变更</button>
      <button class="btn primary" id="group-save">保存</button>
    </div>`, { wide: true });
  $('#group-save').onclick = () => submitGroup('edit', id);
  $('#group-preview').onclick = () => enforcementPreview();
}
async function deleteGroup(id, count) {
  id = uq(id);
  if (count) return toast(`仍有 ${count} 个用户在该组，请先迁移`, 1);
  if (!confirm(`删除用户组 ${id}？`)) return;
  try {
    await api(`/api/groups/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('已删除'); renderPage('groups');
  } catch (e) { toast('无法删除: ' + e.message, 1); }
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
  state.auditOffset = 0;
  state.auditLimit = 50;
  await loadAudit();
};
function auditQuery() {
  const qs = new URLSearchParams();
  qs.set('limit', String(state.auditLimit || 50));
  qs.set('offset', String(state.auditOffset || 0));
  const actor = (($('#au-actor') || {}).value || '').trim();
  const action = (($('#au-action') || {}).value || '').trim();
  const subject = (($('#au-subject') || {}).value || '').trim();
  if (actor) qs.set('actor', actor);
  if (action) qs.set('action', action);
  if (subject) qs.set('subject', subject);
  return qs;
}
async function loadAudit() {
  try {
    const data = await api('/api/audit?' + auditQuery().toString());
    const items = Array.isArray(data) ? data : (data.items || []);
    const total = Array.isArray(data) ? items.length : (data.total || 0);
    const limit = Array.isArray(data) ? items.length : (data.limit || 50);
    const offset = Array.isArray(data) ? 0 : (data.offset || 0);
    state.audit = data;
    const page = Math.floor(offset / Math.max(limit, 1)) + 1;
    const pages = Math.max(1, Math.ceil(total / Math.max(limit, 1)));
    $('#view').innerHTML = `
      <div class="filter-bar">
        <input id="au-actor" placeholder="操作者" style="width:120px" value="${esc((($('#au-actor') || {}).value || ''))}">
        <input id="au-action" placeholder="动作" style="width:140px" value="${esc((($('#au-action') || {}).value || ''))}">
        <input id="au-subject" placeholder="对象" style="width:140px" value="${esc((($('#au-subject') || {}).value || ''))}">
        <button class="btn" id="au-go">筛选</button>
      </div>
      <div class="pager">
        <span>共 ${esc(total)} 条 · 第 ${esc(page)}/${esc(pages)} 页</span>
        <button class="btn sm" id="au-prev" ${offset <= 0 ? 'disabled' : ''}>上一页</button>
        <button class="btn sm" id="au-next" ${offset + limit >= total ? 'disabled' : ''}>下一页</button>
      </div>
      ${tableCard('审计日志', `${items.length} 条`, ['时间', '操作者', '动作', '对象', '详情', '结果'],
        auditRows(items))}`;
    $('#au-go').onclick = () => { state.auditOffset = 0; loadAudit(); };
    $('#au-prev').onclick = () => {
      state.auditOffset = Math.max(0, offset - limit); loadAudit();
    };
    $('#au-next').onclick = () => {
      state.auditOffset = offset + limit; loadAudit();
    };
  } catch (e) {
    $('#view').innerHTML = pageError(e);
    if ($('#retry-page')) $('#retry-page').onclick = () => loadAudit();
  }
}
function auditRows(rows) {
  return rows.map((a) => `<tr>
    <td>${esc(fmtAgeTs(a.ts))}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td>
    <td>${esc(a.subject)}</td><td>${esc(a.detail)}</td>
    <td>${a.ok ? '<span class="tag ok">成功</span>' : '<span class="tag bad">失败</span>'}</td>
  </tr>`).join('');
}

if (typeof bootPanel === 'function') bootPanel();

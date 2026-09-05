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
/* ---------- members ----------
   A product back-office rather than an ops table: the stats answer "how is the
   member base doing", the filters answer "who am I looking for", and the row
   shows provenance (which door they came in by, who vouched for them) because
   that is what the invite tree is for. */
const MEMBER_COLS = ['', 'Emby 账号', '套餐', '有效期', '注册渠道', '邀请人',
  '下级', 'TG', '积分', '本月观看',
  '直链 7 天', '直链 30 天', '直链累计', '最近活跃', ''];

/* Sortable columns. Sorting happens client-side over the page already
   fetched: the list is capped at a few hundred rows, and a round trip per
   header click would make an interaction that should feel instant depend on
   the network. */
const MEMBER_SORTS = {
  edge7: (m) => Number((m.edge || {}).bytes_7d || 0),
  edge30: (m) => Number((m.edge || {}).bytes_30d || 0),
  edgeTotal: (m) => Number((m.edge || {}).bytes_total || 0),
  lastActive: (m) => Number(m.last_activity_ts || 0),
};

PAGES.members = async () => {
  $('#view').innerHTML = pageLoading();
  /* Last-activity rides along but must never block the page: it is one extra
     call to Emby, and a member list that fails because a decorative column
     could not load is worse than one missing the column. */
  const [listing, groups, activity] = await Promise.all([
    api('/api/members'), api('/api/groups'),
    api('/api/members-activity').catch(() => ({ activity: {} })),
  ]);
  state.memberListing = listing;
  state.groups = groups;
  const members = listing.members || [];
  const activityMap = (activity && activity.activity) || {};
  for (const m of members) {
    const stamp = activityMap[m.emby_user_id];
    const parsed = stamp ? Date.parse(String(stamp).replace(/(\.\d{3})\d+/, '$1')) : NaN;
    m.last_activity_ts = Number.isNaN(parsed) ? 0 : parsed / 1000;
  }
  if (state.memberSort && MEMBER_SORTS[state.memberSort.key]) {
    const pick = MEMBER_SORTS[state.memberSort.key];
    const dir = state.memberSort.desc ? -1 : 1;
    members.sort((a, b) => (pick(a) - pick(b)) * dir);
  }
  state.watchPeak = members.reduce((max, m) => Math.max(max, Number(m.watch_hours || 0)), 0);
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const dayStart = midnight.getTime() / 1000;
  const ok = members.filter((m) => m.state === 'active').length;
  const off = members.filter((m) => m.state !== 'active').length;
  // Legacy accounts have no register_at, so they can never count as "new
  // today" -- dating them to the import would invent a signup that never was.
  const fresh = members.filter((m) => m.register_at && m.register_at >= dayStart).length;
  const unmanagedN = (listing.unmanaged || []).filter((u) => !u.is_admin).length;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('☺', members.length, '总用户', listing.truncated ? `列表被截断（limit ${listing.limit}）` : '已纳入管理')}
      ${stat('✓', ok, '已启用', '可正常播放')}
      ${stat('⊘', off, '停用中', '含过期与超额')}
      ${stat('✦', fresh, '今日新增', '按注册时间')}
    </div>
    <div class="filter-bar">
      <input id="mf-q" placeholder="搜索用户名 / 备注" style="min-width:170px">
      <select id="mf-state">
        <option value="">全部状态</option>
        <option value="active">正常</option><option value="expired">已过期</option>
        <option value="exhausted">已超额</option><option value="suspended">已停用</option>
        <option value="pending">待开通</option>
      </select>
      <select id="mf-group"><option value="">全部套餐</option>${groups.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('')}</select>
      <select id="mf-via">
        <option value="">全部渠道</option>
        <option value="admin">管理员</option><option value="invite">邀请</option>
        <option value="redeem">卡密</option><option value="legacy">历史导入</option>
      </select>
      <select id="mf-tg">
        <option value="">TG 不限</option>
        <option value="yes">已绑定</option><option value="no">未绑定</option>
      </select>
      <select id="mf-exp">
        <option value="">到期不限</option>
        <option value="soon">7 天内</option><option value="gone">已过期</option>
      </select>
      <input id="mf-inviter" placeholder="邀请人" style="width:120px">
      <button class="btn primary" id="mf-apply">筛选</button>
      <button class="btn" id="mf-reset">重置</button>
      <button class="btn" id="mf-preview">预览变更</button>
      <button class="btn danger" id="mf-apply-enf">应用策略</button>
    </div>
    <div class="filter-bar" id="bulk-bar" style="display:none">
      <span id="bulk-count" class="muted">已选 0 人</span>
      <button class="btn primary" onclick="bulkRenew()">批量续期</button>
      <button class="btn" onclick="bulkAction('activate')">批量启用</button>
      <button class="btn" onclick="bulkAction('suspend')">批量停用</button>
      <button class="btn" onclick="bulkAction('reset-traffic')">重置流量</button>
      <button class="btn" onclick="toggleAllMembers(false)">取消选择</button>
    </div>
    ${listing.truncated ? '<div class="help">后端返回已达上限，计数可能不完整。筛选只作用于当前这一页。</div>' : ''}
    ${tableCard('用户', `${members.length} 人`, MEMBER_COLS,
      members.map((m) => memberRow(m)).join(''))}
    ${card('纳入现有 Emby 账号', '未纳入的账号不受任何限制，也不计流量',
      `<div class="card-body">
        ${unmanagedN ? `<div class="toolbar"><button class="btn primary" id="mf-enroll-all">一键纳入默认组（${unmanagedN} 个）</button></div>` : ''}
        ${enrolmentTable(listing.unmanaged || [], groups)}</div>`)}`;
  const memberCard = [...document.querySelectorAll('#view .card')].find((c) => {
    const h = c.querySelector('h3');
    return h && h.textContent === '用户';
  });
  if (memberCard) {
    const table = memberCard.querySelector('table');
    if (table) { table.id = 'member-table'; table.classList.add('member-table'); }
  }
  const headCell = document.querySelector('#member-table thead th');
  if (headCell) {
    headCell.innerHTML = '<input type="checkbox" id="m-pick-all">';
    $('#m-pick-all').onchange = (e) => toggleAllMembers(e.target.checked);
  }
  bindMemberSorting();
  $('#mf-apply').onclick = filterMembers;
  $('#mf-reset').onclick = resetMemberFilters;
  ['mf-q', 'mf-inviter'].forEach((id) => {
    if ($('#' + id)) $('#' + id).onkeydown = (e) => { if (e.key === 'Enter') filterMembers(); };
  });
  $('#mf-preview').onclick = () => enforcementPreview();
  $('#mf-apply-enf').onclick = () => enforcementApply();
  if ($('#mf-enroll-all')) $('#mf-enroll-all').onclick = enrollAllDefaults;
  if (state.memberFilter) applyStoredMemberFilter();
};
/* Header clicks toggle the sort and re-render. The chosen sort lives in
   state, like the filters, so it survives the re-render it triggers. */
function bindMemberSorting() {
  const heads = document.querySelectorAll('#member-table thead th');
  const order = [null, null, null, null, null, null, null, null, null, null,
    'edge7', 'edge30', 'edgeTotal', 'lastActive', null];
  heads.forEach((cell, index) => {
    const key = order[index];
    if (!key) return;
    cell.classList.add('sortable');
    const active = state.memberSort && state.memberSort.key === key;
    if (active) cell.textContent += state.memberSort.desc ? ' ↓' : ' ↑';
    cell.onclick = () => {
      const wasDesc = active && state.memberSort.desc;
      // First click on a byte column sorts descending: "who used the most"
      // is the question being asked, and ascending would open on zeros.
      state.memberSort = { key, desc: !active ? true : !wasDesc };
      renderPage('members');
    };
  });
}

/* Jumping from an inviter cell to "everyone they brought in" has to survive
   the page re-render, so the intent is stored rather than applied to DOM that
   is about to be replaced. */
function filterByInviter(name) {
  state.memberFilter = { inviter: uq(name) };
  renderPage('members', true);
}
function applyStoredMemberFilter() {
  const wanted = state.memberFilter;
  state.memberFilter = null;
  if (!wanted || !$('#mf-inviter')) return;
  $('#mf-inviter').value = wanted.inviter || '';
  filterMembers();
}
function resetMemberFilters() {
  ['mf-q', 'mf-inviter'].forEach((id) => { if ($('#' + id)) $('#' + id).value = ''; });
  ['mf-state', 'mf-group', 'mf-via', 'mf-tg', 'mf-exp'].forEach((id) => {
    if ($('#' + id)) $('#' + id).value = '';
  });
  filterMembers();
}
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
/* Telegram column: a linked chat is what makes expiry reminders possible, so
   the list has to show at a glance who can actually be reached. */
function tgCell(m) {
  const id = q(m.emby_user_id);
  if (m.tg_user_id) {
    const who = m.tg_username ? `@${m.tg_username}` : '已关联';
    return `<span class="tag ok" title="${esc(who)}">${esc(who)}</span>
      <button class="btn sm" onclick="memberTgUnbind('${id}')">解绑</button>`;
  }
  // Accounts link themselves: a member registers in the bot, or claims an old
  // account and an operator approves it. There is nothing to hand out here.
  return '<span class="muted">未关联</span>';
}
/* Which door a member came in by. 'legacy' is shown as 历史导入 rather than
   hidden: several hundred accounts predate the bot, and labelling them as a
   channel they never used would make this column a lie. */
const CHANNEL_LABELS = {
  admin: ['admin', '管理员'], invite: ['invite', '邀请'],
  redeem: ['redeem', '卡密'], legacy: ['legacy', '历史导入'],
};
function channelTag(via) {
  const [cls, label] = CHANNEL_LABELS[via] || CHANNEL_LABELS.legacy;
  return `<span class="tag chan ${cls}">${esc(label)}</span>`;
}
function inviterCell(m) {
  if (!m.inviter_id) return '<span class="muted">—</span>';
  const name = m.inviter_name || '(已删除)';
  if (m.inviter_name === '(已删除)' || !m.inviter_name) {
    return `<span class="muted" title="邀请人账号已删除">${esc(name)}</span>`;
  }
  return `<button class="link-btn" title="只看这个人邀请的成员"
    onclick="filterByInviter('${q(name)}')">${esc(name)}</button>`;
}
/* Hours watched this month, with a bar scaled against the busiest member on
   the page: an absolute scale would leave every bar invisible on a library
   where nobody watches 100 hours. */
function watchCell(m) {
  const hours = Number(m.watch_hours || 0);
  if (!hours) return '<span class="muted">—</span>';
  const peak = Number(state.watchPeak || 0) || hours;
  const pct = Math.max(4, Math.min(100, Math.round(hours / peak * 100)));
  return `<div class="watch-cell"><div class="n">${esc(hours.toFixed(1))} 小时</div>
    <span class="bar"><i style="width:${pct}%"></i></span></div>`;
}
function memberRow(m) {
  const id = q(m.emby_user_id);
  const suspended = m.state === 'suspended';
  const tog = suspended ? 'active' : 'suspended';
  const kids = Number(m.invitee_count || 0);
  return `<tr>
    <td><input type="checkbox" class="m-pick" value="${id}" onchange="syncBulkBar()"></td>
    <td><div class="u-name">${esc(m.username)}${roleTags(m)}</div>
      <div class="u-sub">${stateTag(m.state)}</div></td>
    <td>${esc(m.group_name)}</td>
    <td>${daysLeftHtml(m)}</td>
    <td>${channelTag(m.register_via)}</td>
    <td>${inviterCell(m)}</td>
    <td>${kids ? `<button class="link-btn" onclick="filterByInviter('${q(m.username)}')">${kids}</button>`
    : '<span class="muted">0</span>'}</td>
    <td>${tgCell(m)}</td>
    <td>${pointsCell(m)}</td>
    <td>${watchCell(m)}</td>
    <td>${edgeCell(m, 'bytes_7d')}</td>
    <td>${edgeCell(m, 'bytes_30d')}</td>
    <td>${edgeCell(m, 'bytes_total')}</td>
    <td>${lastActiveCell(m)}</td>
    <td class="icon-actions">
      <button class="btn sm" title="详情" onclick="memberDetail('${id}')">👁</button>
      <button class="btn sm" title="续期" onclick="memberRenew('${id}')">⏳</button>
      <button class="btn sm" title="${suspended ? '启用' : '停用'}"
        onclick="memberStatus('${id}','${tog}')">${suspended ? '✅' : '⛔'}</button>
      <button class="btn sm danger" title="删除"
        onclick="memberDelete('${id}','${q(m.username)}')">🗑</button>
    </td></tr>`;
}

/* Measured direct-link bytes. Zero is muted rather than bold: on a server
   whose ledger has only just started every row reads zero, and shouting it
   would make the column look broken rather than new. */
/* Per-node and per-day measured bytes. Shown as its own section rather than
   folded into the legacy bar above, because the two numbers are not the same
   quantity: one is sampled from playback the media server saw, the other is
   bytes the edge actually sent. Presenting them as one figure is what made
   the old number look authoritative. */
function edgeDetailSection(edge) {
  const byNode = (edge && edge.by_node) || [];
  const byDay = (edge && edge.by_day) || [];
  if (!byNode.length && !byDay.length) {
    return `<h3 style="font-size:13px;margin:16px 0 6px">直链流量（实测）</h3>
      <div class="help muted">该成员在统计窗口内没有直链流量记录。</div>`;
  }
  const total = byNode.reduce((sum, r) => sum + Number(r.bytes || 0), 0);
  const nodeRows = byNode.map((r) => `<tr><td>${esc(r.node)}</td>
    <td>${fmtBytes(r.bytes)}</td><td>${esc(r.requests)}</td></tr>`).join('');
  const recent = byDay.slice(-14).reverse();
  const dayRows = recent.map((r) => `<tr><td>${esc(r.day)}</td>
    <td>${fmtBytes(r.bytes)}</td><td>${esc(r.requests)}</td></tr>`).join('');
  return `<h3 style="font-size:13px;margin:16px 0 6px">直链流量（实测 · 近 ${esc((edge && edge.days) || 30)} 天）</h3>
    <div class="help">合计 <b>${fmtBytes(total)}</b>，来自各节点 nginx 实际发送字节。</div>
    <table><thead><tr><th>节点</th><th>流量</th><th>请求数</th></tr></thead>
      <tbody>${nodeRows || '<tr><td colspan="3" class="muted">无</td></tr>'}</tbody></table>
    <h3 style="font-size:13px;margin:16px 0 6px">按日明细</h3>
    <table><thead><tr><th>日期</th><th>流量</th><th>请求数</th></tr></thead>
      <tbody>${dayRows || '<tr><td colspan="3" class="muted">无</td></tr>'}</tbody></table>`;
}

function edgeCell(m, key) {
  const n = Number((m.edge || {})[key] || 0);
  return n ? fmtBytes(n) : '<span class="muted">0</span>';
}

/* Last activity comes from Emby, which tracks it per account. Inferring
   "inactive" from a traffic total is precisely the mistake that made an
   unmeasured account indistinguishable from an idle one. */
function lastActiveCell(m) {
  const ts = Number(m.last_activity_ts || 0);
  if (!ts) return '<span class="muted">—</span>';
  const age = Date.now() / 1000 - ts;
  const cls = age > 90 * 86400 ? 'danger-text' : '';
  return `<span class="${cls}">${esc(fmtAge(age))}前</span>`;
}

/* A zero balance is muted rather than bold: on a server that has just
   switched points on every row reads zero, and shouting it would make the
   column look broken. */
function pointsCell(m) {
  const n = Number(m.points || 0);
  return n ? `<b>${esc(n)}</b>` : '<span class="muted">0</span>';
}

/* ---------- bulk selection ----------
   The bar only appears once something is ticked: a row of destructive-looking
   buttons above an empty selection invites a click that cannot do anything. */
function pickedIds() {
  return [...document.querySelectorAll('.m-pick:checked')].map((el) => el.value);
}
function syncBulkBar() {
  const n = pickedIds().length;
  const bar = $('#bulk-bar');
  if (!bar) return;
  bar.style.display = n ? 'flex' : 'none';
  const label = $('#bulk-count');
  if (label) label.textContent = `已选 ${n} 人`;
  const all = document.querySelectorAll('.m-pick').length;
  const head = $('#m-pick-all');
  if (head) {
    head.checked = n > 0 && n === all;
    head.indeterminate = n > 0 && n < all;
  }
}
function toggleAllMembers(on) {
  document.querySelectorAll('.m-pick').forEach((el) => { el.checked = on; });
  syncBulkBar();
}
async function bulkAction(action, extra) {
  const ids = pickedIds();
  if (!ids.length) return;
  try {
    const r = await api('/api/members/bulk', {
      method: 'POST',
      body: JSON.stringify({ action, user_ids: ids, ...(extra || {}) }),
    });
    // Partial success is normal, and saying only "done" would hide it.
    if (r.failed && r.failed.length) {
      toast(`成功 ${r.ok} 人，失败 ${r.failed.length} 人：${esc(r.failed[0].error)}`, 1);
    } else {
      toast(`已处理 ${r.ok} 人`);
    }
    renderPage('members', true);
  } catch (e) { toast('批量操作失败: ' + e.message, 1); }
}
async function bulkRenew() {
  const days = prompt('批量续期天数', '30');
  if (!days) return;
  const n = Number(days);
  if (!Number.isFinite(n) || n <= 0) { toast('天数必须大于 0', 1); return; }
  await bulkAction('renew', { days: n });
}
async function memberTgUnbind(id) {
  if (!confirm('解绑后该成员将不再收到到期提醒，确认？')) return;
  try {
    await api(`/api/members/${encodeURIComponent(id)}/telegram/unbind`,
      { method: 'POST' });
    toast('已解绑');
    renderPage('members', true);
  } catch (e) { toast('解绑失败: ' + e.message, 1); }
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
  const st = (($('#mf-state') || {}).value || '');
  const via = (($('#mf-via') || {}).value || '');
  const tg = (($('#mf-tg') || {}).value || '');
  const exp = (($('#mf-exp') || {}).value || '');
  const inviter = (($('#mf-inviter') || {}).value || '').trim().toLowerCase();
  const now = Date.now() / 1000;
  const rows = (listing.members || []).filter((m) => {
    if (grp && m.group_id !== grp) return false;
    if (st && m.state !== st) return false;
    if (via && (m.register_via || 'legacy') !== via) return false;
    if (tg === 'yes' && !m.tg_user_id) return false;
    if (tg === 'no' && m.tg_user_id) return false;
    if (exp === 'soon' && !(m.expires_at && m.expires_at > now
      && m.expires_at - now <= 7 * 86400)) return false;
    if (exp === 'gone' && !(m.expires_at && m.expires_at <= now)) return false;
    if (inviter && !String(m.inviter_name || '').toLowerCase().includes(inviter)) return false;
    if (qv && !(`${m.username} ${m.note || ''} ${m.contact || ''}`).toLowerCase().includes(qv)) return false;
    return true;
  });
  const tbody = document.querySelector('#member-table tbody');
  if (tbody) tbody.innerHTML = rows.map(memberRow).join('') || '<tr><td colspan="10" class="empty">没有匹配的成员</td></tr>';
  // Filtering re-renders the rows, so any previous ticks are gone with them.
  syncBulkBar();
}
/* Creating an Emby account by hand was removed in v0.19 (owner decision):
   accounts arrive through the bot's three registration channels, and a
   panel-made account has no inviter, no channel and no chat behind it. The
   enrolment card below still adopts accounts that already exist in Emby. */
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
/* Delete means delete: the panel record and the Emby account, plus whoever
   vouched for them. The preview is fetched first and named in the prompt --
   an operator told "this removes 1 account" who loses 2 will never trust the
   dialog again, and the second account is one they never clicked on. */
async function memberDelete(id, username) {
  id = uq(id); username = uq(username);
  let preview = null;
  try {
    preview = await api(`/api/members/${encodeURIComponent(id)}/delete-preview`);
  } catch (e) { toast('无法读取删除影响: ' + e.message, 1); return; }

  const also = (preview.cascade || []).map((c) => `· ${c.username}（${c.reason}）`);
  const lines = [
    `确认删除「${username}」？`, '',
    '将同时删除：',
    `· ${username}（本人）`,
    ...also,
    '',
    'Emby 账号与面板记录都会被删除，且不可恢复。',
  ];
  if (!confirm(lines.join('\n'))) return;
  try {
    const r = await api(`/api/members/${encodeURIComponent(id)}`, { method: 'DELETE' });
    const n = (r.removed || []).length;
    toast(n > 1 ? `已删除 ${n} 个账号（含邀请人）` : '已删除');
    renderPage('members', true);
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
function memberRequests(d) {
  const rows = d.requests || [];
  const left = d.request_remaining == null ? '不限' : d.request_remaining + ' 次';
  if (!rows.length) {
    return `<div class="muted">还没有求过片。本月剩余 ${esc(left)}。</div>`;
  }
  return `<div class="muted" style="margin-bottom:6px">本月剩余 ${esc(left)}</div>
    <table><thead><tr><th>编号</th><th>片名</th><th>状态</th><th>接单人</th><th>时间</th></tr></thead><tbody>
    ${rows.map((r) => `<tr>
      <td>#${esc(r.id)}</td>
      <td>${esc(r.display_title)}${r.result_note
    ? `<div class="u-sub muted">${esc(r.result_note)}</div>` : ''}</td>
      <td>${requestStatusTag(r)}</td>
      <td>${r.claimed_by_name ? esc(r.claimed_by_name) : '<span class="muted">—</span>'}</td>
      <td class="muted">${esc(fmtAgeTs(r.created_at))}</td>
    </tr>`).join('')}</tbody></table>`;
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
      <div class="help">到期 ${esc(fmtExpiry(m.expires_at_effective || m.expires_at))}
        · <span title="按播放会话采样估算，不含直链流量">旧口径流量（不含直链）</span>
        ${fmtBytes(m.traffic_used_bytes)} / ${fmtQuota(m.traffic_quota_bytes)}</div>
      <div class="help">注册渠道 ${channelTag(m.register_via)}
        · 邀请人 ${m.inviter_name ? esc(m.inviter_name) : '<span class="muted">—</span>'}
        · 下级 ${esc(m.invitee_count || 0)} 人
        · 邀请名额 ${esc(m.invite_quota || 0)}</div>
      <div class="detail-actions">
        <button class="btn sm" onclick="memberKick('${q(id)}')">踢下线</button>
        <button class="btn sm" onclick="memberPassword('${q(id)}')">重置密码</button>
        <button class="btn sm" onclick="memberStatus('${q(id)}','${tog}')">${togLabel}</button>
        <button class="btn sm" onclick="memberRenew('${q(id)}')">续期</button>
        <button class="btn sm" onclick="memberReset('${q(id)}')">重置流量</button>
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
      ${edgeDetailSection(d.edge)}
      <h3 style="font-size:13px;margin:16px 0 6px">求片记录</h3>
      ${memberRequests(d)}
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
      <h3 style="font-size:13px;margin:16px 0 6px">积分</h3>
      ${pointsBlock(id, d)}
      <h3 style="font-size:13px;margin:16px 0 6px">审计时间线</h3>
      ${audit.length ? audit.slice(0, 20).map((a) => `<div class="list-row"><div>
        <div class="t">${esc(a.action)}</div><div class="s">${esc(a.detail)}</div></div>
        <span class="muted">${esc(fmtAgeTs(a.ts))}</span></div>`).join('') : '<div class="empty">暂无记录</div>'}`;
    if ($('#pt-apply')) $('#pt-apply').onclick = () => adjustPoints(id);
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
/* Balance, recent rows, and the way to change it -- together, because an
   operator adjusting points needs to see what they are adjusting from. The
   reason box is not optional in spirit: an unexplained adjustment is the one
   nobody can answer questions about later. */
function pointsBlock(id, d) {
  const rows = d.points_ledger || [];
  return `<div class="help">当前余额 <b>${esc(d.points || 0)}</b></div>
    <div class="detail-actions">
      <input id="pt-delta" type="number" placeholder="增减，如 100 或 -50" style="width:170px">
      <input id="pt-reason" placeholder="原因（会记入审计）" style="min-width:180px">
      <button class="btn sm primary" id="pt-apply">调整积分</button>
    </div>
    ${rows.length ? `<table><thead><tr><th>时间</th><th>变动</th><th>原因</th></tr></thead><tbody>
      ${rows.slice(0, 10).map((r) => `<tr><td>${esc(fmtAgeTs(r.created_at))}</td>
        <td>${Number(r.delta) > 0 ? '+' : ''}${esc(r.delta)}</td>
        <td>${esc(r.reason_label || r.reason)}${r.ref ? ' · ' + esc(r.ref) : ''}</td></tr>`).join('')}
    </tbody></table>` : '<div class="empty">暂无积分记录</div>'}`;
}
async function adjustPoints(id) {
  id = uq(id);
  const delta = Number(($('#pt-delta') || {}).value || 0);
  if (!delta) { toast('请输入非零的增减数量', 1); return; }
  try {
    await api(`/api/points/${encodeURIComponent(id)}/adjust`, {
      method: 'POST',
      body: JSON.stringify({ delta, reason: ($('#pt-reason') || {}).value || '' }),
    });
    toast('已调整积分'); memberDetail(id); renderPage('members');
  } catch (e) { toast('失败: ' + e.message, 1); }
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
  bits.push(g.request_quota ? '求片 ' + g.request_quota + '/月' : '求片不限');
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
    <div class="form-row"><label>每月求片</label><input id="${prefix}-requests" type="number" min="0" value="${v('request_quota', 3)}" style="width:90px"><span class="muted">次/月，0 = 不限；被拒绝的求片也算一次</span></div>
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
    request_quota: parseInt($(`#${prefix}-requests`).value, 10) || 0,
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

/* ---------- Telegram ----------
   Grouped as their own nav section: the bot is a second front door with its
   own settings, its own approval queue and its own audit, and scattering those
   across "settings" and "members" made each of them hard to find. */

PAGES.tgbot = async () => {
  $('#view').innerHTML = pageLoading();
  const tg = await api('/api/settings/telegram').catch(() => null);
  if (!tg) { $('#view').innerHTML = pageError('无法读取 Telegram 配置'); return; }
  const st = tg.status || {};
  const running = st.running && tg.enabled;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('✈', running ? '运行中' : (tg.bot_token_set ? '已停止' : '未配置'), '机器人',
        st.last_error ? `最近错误：${st.last_error}` : (tg.bot_token_set ? tg.bot_token_masked : '尚未填写 Token'))}
      ${stat('🆕', tg.registration_open ? '开放' : '关闭', '注册',
        [tg.allow_admin_grant ? '预授权' : '', tg.allow_invite ? '邀请码' : '',
          tg.allow_redeem ? '卡密' : ''].filter(Boolean).join(' / ') || '全部通道已关闭')}
      ${stat('☺', tg.max_users ? `上限 ${tg.max_users}` : '不限', '名额',
        tg.require_group ? `需加入 ${tg.require_group}` : '无群组要求')}
      ${stat('⚡', '任务中心', '定时推送', '排行与到期提醒已迁至自动化')}
    </div>
    ${card('机器人对接', 'Token 仅保存在服务端，不会回传浏览器',
      `<div class="card-body">
        <div class="form-row"><label>Bot Token</label>
          <input id="tg-token" type="password" autocomplete="new-password"
            placeholder="${tg.bot_token_set ? esc(tg.bot_token_masked) + '（留空则不修改）' : '向 @BotFather 申请后粘贴'}"></div>
        <div class="form-row"><label>启用机器人</label>
          <input id="tg-enabled" type="checkbox" ${tg.enabled ? 'checked' : ''}>
          <span class="muted">关闭后停止收发消息，配置保留</span></div>
        <div class="form-row"><label>Emby 地址</label>
          <input id="tg-embyurl" value="${esc(tg.emby_public_url || '')}" placeholder="https://emby.example.com">
          <span class="muted">随账号一起发给新成员</span></div>
        <div class="toolbar">
          <button class="btn" id="tg-test">测试连接</button>
          <button class="btn primary" id="tg-save">保存</button>
          <span id="tg-result" class="muted"></span>
        </div>
      </div>`)}
    ${card('注册开户', '注册需要凭证：预授权、邀请码或卡密，三选一',
      `<div class="card-body">
        <div class="form-row"><label>管理员预授权</label>
          <input id="tg-ch-admin" type="checkbox" ${tg.allow_admin_grant ? 'checked' : ''}>
          <span class="muted">名单在「邀请与授权」页维护</span></div>
        <div class="form-row"><label>邀请码</label>
          <input id="tg-ch-invite" type="checkbox" ${tg.allow_invite ? 'checked' : ''}>
          <span class="muted">老用户用自己的名额生成</span></div>
        <div class="form-row"><label>卡密</label>
          <input id="tg-ch-redeem" type="checkbox" ${tg.allow_redeem ? 'checked' : ''}>
          <span class="muted">在「卡密管理」页批量生成</span></div>
        <div class="help">三个通道全部关闭 = 停止注册。卡密自带套餐和天数，下面的赠送天数只对预授权和邀请码生效。</div>
        <div class="form-row"><label>赠送天数</label>
          <input id="tg-regdays" type="number" min="0" max="3650" value="${esc(tg.register_days)}" style="width:100px">
          <span class="muted">0 = 不限期</span></div>
        <div class="form-row"><label>注册名额</label>
          <input id="tg-max" type="number" min="0" value="${esc(tg.max_users)}" style="width:100px">
          <span class="muted">0 = 不限；名额是防止链接外泄后被刷爆的唯一闸门</span></div>
        <div class="form-row"><label>默认用户组</label>
          <input id="tg-group" value="${esc(tg.default_group_id || '')}" placeholder="留空使用系统默认组"></div>
        <div class="form-row"><label>要求群组</label>
          <input id="tg-reqgroup" value="${esc(tg.require_group || '')}" placeholder="@yourgroup 或 -100xxxxxxxxx">
          <span class="muted">留空则不限制</span></div>
        <div class="toolbar"><button class="btn primary" id="tg-save2">保存</button></div>
      </div>`)}
    ${card('通知与排行', '这两件事现在是定时任务',
      `<div class="card-body">
        <div class="muted" style="line-height:1.6">
          <b>到期提醒</b>与<b>排行推送</b>已迁至「自动化 → 任务中心」，
          在那里配置目标群组、时间和开关。
          放在两个页面各有一个开关，先后保存会互相覆盖，也会让同一条消息发两遍。
        </div>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn" onclick="go('automation')">前往任务中心</button>
          <button class="btn" id="tg-sendrank">立即发送一次排行</button>
          <span id="tg-rankresult" class="muted"></span>
        </div>
      </div>`)}`;
  ['tg-save', 'tg-save2'].forEach((id) => {
    if ($('#' + id)) $('#' + id).onclick = saveTelegramPage;
  });
  $('#tg-test').onclick = testTelegramPage;
  $('#tg-sendrank').onclick = sendRankingsNow;
};

/* An empty token box means "keep the stored one", never "clear it": the
   sentinel is what tells the server which of the two was meant. */
function telegramPagePayload() {
  const typed = ($('#tg-token') || {}).value || '';
  const num = (id, dflt) => {
    const el = $('#' + id);
    return el ? Number(el.value || dflt) : dflt;
  };
  const str = (id) => (($('#' + id) || {}).value || '').trim();
  const flag = (id) => (($('#' + id) || {}).checked) || false;
  return {
    bot_token: typed.trim() || SECRET_KEEP,
    enabled: flag('tg-enabled'),
    emby_public_url: str('tg-embyurl'),
    allow_admin_grant: flag('tg-ch-admin'),
    allow_invite: flag('tg-ch-invite'),
    allow_redeem: flag('tg-ch-redeem'),
    register_days: num('tg-regdays', 30),
    max_users: num('tg-max', 0),
    default_group_id: str('tg-group'),
    require_group: str('tg-reqgroup'),
  };
}
async function saveTelegramPage() {
  try {
    await api('/api/settings/telegram', {
      method: 'POST', body: JSON.stringify(telegramPagePayload()) });
    toast('已保存');
    renderPage('tgbot', true);
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function testTelegramPage() {
  const el = $('#tg-result');
  el.textContent = '正在测试…';
  try {
    // Save first: verification asks Telegram who the bot is, which needs the
    // credential already stored rather than sent along for inspection.
    const payload = telegramPagePayload();
    await api('/api/settings/telegram', {
      method: 'POST', body: JSON.stringify(payload) });
    const r = await api('/api/settings/telegram/verify', { method: 'POST' });
    if (!r.ok) {
      el.innerHTML = `<span class="tag bad">连接失败</span> ${esc(r.error || '')}`;
      return;
    }
    el.innerHTML = `<span class="tag ok">连接成功</span> @${esc(r.username || '')}`;
    // A successful test with the switch still off is the trap: the operator
    // reads "连接成功" as "the bot is running" and walks away, while nothing
    // is polling. Verification proves the credential works, so switch it on
    // and say so rather than leaving a working bot stopped.
    if (!payload.enabled) {
      await api('/api/settings/telegram', {
        method: 'POST', body: JSON.stringify({ ...payload, enabled: true }) });
      toast('已自动启用机器人');
      renderPage('tgbot', true);
    }
  } catch (e) {
    el.innerHTML = `<span class="tag bad">连接失败</span> ${esc(e.message)}`;
  }
}
async function sendRankingsNow() {
  const el = $('#tg-rankresult');
  el.textContent = '发送中…';
  try {
    const r = await api('/api/telegram/rankings/send', {
      method: 'POST', body: JSON.stringify({ days: 1 }) });
    el.innerHTML = r.sent
      ? '<span class="tag ok">已发送</span>'
      : '<span class="tag bad">发送失败</span>';
  } catch (e) { el.innerHTML = `<span class="tag bad">${esc(e.message)}</span>`; }
}

/* ---------- redeem codes ----------
   A card is a bearer credential: whoever reads it can spend it. The table
   masks them and reveals one on demand, while the generation result and the
   CSV export show them in full -- those are the operator handing cards out,
   which is the entire point of minting them. */
PAGES.redeem = async () => {
  $('#view').innerHTML = pageLoading();
  const [listing, groups] = await Promise.all([
    api('/api/redeem'), api('/api/groups')]);
  state.redeemListing = listing;
  const codes = listing.codes || [];
  const st = listing.stats || {};
  const statusLabel = { unused: ['ok', '未使用'], used: ['idle', '已使用'],
    revoked: ['bad', '已作废'] };
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('🎟', st.unused || 0, '未使用', '可以发出去的')}
      ${stat('✓', st.used || 0, '已使用', '已经换成账号')}
      ${stat('⊘', st.revoked || 0, '已作废', '不再可用')}
    </div>
    ${card('生成卡密', '每张卡自带套餐和天数，注册时一次性核销',
      `<div class="card-body">
        <div class="form-row"><label>套餐</label>
          <select id="rd-group">${groups.map((g) => `<option value="${esc(g.id)}" ${g.is_default ? 'selected' : ''}>${esc(g.name)}</option>`).join('')}</select></div>
        <div class="form-row"><label>天数</label>
          <input id="rd-days" type="number" min="0" max="3650" value="30" style="width:110px">
          <span class="muted">0 = 不限期</span></div>
        <div class="form-row"><label>数量</label>
          <input id="rd-count" type="number" min="1" max="500" value="10" style="width:110px">
          <span class="muted">一次最多 500 张</span></div>
        <div class="form-row"><label>批次名</label>
          <input id="rd-batch" placeholder="留空自动按时间生成"></div>
        <div class="form-row"><label>备注</label>
          <input id="rd-note" placeholder="给自己看的说明，例如「双十一活动」"></div>
        <div class="toolbar"><button class="btn primary" id="rd-make">生成</button></div>
        <div id="rd-result"></div>
      </div>`)}
    <div class="filter-bar">
      <select id="rd-f-status">
        <option value="">全部状态</option>
        <option value="unused">未使用</option><option value="used">已使用</option>
        <option value="revoked">已作废</option>
      </select>
      <select id="rd-f-batch"><option value="">全部批次</option>${
  (listing.batches || []).map((b) => `<option value="${esc(b)}">${esc(b)}</option>`).join('')}</select>
      <button class="btn primary" id="rd-filter">筛选</button>
      <button class="btn" id="rd-export">导出 CSV</button>
    </div>
    ${tableCard('卡密', `${codes.length} 张`,
    ['卡密', '套餐', '天数', '状态', '批次', '使用者', '使用时间', ''],
    codes.map((c) => {
      const [cls, label] = statusLabel[c.status] || ['idle', c.status];
      return `<tr>
        <td><span class="code-cell" title="点击复制完整卡密"
          onclick="copyRedeem('${q(c.code)}', this)">${esc(c.masked)}</span></td>
        <td>${esc(c.group_name || c.group_id)}</td>
        <td>${c.days ? esc(c.days) + ' 天' : '不限期'}</td>
        <td><span class="tag ${cls}">${esc(label)}</span></td>
        <td>${esc(c.batch || '—')}</td>
        <td>${esc(c.used_by || '—')}</td>
        <td>${c.used_at ? esc(fmtAgeTs(c.used_at)) : '<span class="muted">—</span>'}</td>
        <td class="icon-actions">${c.status === 'unused'
    ? `<button class="btn sm danger" title="作废" onclick="revokeRedeem('${q(c.code)}')">⊘</button>`
    : ''}</td></tr>`;
    }).join(''))}`;
  $('#rd-make').onclick = generateRedeem;
  $('#rd-filter').onclick = () => renderRedeemFiltered();
  $('#rd-export').onclick = exportRedeem;
};
function redeemFilterQuery() {
  const params = new URLSearchParams();
  const stv = (($('#rd-f-status') || {}).value || '');
  const bv = (($('#rd-f-batch') || {}).value || '');
  if (stv) params.set('status', stv);
  if (bv) params.set('batch', bv);
  return params.toString();
}
async function renderRedeemFiltered() {
  const qs = redeemFilterQuery();
  try {
    const listing = await api('/api/redeem' + (qs ? '?' + qs : ''));
    state.redeemListing = listing;
    // Re-render through the page so the table, stats and options stay in
    // agreement rather than drifting apart cell by cell.
    const keep = { status: ($('#rd-f-status') || {}).value, batch: ($('#rd-f-batch') || {}).value };
    await PAGES.redeem();
    if ($('#rd-f-status')) $('#rd-f-status').value = keep.status || '';
    if ($('#rd-f-batch')) $('#rd-f-batch').value = keep.batch || '';
    const rows = listing.codes || [];
    const tbody = document.querySelector('#view table tbody');
    if (tbody && rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">没有匹配的卡密</td></tr>';
    }
  } catch (e) { toast('筛选失败: ' + e.message, 1); }
}
async function generateRedeem() {
  const body = {
    group_id: ($('#rd-group') || {}).value || '',
    days: Number(($('#rd-days') || {}).value || 0),
    count: Number(($('#rd-count') || {}).value || 0),
    batch: (($('#rd-batch') || {}).value || '').trim(),
    note: (($('#rd-note') || {}).value || '').trim(),
  };
  try {
    const r = await api('/api/redeem/generate', {
      method: 'POST', body: JSON.stringify(body) });
    const values = (r.codes || []).map((c) => c.code);
    state.lastRedeemBatch = values;
    const box = $('#rd-result');
    if (box) {
      box.innerHTML = `
        <div class="help" style="margin-top:14px">
          已生成 <b>${values.length}</b> 张（批次 ${esc((r.codes[0] || {}).batch || '')}）。
          <b>这是唯一一次完整显示</b>，列表里只会看到掩码。
        </div>
        <div class="code-dump" id="rd-dump">${esc(values.join('\n'))}</div>
        <div class="toolbar" style="margin-top:10px">
          <button class="btn" id="rd-copy">复制全部</button>
          <button class="btn" id="rd-dl">下载 CSV</button>
        </div>`;
      $('#rd-copy').onclick = () => copyText(values.join('\n'), `已复制 ${values.length} 张`);
      $('#rd-dl').onclick = () => downloadRedeemCsv((r.codes[0] || {}).batch || '');
    }
    toast(`已生成 ${values.length} 张`);
  } catch (e) { toast('生成失败: ' + e.message, 1); }
}
async function copyText(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okMsg || '已复制');
    return true;
  } catch (e) {
    // Clipboard access is denied outside a secure context, which is exactly
    // where a self-hosted panel often runs. Say so instead of failing mutely.
    toast('复制失败，请手动选择文本', 1);
    return false;
  }
}
function copyRedeem(value, el) {
  value = uq(value);
  copyText(value, '已复制完整卡密').then((ok) => {
    if (ok && el) {
      const was = el.textContent;
      el.textContent = value;
      setTimeout(() => { el.textContent = was; }, 4000);
    }
  });
}
function downloadRedeemCsv(batch) {
  const qs = batch ? '?batch=' + encodeURIComponent(batch) : '';
  window.open('/api/redeem/export.csv' + qs, '_blank');
}
function exportRedeem() {
  const qs = redeemFilterQuery();
  window.open('/api/redeem/export.csv' + (qs ? '?' + qs : ''), '_blank');
}
async function revokeRedeem(value) {
  value = uq(value);
  if (!confirm('作废这张卡密？作废后无法再用来注册。')) return;
  try {
    await api(`/api/redeem/${encodeURIComponent(value)}/revoke`, { method: 'POST' });
    toast('已作废');
    renderPage('redeem', true);
  } catch (e) { toast('作废失败: ' + e.message, 1); }
}

/* ---------- invites and pre-authorisation ----------
   The two ways in that do not involve a card: the operator naming a Telegram
   id, or a member spending an invite slot. Both are shown next to the channel
   switches, because "why can nobody register" is usually one of them being
   off. */
PAGES.invites = async () => {
  $('#view').innerHTML = pageLoading();
  const [grants, listing, tg] = await Promise.all([
    api('/api/registration/grants').catch(() => []),
    api('/api/members').catch(() => ({ members: [] })),
    api('/api/settings/telegram').catch(() => ({})),
  ]);
  const members = (listing.members || []).slice()
    .sort((a, b) => String(a.username).localeCompare(String(b.username)));
  state.inviteMembers = members;
  const pending = grants.filter((g) => !g.used_at).length;
  const withQuota = members.filter((m) => (m.invite_quota || 0) > 0);
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('🎫', pending, '待使用授权', `共 ${grants.length} 条`)}
      ${stat('☺', withQuota.length, '持有邀请名额的成员',
    `合计 ${withQuota.reduce((n, m) => n + (m.invite_quota || 0), 0)} 个名额`)}
      ${stat('🌳', members.filter((m) => m.register_via === 'invite').length,
    '通过邀请加入', '占比按当前列表')}
    </div>
    ${card('注册通道', '三个通道各自独立；全部关闭等于停止注册',
    `<div class="card-body">
        <div class="form-row"><label>管理员预授权</label>
          <input id="ch-admin" type="checkbox" ${tg.allow_admin_grant ? 'checked' : ''}>
          <span class="muted">名单里的 Telegram 账号无需任何凭证</span></div>
        <div class="form-row"><label>邀请码</label>
          <input id="ch-invite" type="checkbox" ${tg.allow_invite ? 'checked' : ''}>
          <span class="muted">老用户用自己的名额生成</span></div>
        <div class="form-row"><label>卡密</label>
          <input id="ch-redeem" type="checkbox" ${tg.allow_redeem ? 'checked' : ''}>
          <span class="muted">管理员生成，见「卡密管理」</span></div>
        ${tg.enabled ? '' : '<div class="help">机器人当前未启用，通道开关不会生效。</div>'}
        <div class="toolbar"><button class="btn primary" id="ch-save">保存</button></div>
      </div>`)}
    ${card('管理员预授权', '直接放行某个 Telegram 账号，不需要邀请码或卡密',
    `<div class="card-body">
        <div class="form-row"><label>Telegram ID</label>
          <input id="gr-id" placeholder="纯数字，例如 6425070392" style="max-width:260px">
          <button class="btn primary" id="gr-add">授权</button></div>
        <div class="help">让对方发 /start 给机器人，机器人会回显他的数字 ID。</div>
        ${grants.length ? `<table><thead><tr><th>Telegram ID</th><th>状态</th><th>授权人</th><th>时间</th><th></th></tr></thead><tbody>
          ${grants.map((g) => `<tr>
            <td>${esc(g.tg_user_id)}</td>
            <td>${g.used_at ? '<span class="tag idle">已使用</span>' : '<span class="tag ok">待使用</span>'}</td>
            <td>${esc(g.granted_by || '—')}</td>
            <td>${esc(fmtAgeTs(g.created_at))}</td>
            <td class="icon-actions"><button class="btn sm danger" title="撤销"
              onclick="revokeGrant('${q(g.tg_user_id)}')">🗑</button></td>
          </tr>`).join('')}</tbody></table>` : '<div class="empty">还没有预授权的账号</div>'}
      </div>`)}
    ${card('邀请名额', '发放后成员可在机器人里自助生成邀请码',
    `<div class="card-body">
        <div class="form-row"><label>成员</label>
          <select id="iq-member" style="max-width:260px">${members.map((m) => `<option value="${esc(m.emby_user_id)}">${esc(m.username)}（${m.invite_quota || 0}）</option>`).join('')}</select>
          <input id="iq-delta" type="number" value="1" style="width:90px">
          <button class="btn primary" id="iq-give">发放</button></div>
        <div class="help">填负数即可收回名额；名额不会低于 0。</div>
        ${withQuota.length ? `<table><thead><tr><th>成员</th><th>剩余名额</th><th>已邀请</th><th></th></tr></thead><tbody>
          ${withQuota.map((m) => `<tr>
            <td>${esc(m.username)}</td><td>${esc(m.invite_quota || 0)}</td>
            <td>${esc(m.invitee_count || 0)}</td>
            <td class="icon-actions"><button class="btn sm" title="查看邀请码"
              onclick="showMemberInvites('${q(m.emby_user_id)}','${q(m.username)}')">👁</button></td>
          </tr>`).join('')}</tbody></table>` : '<div class="empty">还没有成员持有邀请名额</div>'}
        <div id="iq-detail"></div>
      </div>`)}`;
  $('#ch-save').onclick = saveChannels;
  $('#gr-add').onclick = addGrant;
  $('#iq-give').onclick = giveQuota;
};
async function saveChannels() {
  try {
    await api('/api/settings/telegram', {
      method: 'POST',
      body: JSON.stringify({
        bot_token: SECRET_KEEP,
        allow_admin_grant: (($('#ch-admin') || {}).checked) || false,
        allow_invite: (($('#ch-invite') || {}).checked) || false,
        allow_redeem: (($('#ch-redeem') || {}).checked) || false,
      }) });
    toast('已保存');
    renderPage('invites', true);
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function addGrant() {
  const value = (($('#gr-id') || {}).value || '').trim();
  if (!value) { toast('请填写 Telegram ID', 1); return; }
  try {
    await api('/api/registration/grants', {
      method: 'POST', body: JSON.stringify({ tg_user_id: value }) });
    toast('已授权');
    renderPage('invites', true);
  } catch (e) { toast('授权失败: ' + e.message, 1); }
}
async function revokeGrant(value) {
  value = uq(value);
  if (!confirm('撤销这条预授权？对方将无法直接注册。')) return;
  try {
    await api(`/api/registration/grants/${encodeURIComponent(value)}`,
      { method: 'DELETE' });
    toast('已撤销');
    renderPage('invites', true);
  } catch (e) { toast('撤销失败: ' + e.message, 1); }
}
async function giveQuota() {
  const id = ($('#iq-member') || {}).value || '';
  const delta = Number(($('#iq-delta') || {}).value || 0);
  if (!id || !delta) { toast('请选择成员并填写数量', 1); return; }
  try {
    const r = await api(`/api/members/${encodeURIComponent(id)}/invite-quota`, {
      method: 'POST', body: JSON.stringify({ delta }) });
    toast(`名额已更新为 ${r.quota}`);
    renderPage('invites', true);
  } catch (e) { toast('发放失败: ' + e.message, 1); }
}
async function showMemberInvites(id, username) {
  id = uq(id); username = uq(username);
  const box = $('#iq-detail');
  if (!box) return;
  box.innerHTML = '<div class="page-loading">加载中…</div>';
  try {
    const d = await api(`/api/members/${encodeURIComponent(id)}/invites`);
    const codes = d.invites || [];
    const kids = d.invitees || [];
    box.innerHTML = `
      <div class="help" style="margin-top:16px"><b>${esc(username)}</b> · 剩余名额 ${esc(d.quota)}</div>
      ${codes.length ? `<table><thead><tr><th>邀请码</th><th>剩余次数</th><th>有效期</th><th>状态</th></tr></thead><tbody>
        ${codes.map((c) => `<tr>
          <td><span class="code-cell" onclick="copyRedeem('${q(c.code)}', this)">${esc(c.masked)}</span></td>
          <td>${esc(c.uses_left)}</td>
          <td>${c.expires_at ? esc(fmtExpiry(c.expires_at)) : '永久'}</td>
          <td>${c.revoked ? '<span class="tag bad">已作废</span>'
    : (c.usable ? '<span class="tag ok">可用</span>' : '<span class="tag idle">已用完</span>')}</td>
        </tr>`).join('')}</tbody></table>` : '<div class="empty">还没有生成过邀请码</div>'}
      ${kids.length ? `<div class="help" style="margin-top:12px">已邀请 ${kids.length} 人：${
  kids.map((k) => esc(k.username)).join('、')}</div>` : ''}`;
  } catch (e) { box.innerHTML = `<div class="help">加载失败：${esc(e.message)}</div>`; }
}

PAGES.tgrequests = async () => {
  $('#view').innerHTML = pageLoading();
  const rows = await api('/api/telegram/requests').catch(() => []);
  const kindLabel = (k) => (k === 'rebind' ? '换绑' : '认领');
  $('#view').innerHTML = `
    <div class="help">
      注册不经过这里：聊天本身已经证明了申请人是谁。
      只有<b>认领旧账号</b>和<b>换绑到新 Telegram</b> 需要确认，因为这两件事申请人无法自证。
    </div>
    ${tableCard('待处理申请', `${rows.length} 条`,
      ['类型', 'Telegram', '申请账号', '提交时间', ''],
      rows.map((r) => `<tr>
        <td><span class="tag ${r.kind === 'rebind' ? 'warn' : 'idle'}">${esc(kindLabel(r.kind))}</span></td>
        <td>${r.tg_username ? '@' + esc(r.tg_username) : esc(r.tg_user_id)}</td>
        <td>${esc(r.wanted_username)}</td>
        <td>${esc(fmtAgeTs(r.created_at))}</td>
        <td class="row-actions">
          <button class="btn sm" onclick="reviewTgRequest(${r.id}, true)">通过</button>
          <button class="btn sm danger" onclick="reviewTgRequest(${r.id}, false)">拒绝</button>
        </td></tr>`).join(''))}`;
};
async function reviewTgRequest(id, approve) {
  if (!approve && !confirm('拒绝这条申请？申请人会收到通知。')) return;
  try {
    await api(`/api/telegram/requests/${id}/review`, {
      method: 'POST', body: JSON.stringify({ approve }) });
    toast(approve ? '已通过并关联' : '已拒绝');
    renderPage('tgrequests', true);
  } catch (e) { toast('操作失败: ' + e.message, 1); }
}

PAGES.tggroup = async () => {
  $('#view').innerHTML = `
    <div class="help">
      核查已关联成员是否还在要求的群组里。<b>只报告，不自动停用</b>：
      退群和停止付费不是一回事，这个判断留给人。
    </div>
    ${card('群组核查', '需要机器人是该群管理员才能查询',
      `<div class="card-body">
        <div class="toolbar">
          <button class="btn primary" id="ga-run">开始核查</button>
          <span id="ga-status" class="muted">尚未执行</span>
        </div>
        <div id="ga-result" style="margin-top:12px"></div>
      </div>`)}`;
  $('#ga-run').onclick = runGroupAudit;
};
async function runGroupAudit() {
  const st = $('#ga-status');
  const box = $('#ga-result');
  st.textContent = '核查中…（成员较多时需要一会儿）';
  box.innerHTML = '';
  try {
    const r = await api('/api/telegram/group-audit', { method: 'POST' });
    if (r.unavailable) {
      st.innerHTML = '<span class="tag idle">未配置群组</span>';
      box.innerHTML = '<div class="empty">先在「机器人」页填写要求群组</div>';
      return;
    }
    st.innerHTML = `<span class="tag ok">已核查 ${r.checked} 人</span>`;
    box.innerHTML = (r.left || []).length
      ? `<table><thead><tr><th>用户</th><th>Telegram</th><th>状态</th></tr></thead><tbody>
          ${r.left.map((m) => `<tr><td>${esc(m.username || '-')}</td>
            <td>${esc(m.tg_user_id)}</td>
            <td><span class="tag warn">${esc(m.status || '已离开')}</span></td></tr>`).join('')}
        </tbody></table>`
      : '<div class="empty">所有已关联成员都还在群里</div>';
  } catch (e) {
    st.innerHTML = `<span class="tag bad">核查失败</span> ${esc(e.message)}`;
  }
}

/* ---------- 自动化 ----------
   Every scheduled job is a plugin, and this page is generated from what the
   backend declares rather than hand-written per feature: a card, its form, its
   schedule line and its last result all come from the same payload. Adding a
   job to the panel means adding a file on the server, not editing this file. */

const automation = { category: 'task', open: {}, busy: {} };

const PLUGIN_CATEGORIES = [
  { id: 'task', label: '任务' },
  { id: 'points', label: '积分' },
  { id: 'request', label: '求片' },
];

PAGES.automation = async () => {
  $('#view').innerHTML = pageLoading();
  const cards = await api(`/api/plugins?category=${encodeURIComponent(automation.category)}`)
    .catch(() => null);
  if (!cards) { $('#view').innerHTML = pageError('无法读取任务列表'); return; }

  const tabs = PLUGIN_CATEGORIES.map((c) =>
    `<button class="btn ${c.id === automation.category ? 'primary' : ''}"
       onclick="switchPluginCategory('${c.id}')">${esc(c.label)}</button>`).join('');

  const emptyLabel = ({ points: '还没有已注册的积分功能',
    request: '还没有已注册的求片任务' })[automation.category]
    || '还没有已注册的任务';
  const body = cards.length
    ? `<div class="plugin-grid">${cards.map(pluginCard).join('')}</div>`
    : `<div class="card"><div class="empty">${emptyLabel}</div></div>`;

  $('#view').innerHTML = `
    <div class="help">${{
    points: `积分功能和定时任务共用同一套开关与配置。<b>签到和转账由成员在机器人里触发</b>，
       这里的「立即运行」只统计不发放；<b>关掉开关，机器人里对应的按钮就会消失</b>。`,
    request: `求片相关的定时任务。<b>每条求片在提交时就会推给上片员</b>，
       这里的摘要只是每天提醒一次还有多少没人接，避免没人接的求片一直没动静。`,
  }[automation.category]
    || `定时任务在这里统一开关和配置。<b>「立即运行」不看开关</b>：先试一次再决定要不要常开。
       任何一个任务出错都只影响它自己的卡片，不会影响其他任务。`}
    </div>
    <div class="toolbar" style="margin-bottom:14px">${tabs}</div>
    ${body}`;

  cards.forEach(bindPluginCard);
};

function switchPluginCategory(id) {
  automation.category = id;
  renderPage('automation');
}

function pluginScheduleText(c) {
  if (c.hour !== null && c.hour !== undefined) {
    const hour = (c.config && c.config.hour !== undefined) ? c.config.hour : c.hour;
    return `每天 ${esc(String(hour))}:00`;
  }
  if (c.interval > 0) return `每 ${fmtAge(c.interval)}`;
  return '仅手动';
}

function pluginField(pid, f, value) {
  const id = `pl-${pid}-${f.key}`;
  const v = value === undefined ? f.default : value;
  let input;
  if (f.kind === 'bool') {
    input = `<input id="${id}" type="checkbox" ${v ? 'checked' : ''}>`;
  } else if (f.kind === 'int') {
    const min = f.min === undefined ? '' : ` min="${esc(f.min)}"`;
    const max = f.max === undefined ? '' : ` max="${esc(f.max)}"`;
    input = `<input id="${id}" type="number"${min}${max} value="${esc(v)}" style="width:110px">`;
  } else if (f.kind === 'select') {
    input = `<select id="${id}">${(f.options || []).map((o) =>
      `<option value="${esc(o.value)}" ${o.value === v ? 'selected' : ''}>${esc(o.label)}</option>`
    ).join('')}</select>`;
  } else if (f.kind === 'text') {
    input = `<textarea id="${id}" rows="3" style="flex:1;min-width:240px">${esc(v)}</textarea>`;
  } else {
    input = `<input id="${id}" value="${esc(v)}" style="flex:1;min-width:200px">`;
  }
  return `<div class="form-row">
    <label>${esc(f.label)}</label>${input}
    ${f.help ? `<span class="muted">${esc(f.help)}</span>` : ''}
  </div>`;
}

/* A result the operator can check: when it ran, whether it worked, what it did
   and how long it took. A job reporting nothing is indistinguishable from a job
   that never ran, which is the failure mode that goes unnoticed for weeks. */
function pluginLastRun(c) {
  const last = c.last_run;
  if (!last) return '<div class="muted">尚未运行过</div>';
  const summary = last.summary || {};
  const kv = Object.keys(summary).map((k) =>
    `<span class="kv"><b>${esc(k)}</b>${esc(String(summary[k]))}</span>`).join('');
  return `<div class="plugin-last">
    <div>
      ${last.ok ? '<span class="tag ok">成功</span>' : '<span class="tag bad">失败</span>'}
      <span class="muted">${esc(fmtAgeTs(last.started_at))} ·
        ${esc(last.trigger === 'manual' ? '手动' : '定时')} ·
        ${esc(Math.round(Number(last.duration_ms || 0)))} ms</span>
    </div>
    ${kv ? `<div class="kv-row">${kv}</div>` : ''}
  </div>`;
}

function pluginCard(c) {
  const busy = automation.busy[c.id];
  const open = automation.open[c.id];
  return `<div class="card plugin-card" data-plugin="${esc(c.id)}">
    <div class="card-head">
      <div style="display:flex;gap:10px;align-items:flex-start">
        <div class="ic-box">${c.icon || '⚙'}</div>
        <div>
          <h3>${esc(c.name)}</h3>
          <div class="sub">${esc(pluginScheduleText(c))}</div>
        </div>
      </div>
      <label class="plugin-switch">
        <input id="pl-${esc(c.id)}-enabled" type="checkbox" ${c.enabled ? 'checked' : ''}>
        <span class="muted">${c.enabled ? '已启用' : '已停用'}</span>
      </label>
    </div>
    <div class="card-body">
      <div class="muted" style="line-height:1.6;margin-bottom:12px">${esc(c.description)}</div>
      ${(c.fields || []).map((f) => pluginField(c.id, f, (c.config || {})[f.key])).join('')}
      <div class="toolbar" style="margin-top:12px">
        <button class="btn primary" data-act="save" ${busy ? 'disabled' : ''}>保存</button>
        <button class="btn" data-act="run" ${busy ? 'disabled' : ''}>
          ${busy ? '运行中…' : '立即运行'}</button>
        <button class="btn sm" data-act="history">${open ? '收起历史' : '历史'}</button>
      </div>
      <div style="margin-top:12px">${pluginLastRun(c)}</div>
      <div class="plugin-history" data-history="${esc(c.id)}">
        ${open ? '<div class="muted">读取中…</div>' : ''}
      </div>
    </div>
  </div>`;
}

function pluginCardEl(pid) {
  return document.querySelector(`.plugin-card[data-plugin="${pid}"]`);
}

function bindPluginCard(c) {
  const el = pluginCardEl(c.id);
  if (!el) return;
  el.querySelector('[data-act="save"]').onclick = () => savePlugin(c);
  el.querySelector('[data-act="run"]').onclick = () => runPlugin(c);
  el.querySelector('[data-act="history"]').onclick = () => togglePluginHistory(c);
  if (automation.open[c.id]) loadPluginHistory(c.id);
}

function pluginPayload(c) {
  const config = {};
  (c.fields || []).forEach((f) => {
    const el = $(`#pl-${c.id}-${f.key}`);
    if (!el) return;
    config[f.key] = f.kind === 'bool' ? el.checked : el.value;
  });
  const sw = $(`#pl-${c.id}-enabled`);
  return { enabled: sw ? sw.checked : c.enabled, config };
}

async function savePlugin(c) {
  try {
    await api(`/api/plugins/${encodeURIComponent(c.id)}`, {
      method: 'POST', body: JSON.stringify(pluginPayload(c)) });
    toast('已保存');
    renderPage('automation');
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}

async function runPlugin(c) {
  // Save first: running with what is on screen rather than what was last
  // stored is what the operator means by "试一次".
  automation.busy[c.id] = true;
  renderPage('automation');
  try {
    await api(`/api/plugins/${encodeURIComponent(c.id)}`, {
      method: 'POST', body: JSON.stringify(pluginPayload(c)) });
    const r = await api(`/api/plugins/${encodeURIComponent(c.id)}/run`, { method: 'POST' });
    toast(r.ok ? '运行完成' : ('运行失败: ' + (r.error || '见卡片结果')), !r.ok);
  } catch (e) {
    toast('运行失败: ' + e.message, 1);
  } finally {
    automation.busy[c.id] = false;
    renderPage('automation');
  }
}

function togglePluginHistory(c) {
  automation.open[c.id] = !automation.open[c.id];
  renderPage('automation');
}

async function loadPluginHistory(pid) {
  const box = document.querySelector(`[data-history="${pid}"]`);
  if (!box) return;
  try {
    const rows = await api(`/api/plugins/${encodeURIComponent(pid)}/history?limit=10`);
    box.innerHTML = rows.length
      ? `<table><thead><tr><th>时间</th><th>结果</th><th>触发</th><th>耗时</th><th>摘要</th></tr></thead>
         <tbody>${rows.map((r) => `<tr>
           <td>${esc(fmtAgeTs(r.started_at))}</td>
           <td>${r.ok ? '<span class="tag ok">成功</span>' : '<span class="tag bad">失败</span>'}</td>
           <td>${esc(r.trigger === 'manual' ? '手动' : '定时')}</td>
           <td>${esc(Math.round(Number(r.duration_ms || 0)))} ms</td>
           <td class="muted">${esc(JSON.stringify(r.summary || {}))}</td>
         </tr>`).join('')}</tbody></table>`
      : '<div class="empty">还没有运行记录</div>';
  } catch (e) {
    box.innerHTML = `<div class="muted">历史读取失败：${esc(e.message)}</div>`;
  }
}

/* ---------- 安全 ----------
   Two pages that share one principle: the panel sits on the media path, so it
   reports far more readily than it acts. Access rules are the one place it does
   refuse, which is why the block log is shown right next to them -- a refusal
   that leaves no trace is indistinguishable from a broken node. */

PAGES.access = async () => {
  $('#view').innerHTML = pageLoading();
  const [rules, blocks] = await Promise.all([
    api('/api/access/rules').catch(() => null),
    api('/api/access/blocks?limit=100').catch(() => []),
  ]);
  if (!rules) { $('#view').innerHTML = pageError('无法读取访问规则'); return; }

  const dayAgo = (Date.now() / 1000) - 86400;
  const todayBlocks = blocks.filter((b) => Number(b.blocked_at || 0) >= dayAgo).length;
  const kindLabel = (k) => (k === 'network' ? '网段' : '客户端');

  $('#view').innerHTML = `
    <div class="help">
      规则只在播放请求上生效。<b>出错一律放行</b>：面板挡在播放链路上，
      写错一条正则不能让所有人看不了片。被拒的请求都会记录在下面。
    </div>
    <div class="stat-grid">
      ${stat('🛡', rules.length, '规则数', `${rules.filter((r) => r.enabled).length} 条已启用`)}
      ${stat('⛔', todayBlocks, '今日拦截', `累计 ${blocks.length} 条记录`)}
    </div>
    ${card('新增规则', '客户端按 User-Agent 正则匹配；网段填单个地址或 CIDR',
      `<div class="card-body">
        <div class="form-row"><label>类型</label>
          <select id="ac-kind">
            <option value="client">客户端（User-Agent）</option>
            <option value="network">网段（IP / CIDR）</option>
          </select></div>
        <div class="form-row"><label>内容</label>
          <input id="ac-pattern" placeholder="例如 curl|wget 或 203.0.113.0/24"></div>
        <div class="form-row"><label>动作</label>
          <select id="ac-action">
            <option value="deny">拒绝</option>
            <option value="allow">放行</option>
          </select>
          <span class="muted">放行规则优先于拒绝，用来给例外开口子</span></div>
        <div class="form-row"><label>备注</label>
          <input id="ac-note" placeholder="写清楚为什么加这条，几个月后你会需要"></div>
        <div class="toolbar"><button class="btn primary" id="ac-add">添加规则</button></div>
      </div>`)}
    ${tableCard('规则列表', `${rules.length} 条`,
      ['类型', '内容', '动作', '备注', '启用', ''],
      rules.map((r) => `<tr>
        <td><span class="tag idle">${esc(kindLabel(r.kind))}</span></td>
        <td><code>${esc(r.pattern)}</code></td>
        <td>${r.action === 'deny'
          ? '<span class="tag bad">拒绝</span>' : '<span class="tag ok">放行</span>'}</td>
        <td class="muted">${esc(r.note || '-')}</td>
        <td><input type="checkbox" ${r.enabled ? 'checked' : ''}
          onchange="toggleAccessRule(${r.id}, this.checked)"></td>
        <td class="row-actions">
          <button class="btn sm danger" onclick="deleteAccessRule(${r.id})">删除</button>
        </td></tr>`).join(''))}
    ${tableCard('拦截记录', '最近 100 条', ['时间', '用户', '客户端', '地址', '命中规则'],
      blocks.map((b) => `<tr>
        <td>${esc(fmtAgeTs(b.blocked_at))}</td>
        <td>${esc(b.username || '-')}</td>
        <td class="muted" title="${esc(b.user_agent || '')}">${esc((b.user_agent || '-').slice(0, 60))}</td>
        <td>${esc(b.remote_ip || '-')}</td>
        <td class="muted">${esc(b.reason || '')}${b.rule_id ? ` (#${esc(b.rule_id)})` : ''}</td>
      </tr>`).join(''))}`;
  $('#ac-add').onclick = addAccessRule;
};

async function addAccessRule() {
  const pattern = $('#ac-pattern').value.trim();
  if (!pattern) { toast('请填写规则内容', 1); return; }
  try {
    await api('/api/access/rules', { method: 'POST', body: JSON.stringify({
      kind: $('#ac-kind').value, pattern, action: $('#ac-action').value,
      note: $('#ac-note').value.trim(), enabled: true }) });
    toast('规则已添加');
    renderPage('access', true);
  } catch (e) {
    // A bad regex or netmask comes back as a 400 with the reason; showing it
    // verbatim is the difference between fixing it and guessing.
    toast('添加失败: ' + e.message, 1);
  }
}

async function toggleAccessRule(id, enabled) {
  try {
    await api(`/api/access/rules/${id}/enabled`, {
      method: 'POST', body: JSON.stringify({ enabled }) });
    toast(enabled ? '规则已启用' : '规则已停用');
    renderPage('access');
  } catch (e) { toast('操作失败: ' + e.message, 1); }
}

async function deleteAccessRule(id) {
  if (!confirm('删除这条规则？')) return;
  try {
    await api(`/api/access/rules/${id}`, { method: 'DELETE' });
    toast('规则已删除');
    renderPage('access', true);
  } catch (e) { toast('删除失败: ' + e.message, 1); }
}

PAGES.sharing = async () => {
  $('#view').innerHTML = pageLoading();
  const data = await api('/api/sharing?limit=50').catch(() => null);
  if (!data) { $('#view').innerHTML = pageError('无法读取共享检测结果'); return; }
  const st = data.status || {};
  const items = data.items || [];
  $('#view').innerHTML = `
    <div class="help">
      同一账号同时在多个网络播放时会记在这里。<b>只记录不处理</b>：
      一家人有电视和手机，手机从 Wi-Fi 切到流量也会算成两个网络，
      按这个自动封号会误伤付费用户。判断留给人。
    </div>
    <div class="stat-grid">
      ${stat('👥', st.tracked_accounts || 0, '追踪账号', '当前有播放活动的账号')}
      ${stat('⚠', st.multi_network_now || 0, '多地播放', '此刻同时在多个网络')}
      ${stat('☰', items.length, '历史发现', `网络需持续 ${fmtAge(st.min_network_seconds || 0)}才计入`)}
    </div>
    ${tableCard('发现记录', '最近 50 条', ['时间', '用户', '网络数', '网络列表'],
      items.map((it) => `<tr>
        <td>${esc(fmtAgeTs(it.detected_at))}</td>
        <td>${esc(it.username || it.emby_user_id || '-')}</td>
        <td><span class="tag warn">${esc(it.network_count)}</span></td>
        <td class="muted">${(it.networks || []).map((n) => esc(n)).join(' · ')}</td>
      </tr>`).join(''))}`;
};

/* ---------- 兑换商城 ----------
   Items are data, so this page is a plain editor over them: the operator
   prices and retires things without a release. The orders table underneath is
   the other half -- a catalogue with no record of what it handed out cannot
   answer "why does this member have 500GB extra". */
const SHOP_KINDS = [
  { id: 'traffic', label: '流量包', unit: 'GB' },
  { id: 'days', label: '会员天数', unit: '天' },
  { id: 'bandwidth', label: '带宽提速', unit: 'Mbps' },
  { id: 'invite', label: '邀请名额', unit: '个' },
];
function shopUnit(kind) {
  const found = SHOP_KINDS.find((k) => k.id === kind);
  return found ? found.unit : '';
}
PAGES.shop = async () => {
  $('#view').innerHTML = pageLoading();
  const [items, orders] = await Promise.all([
    api('/api/shop/items').catch(() => null),
    api('/api/shop/orders?limit=50').catch(() => []),
  ]);
  if (!items) { $('#view').innerHTML = pageError('无法读取商城商品'); return; }
  // Kept so the edit dialog can prefill from the row already on screen
  // rather than re-fetching one item.
  state.shopItems = items;
  const live = items.filter((i) => i.enabled).length;
  const spent = orders.reduce((sum, o) => sum + Number(o.cost || 0), 0);
  $('#view').innerHTML = `
    <div class="help">
      成员用积分在机器人的「背包 → 兑换商城」里兑换这些商品。
      <b>新建的商品默认要手动开启</b>，开启后成员才看得到；
      带宽提速对<b>不限速</b>的账号无意义，系统会直接拒绝并且不扣分。
    </div>
    <div class="stat-grid">
      ${stat('🎁', items.length, '商品', `${live} 个已上架`)}
      ${stat('📜', orders.length, '兑换记录', '最近 50 条')}
      ${stat('💰', spent, '消耗积分', '这些记录合计')}
    </div>
    ${card('新增商品', '数量的单位随类型变化：流量按 GB，天数按天，提速按 Mbps，名额按个',
    `<div class="card-body">
        <div class="form-row"><label>类型</label>
          <select id="sh-kind">${SHOP_KINDS.map((k) =>
    `<option value="${esc(k.id)}">${esc(k.label)}</option>`).join('')}</select>
          <span class="muted" id="sh-unit">单位 GB</span></div>
        <div class="form-row"><label>名称</label>
          <input id="sh-name" placeholder="例如「流量包 50GB」"></div>
        <div class="form-row"><label>说明</label>
          <input id="sh-desc" placeholder="成员在机器人里看到的一句话说明"></div>
        <div class="form-row"><label>消耗积分</label>
          <input id="sh-cost" type="number" min="1" value="100" style="width:110px"></div>
        <div class="form-row"><label>数量</label>
          <input id="sh-amount" type="number" min="1" value="50" style="width:110px"></div>
        <div class="form-row"><label>每人限兑</label>
          <input id="sh-limit" type="number" min="0" value="0" style="width:110px">
          <span class="muted">0 = 不限</span></div>
        <div class="form-row"><label>排序</label>
          <input id="sh-sort" type="number" value="0" style="width:110px">
          <span class="muted">越小越靠前</span></div>
        <div class="form-row"><label>立即上架</label>
          <input id="sh-enabled" type="checkbox"></div>
        <div class="toolbar"><button class="btn primary" id="sh-add">新增商品</button></div>
      </div>`)}
    ${tableCard('商品', `${items.length} 个`,
    ['名称', '类型', '消耗', '数量', '每人限兑', '排序', '上架', ''],
    items.length ? items.map((i) => `<tr>
        <td><div class="u-name">${esc(i.name)}</div>
          ${i.description ? `<div class="u-sub muted">${esc(i.description)}</div>` : ''}</td>
        <td>${esc(i.kind_label || i.kind)}</td>
        <td><b>${esc(i.cost)}</b> 分</td>
        <td>${esc(i.amount)} ${esc(i.unit || shopUnit(i.kind))}</td>
        <td>${i.per_user_limit ? esc(i.per_user_limit) + ' 次' : '<span class="muted">不限</span>'}</td>
        <td>${esc(i.sort)}</td>
        <td><input type="checkbox" ${i.enabled ? 'checked' : ''}
          onchange="toggleShopItem(${Number(i.id)}, this.checked)"></td>
        <td class="icon-actions">
          <button class="btn sm" title="编辑" onclick="editShopItem(${Number(i.id)})">✎</button>
          <button class="btn sm danger" title="删除"
            onclick="deleteShopItem(${Number(i.id)}, '${q(i.name)}')">🗑</button>
        </td></tr>`).join('')
      : '<tr><td colspan="8"><div class="empty">还没有商品</div></td></tr>')}
    ${tableCard('兑换记录', '最近 50 条', ['时间', '用户', '商品', '发放', '消耗'],
    orders.length ? orders.map((o) => `<tr>
        <td>${esc(fmtAgeTs(o.created_at))}</td>
        <td>${esc(o.username || o.emby_user_id || '-')}</td>
        <td>${esc(o.item_name || '-')}</td>
        <td>${esc(o.amount)} ${esc(o.unit || shopUnit(o.kind))}</td>
        <td>-${esc(o.cost)} 分</td></tr>`).join('')
      : '<tr><td colspan="5"><div class="empty">还没有人兑换过</div></td></tr>')}`;
  const kindSel = $('#sh-kind');
  if (kindSel) {
    kindSel.onchange = () => {
      const unit = $('#sh-unit');
      if (unit) unit.textContent = '单位 ' + shopUnit(kindSel.value);
    };
  }
  if ($('#sh-add')) $('#sh-add').onclick = addShopItem;
};
function shopFormPayload() {
  return {
    kind: ($('#sh-kind') || {}).value,
    name: (($('#sh-name') || {}).value || '').trim(),
    description: (($('#sh-desc') || {}).value || '').trim(),
    cost: Number(($('#sh-cost') || {}).value || 0),
    amount: Number(($('#sh-amount') || {}).value || 0),
    per_user_limit: Number(($('#sh-limit') || {}).value || 0),
    sort: Number(($('#sh-sort') || {}).value || 0),
    enabled: !!(($('#sh-enabled') || {}).checked),
  };
}
async function addShopItem() {
  const payload = shopFormPayload();
  if (!payload.name) { toast('请填写商品名称', 1); return; }
  try {
    await api('/api/shop/items', { method: 'POST', body: JSON.stringify(payload) });
    toast('已新增商品'); renderPage('shop');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function toggleShopItem(id, enabled) {
  try {
    await api(`/api/shop/items/${Number(id)}`, {
      method: 'PUT', body: JSON.stringify({ enabled }) });
    toast(enabled ? '已上架' : '已下架'); renderPage('shop');
  } catch (e) { toast('失败: ' + e.message, 1); renderPage('shop'); }
}
function editShopItem(id) {
  const item = ((state.shopItems || []).find((i) => i.id === id));
  const row = item || { id };
  openModal('编辑商品', `
    <div class="form-row"><label>名称</label><input id="se-name" value="${esc(row.name || '')}"></div>
    <div class="form-row"><label>说明</label><input id="se-desc" value="${esc(row.description || '')}"></div>
    <div class="form-row"><label>消耗积分</label>
      <input id="se-cost" type="number" min="1" value="${esc(row.cost || 1)}" style="width:110px"></div>
    <div class="form-row"><label>数量</label>
      <input id="se-amount" type="number" min="1" value="${esc(row.amount || 1)}" style="width:110px"></div>
    <div class="form-row"><label>每人限兑</label>
      <input id="se-limit" type="number" min="0" value="${esc(row.per_user_limit || 0)}" style="width:110px">
      <span class="muted">0 = 不限</span></div>
    <div class="form-row"><label>排序</label>
      <input id="se-sort" type="number" value="${esc(row.sort || 0)}" style="width:110px"></div>
    <div class="toolbar"><button class="btn primary" id="se-save">保存</button></div>`);
  const save = $('#se-save');
  if (save) {
    save.onclick = async () => {
      try {
        await api(`/api/shop/items/${Number(id)}`, {
          method: 'PUT',
          body: JSON.stringify({
            name: ($('#se-name') || {}).value,
            description: ($('#se-desc') || {}).value,
            cost: Number(($('#se-cost') || {}).value || 0),
            amount: Number(($('#se-amount') || {}).value || 0),
            per_user_limit: Number(($('#se-limit') || {}).value || 0),
            sort: Number(($('#se-sort') || {}).value || 0),
          }),
        });
        closeModal(); toast('已保存'); renderPage('shop');
      } catch (e) { toast('失败: ' + e.message, 1); }
    };
  }
}
async function deleteShopItem(id, name) {
  if (!confirm(`确定删除商品「${uq(name)}」？已产生的兑换记录会保留。`)) return;
  try {
    await api(`/api/shop/items/${Number(id)}`, { method: 'DELETE' });
    toast('已删除'); renderPage('shop');
  } catch (e) { toast('失败: ' + e.message, 1); }
}

/* ---------- 求片 ----------
   The operator's view of the same queue the uploaders see in Telegram. It
   exists because the bot fan-out only reaches uploaders who linked a chat,
   and somebody has to be able to see and close a request when nobody did. */
const REQUEST_STATUS_TABS = [
  { id: 'active', label: '未处理' },
  { id: 'open', label: '待接单' },
  { id: 'claimed', label: '处理中' },
  { id: 'done', label: '已处理' },
  { id: 'rejected', label: '已拒绝' },
  { id: '', label: '全部' },
];
const requestsView = { status: 'active' };

function requestStatusTag(row) {
  const cls = ({ open: 'warn', claimed: 'idle', done: 'ok',
    rejected: 'bad' })[row.status] || 'idle';
  return `<span class="tag ${cls}">${esc(row.status_label || row.status)}</span>`;
}
function requestPoster(row) {
  if (!row.poster_path) return '<span class="muted">—</span>';
  const src = String(row.poster_path).startsWith('http')
    ? row.poster_path : `https://image.tmdb.org/t/p/w92${row.poster_path}`;
  return `<img src="${esc(src)}" alt="" loading="lazy"
    style="width:34px;height:51px;object-fit:cover;border-radius:3px">`;
}
function requestActions(row) {
  if (row.status === 'open') {
    return `<button class="btn sm" onclick="claimRequest(${Number(row.id)})">接单</button>`;
  }
  if (row.status === 'claimed') {
    return `<button class="btn sm" onclick="resolveRequest(${Number(row.id)},1)">已处理</button>
      <button class="btn sm danger" onclick="resolveRequest(${Number(row.id)},0)">拒绝</button>`;
  }
  return '<span class="muted">—</span>';
}
PAGES.requests = async () => {
  $('#view').innerHTML = pageLoading();
  const query = requestsView.status ? `?status=${encodeURIComponent(requestsView.status)}` : '';
  const [rows, stats] = await Promise.all([
    api(`/api/requests${query}`).catch(() => null),
    api('/api/requests/stats').catch(() => ({})),
  ]);
  if (!rows) { $('#view').innerHTML = pageError('无法读取求片列表'); return; }

  const tabs = REQUEST_STATUS_TABS.map((t) =>
    `<button class="btn ${t.id === requestsView.status ? 'primary' : ''}"
       onclick="switchRequestStatus('${t.id}')">${esc(t.label)}</button>`).join('');

  $('#view').innerHTML = `
    <div class="help">
      成员在机器人里发 TMDB 链接求片，<b>每条求片会单独发给每个已关联 Telegram 的上片员</b>，
      谁先点「接单」谁负责，其他人的按钮会自动收回。
      这里可以代为接单或关闭；关闭后求片人会收到通知。
      <b>没有配置 TMDB Key 也能用</b>，只是显示编号而不是片名。
    </div>
    <div class="stat-grid">
      ${stat('🕓', stats.open || 0, '待接单', '还没有人认领')}
      ${stat('🔧', stats.claimed || 0, '处理中', '已有上片员接单')}
      ${stat('✅', stats.done || 0, '已处理', '累计')}
      ${stat('📅', stats.month_total || 0, '本月求片', stats.period || '')}
    </div>
    <div class="toolbar" style="margin-bottom:14px">${tabs}</div>
    ${tableCard('求片列表', `${rows.length} 条`,
    ['编号', '海报', '片名', '类型', '求片人', '状态', '接单人', '时间', ''],
    rows.map((r) => `<tr>
        <td>#${esc(r.id)}</td>
        <td>${requestPoster(r)}</td>
        <td><div class="u-name">${esc(r.display_title)}</div>
          <div class="u-sub muted">TMDB ${esc(r.tmdb_id)}${r.note ? ' · ' + esc(r.note) : ''}</div>
          ${r.result_note ? `<div class="u-sub muted">结果：${esc(r.result_note)}</div>` : ''}</td>
        <td>${esc(r.media_label || '-')}</td>
        <td>${esc(r.username || r.emby_user_id || '-')}</td>
        <td>${requestStatusTag(r)}</td>
        <td>${r.claimed_by_name ? esc(r.claimed_by_name) : '<span class="muted">—</span>'}</td>
        <td class="muted">${esc(fmtAgeTs(r.created_at))}</td>
        <td class="row-actions">${requestActions(r)}</td>
      </tr>`).join(''))}`;
};
function switchRequestStatus(id) {
  requestsView.status = id;
  renderPage('requests');
}
async function claimRequest(id) {
  try {
    const r = await api(`/api/requests/${Number(id)}/claim`, {
      method: 'POST', body: JSON.stringify({}) });
    /* A lost race is a normal outcome, not an error: somebody in Telegram
       may have tapped 接单 while this page was open. */
    toast(r && r.ok === false
      ? `已被 ${r.claimed_by_name || '其他上片员'} 接单`
      : '已接单');
    renderPage('requests');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function resolveRequest(id, done) {
  let note = '';
  if (!done) {
    note = prompt('无法处理的原因？会原样发给求片人。', '暂时找不到片源');
    if (note === null) return;
  } else if (!confirm('标记为已处理？求片人会收到通知。')) {
    return;
  }
  try {
    await api(`/api/requests/${Number(id)}/resolve`, {
      method: 'POST', body: JSON.stringify({ done: !!done, note }) });
    toast(done ? '已标记处理完成' : '已拒绝并通知求片人');
    renderPage('requests');
  } catch (e) { toast('失败: ' + e.message, 1); }
}

/* mediadeck panel shell.
   Sidebar-grouped navigation + dashboard/stat-card layout. Pages are rendered
   into #view; each page declares its own loader so new modules only need a
   PAGES entry. */

const state = { page: 'dashboard', timer: null };
const $ = (s) => document.querySelector(s);

const NAV = [
  { group: '概览', items: [
    { id: 'dashboard', icon: '▦', label: '仪表盘', sub: '集中查看系统运行、播放使用和待处理事项' },
  ]},
  { group: '工作台', items: [
    { id: 'acquire', icon: '⌕', label: '搜索订阅', sub: '识别媒体、搜索站点资源、管理订阅' },
    { id: 'downloads', icon: '↓', label: '下载任务', sub: '当前下载队列与进度' },
    { id: 'imports', icon: '⇪', label: '网盘上片', sub: '网盘链接与云盘目录导入' },
    { id: 'users', icon: '☺', label: '用户管理', sub: '账号、状态与密码' },
  ]},
  { group: '资源服务', items: [
    { id: 'nodes', icon: '⛁', label: '节点管理', sub: '推流节点负载与调度' },
    { id: 'pipeline', icon: '⇄', label: '管线状态', sub: '整理、上传队列与配额' },
  ]},
  { group: '系统管理', items: [
    { id: 'update', icon: '⟳', label: '版本更新', sub: '检查并应用新版本' },
  ]},
];

const PAGES = {};

/* ---------------- helpers ---------------- */
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.style.background = bad ? '#e5484d' : '#1f2937';
  t.style.display = 'block';
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.display = 'none'), 3200);
}
async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  if (!r.ok) {
    let d = '';
    try { d = (await r.json()).detail || ''; } catch (e) { /* non-json error */ }
    throw new Error(`${r.status} ${d}`);
  }
  return r.json();
}
function fmtBytes(n) {
  if (!n) return '0';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + u[i];
}
function fmtAge(s) {
  if (!s) return '-';
  if (s < 3600) return Math.round(s / 60) + ' 分钟';
  if (s < 86400) return (s / 3600).toFixed(1) + ' 小时';
  return (s / 86400).toFixed(1) + ' 天';
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const stat = (icon, val, label, sub) => `
  <div class="stat"><div class="ic-box">${icon}</div>
    <div class="val">${val}</div><div class="label">${esc(label)}</div>
    <div class="sub">${esc(sub || '')}</div></div>`;
const card = (title, sub, body, actions) => `
  <div class="card">
    <div class="card-head">
      <div><h3>${esc(title)}</h3>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>
      <div class="toolbar">${actions || ''}</div>
    </div>
    ${body}
  </div>`;
const tableCard = (title, sub, cols, rowsHtml, actions) => card(
  title, sub,
  `<div class="card-body flush">${rowsHtml
    ? `<table><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rowsHtml}</tbody></table>`
    : `<div class="empty">暂无数据</div>`}</div>`,
  actions);

/* ---------------- shell ---------------- */
function buildNav() {
  $('#nav').innerHTML = NAV.map((g) => `
    <div class="nav-group">${esc(g.group)}</div>
    ${g.items.map((it) => `<div class="nav-item" data-page="${it.id}">
        <span class="ic">${it.icon}</span><span>${esc(it.label)}</span></div>`).join('')}
  `).join('');
  $('#nav').addEventListener('click', (e) => {
    const el = e.target.closest('.nav-item');
    if (el) go(el.dataset.page);
  });
}
function navMeta(id) {
  for (const g of NAV) for (const it of g.items) if (it.id === id) return it;
  return NAV[0].items[0];
}
function go(page) {
  state.page = page;
  const meta = navMeta(page);
  document.querySelectorAll('.nav-item').forEach((n) =>
    n.classList.toggle('active', n.dataset.page === page));
  $('#page-title').textContent = meta.label;
  $('#page-sub').textContent = meta.sub;
  location.hash = '#/' + page;
  renderPage(page);
}
async function renderPage(page, manual) {
  const fn = PAGES[page];
  if (!fn) { $('#view').innerHTML = '<div class="empty">页面不存在</div>'; return; }
  try {
    await fn();
    $('#last-updated').textContent = '最近更新: ' + new Date().toLocaleTimeString();
    if (manual) toast('已刷新');
  } catch (e) {
    toast('加载失败: ' + e.message, 1);
  }
}

/* ---------------- pages ---------------- */
PAGES.dashboard = async () => {
  const [sessions, pipe, nodes, subs, dls] = await Promise.all([
    api('/api/emby/sessions').catch(() => []),
    api('/api/pipeline').catch(() => ({ available: false })),
    api('/api/nodes').catch(() => []),
    api('/api/mp/subscribes').catch(() => []),
    api('/api/mp/downloading').catch(() => []),
  ]);
  const online = nodes.filter((n) => n.available).length;
  const d = pipe.available ? pipe.data : {};
  const queues = d.queues || [];
  const queued = queues.reduce((a, q) => a + (q.items || 0), 0);
  const limited = (d.quota || []).filter((q) => q.state !== 'ok').length;
  const alerts = d.alerts || [];

  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('⛁', `${online} / ${nodes.length}`, '在线节点', nodes.length ? '推流节点健康状态' : '尚未配置节点')}
      ${stat('▶', sessions.length, '当前播放', sessions.length ? '正在进行的会话' : '暂无活跃会话')}
      ${stat('⌕', subs.length, '媒体订阅', '追更与补全中')}
      ${stat('↓', dls.length, '下载任务', '下载器活动任务')}
      ${stat('⇄', queued, '管线待处理', pipe.available ? '整理与上传队列' : '快照不可用')}
      ${stat('⚠', alerts.length + limited, '待处理事项', limited ? `${limited} 个上传身份受限` : '系统关键状态')}
    </div>
    <div class="grid-2">
      ${tableCard('当前播放', '实时会话', ['用户', '客户端', '方式', '码率'],
        sessions.map((s) => `<tr><td>${esc(s.UserName)}</td><td>${esc(s.Client)}</td>
          <td>${esc(s.PlayMethod)}</td><td>${esc(s.BitrateMbps)} Mbps</td></tr>`).join(''))}
      ${tableCard('管线队列', pipe.available ? `快照 ${Math.round(pipe.snapshot_age_seconds)}s 前` : '快照不可用',
        ['队列', '条目', '体积', '最老'],
        queues.map((q) => `<tr><td>${esc(q.name)}</td><td>${q.items}</td>
          <td>${fmtBytes(q.bytes)}</td><td>${fmtAge(q.oldest_age_seconds)}</td></tr>`).join(''))}
    </div>
    <div class="grid-2">
      ${tableCard('上传身份配额', '限额状态', ['身份', '状态', '受限起始'],
        (d.quota || []).map((q) => `<tr><td>${esc(q.identity)}</td>
          <td><span class="tag ${q.state === 'ok' ? 'ok' : 'bad'}">${esc(q.state)}</span></td>
          <td>${esc(q.limited_since || '-')}</td></tr>`).join(''))}
      ${card('待处理事项', '系统关键状态',
        alerts.length
          ? `<div class="card-body flush">${alerts.map((a) =>
              `<div class="list-row"><div><div class="t">${esc(a.message)}</div>
               <div class="s">${esc(a.level)}</div></div>
               <span class="tag ${a.level === 'warn' ? 'warn' : 'idle'}">${esc(a.level)}</span></div>`).join('')}</div>`
          : `<div class="empty">当前没有待处理事项</div>`)}
    </div>`;
};

PAGES.acquire = async () => {
  const subs = await api('/api/mp/subscribes').catch(() => []);
  $('#view').innerHTML = `
    ${card('资源搜索', '识别媒体后订阅，或直接搜索站点资源推送下载',
      `<div class="card-body"><div class="toolbar">
         <input id="kw" placeholder="片名 / 关键词" style="flex:1;min-width:260px">
         <button class="btn primary" id="btn-media">识别媒体</button>
         <button class="btn" id="btn-tor">搜索站点资源</button>
       </div></div>`)}
    <div id="search-result"></div>
    ${tableCard('当前订阅', `${subs.length} 个`, ['名称', '年份', '类型', '季', '缺集', '状态', ''],
      subs.map((s) => `<tr><td>${esc(s.name)}</td><td>${esc(s.year)}</td><td>${esc(s.type)}</td>
        <td>${esc(s.season ?? '')}</td><td>${esc(s.lack_episode ?? '')}/${esc(s.total_episode ?? '')}</td>
        <td><span class="tag idle">${esc(s.state)}</span></td>
        <td><button class="btn sm danger" onclick="delSub(${s.id})">退订</button></td></tr>`).join(''))}`;
  $('#btn-media').onclick = searchMedia;
  $('#btn-tor').onclick = searchTorrents;
  $('#kw').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchMedia(); });
};
async function searchMedia() {
  const kw = $('#kw').value.trim();
  if (!kw) return toast('请输入关键词', 1);
  toast('识别中…');
  try {
    const ms = await api('/api/mp/media/search?keyword=' + encodeURIComponent(kw));
    $('#search-result').innerHTML = tableCard('识别结果', `${ms.length} 个候选`,
      ['标题', '年份', '类型', 'TMDB', ''],
      ms.map((m) => `<tr><td>${esc(m.title)}</td><td>${esc(m.year)}</td><td>${esc(m.type)}</td>
        <td>${esc(m.tmdb_id)}</td>
        <td><button class="btn sm primary" onclick="subscribe(${m.tmdb_id},'${esc(m.type)}')">订阅</button></td></tr>`).join(''));
  } catch (e) { toast('识别失败: ' + e.message, 1); }
}
async function searchTorrents() {
  const kw = $('#kw').value.trim();
  if (!kw) return toast('请输入关键词', 1);
  toast('站点搜索中，可能需要十几秒…');
  try {
    const ts = await api('/api/mp/torrents/search?keyword=' + encodeURIComponent(kw));
    $('#search-result').innerHTML = tableCard('站点资源', `${ts.length} 条`,
      ['标题', '站点', '清晰度', '体积', '做种', ''],
      ts.map((t) => `<tr><td>${esc((t.title || '').slice(0, 64))}</td><td>${esc(t.site)}</td>
        <td>${esc(t.resolution)}</td><td>${fmtBytes(t.size)}</td><td>${esc(t.seeders ?? '')}</td>
        <td><button class="btn sm primary" data-enc="${esc(t.enclosure)}" data-title="${esc(t.title)}"
             onclick="downloadTorrent(this)">下载</button></td></tr>`).join(''));
  } catch (e) { toast('搜索失败: ' + e.message, 1); }
}
async function subscribe(tmdbId, mediaType) {
  let season = null;
  if (mediaType && mediaType.includes('剧')) {
    const s = prompt('订阅第几季？（留空为默认）');
    if (s) season = parseInt(s, 10);
  }
  try {
    const r = await api('/api/mp/subscribes', { method: 'POST',
      body: JSON.stringify({ tmdb_id: tmdbId, media_type: mediaType, season }) });
    toast(r.ok ? '已订阅' : '订阅失败: ' + r.message, !r.ok);
    if (r.ok) renderPage('acquire');
  } catch (e) { toast('订阅失败: ' + e.message, 1); }
}
async function delSub(id) {
  if (!confirm('确认退订？')) return;
  try { await api('/api/mp/subscribes/' + id, { method: 'DELETE' }); toast('已退订'); renderPage('acquire'); }
  catch (e) { toast('退订失败: ' + e.message, 1); }
}
async function downloadTorrent(btn) {
  const enclosure = btn.dataset.enc, title = btn.dataset.title;
  if (!confirm('推送下载：' + title + '？')) return;
  try {
    const r = await api('/api/mp/download', { method: 'POST', body: JSON.stringify({ enclosure, title }) });
    toast(r.ok ? '已推送下载' : '失败: ' + r.message, !r.ok);
  } catch (e) { toast('下载失败: ' + e.message, 1); }
}

PAGES.downloads = async () => {
  const ds = await api('/api/mp/downloading').catch(() => []);
  $('#view').innerHTML = tableCard('下载任务', `${ds.length} 个进行中`,
    ['任务', '进度', '状态', '剩余'],
    ds.map((x) => {
      const p = Math.max(0, Math.min(100, x.progress || 0));
      return `<tr><td>${esc((x.title || '').slice(0, 70))}</td>
        <td><span class="bar"><i style="width:${p}%"></i></span> ${p.toFixed(1)}%</td>
        <td><span class="tag idle">${esc(x.state)}</span></td><td>${esc(x.left_time || '')}</td></tr>`;
    }).join(''));
};

PAGES.imports = async () => {
  const js = await api('/api/imports?limit=50').catch(() => []);
  $('#view').innerHTML = `
    ${card('新建导入', '提交网盘链接或云盘目录',
      `<div class="card-body"><div class="toolbar">
        <select id="imp-kind"><option value="drive-link">网盘链接</option><option value="cloud-drive">云盘目录</option></select>
        <input id="imp-src" placeholder="链接 / 目录引用" style="flex:1;min-width:240px">
        <input id="imp-cat" placeholder="分类(可选)" style="width:120px">
        <button class="btn primary" id="imp-go">提交</button>
      </div></div>`)}
    ${tableCard('导入任务', `${js.length} 个`, ['ID', '类型', '来源', '状态', '进度', ''],
      js.map((j) => {
        const p = Math.round((j.progress || 0) * 100);
        const cls = j.state === 'done' ? 'ok' : (j.state === 'failed' ? 'bad' : 'idle');
        return `<tr><td>${esc(j.id)}</td><td>${esc(j.kind)}</td>
          <td>${esc((j.source_ref || '').slice(0, 44))}</td>
          <td><span class="tag ${cls}">${esc(j.state)}</span></td>
          <td><span class="bar"><i style="width:${p}%"></i></span> ${j.items_done}/${j.items_total}</td>
          <td>${(j.state === 'queued' || j.state === 'running')
            ? `<button class="btn sm danger" onclick="cancelImport('${esc(j.id)}')">取消</button>` : ''}</td></tr>`;
      }).join(''))}`;
  $('#imp-go').onclick = submitImport;
};
async function submitImport() {
  const src = $('#imp-src').value.trim();
  if (!src) return toast('请输入来源', 1);
  try {
    await api('/api/imports', { method: 'POST', body: JSON.stringify({
      kind: $('#imp-kind').value, source_ref: src, category: $('#imp-cat').value.trim() }) });
    toast('任务已提交'); renderPage('imports');
  } catch (e) { toast('提交失败: ' + e.message, 1); }
}
async function cancelImport(id) {
  try { await api(`/api/imports/${id}/cancel`, { method: 'POST' }); toast('已取消'); renderPage('imports'); }
  catch (e) { toast('取消失败: ' + e.message, 1); }
}

PAGES.users = async () => {
  const us = await api('/api/emby/users').catch(() => []);
  const disabled = us.filter((u) => u.Policy && u.Policy.IsDisabled).length;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('☺', us.length, '用户总数', '媒体库账号')}
      ${stat('✓', us.length - disabled, '正常账号', '可正常登录')}
      ${stat('⊘', disabled, '已禁用', '停用中的账号')}
    </div>
    ${card('新建用户', '创建媒体库账号',
      `<div class="card-body"><div class="toolbar">
        <input id="nu" placeholder="用户名" style="min-width:220px">
        <button class="btn primary" id="nu-go">创建</button></div></div>`)}
    ${tableCard('用户列表', `${us.length} 个`, ['用户', '状态', ''],
      us.map((u) => {
        const dis = u.Policy && u.Policy.IsDisabled;
        return `<tr><td>${esc(u.Name)}</td>
          <td><span class="tag ${dis ? 'bad' : 'ok'}">${dis ? '已禁用' : '正常'}</span></td>
          <td><button class="btn sm" onclick="userCtl('${esc(u.Id)}','${dis ? 'enable' : 'disable'}')">${dis ? '启用' : '禁用'}</button>
              <button class="btn sm" onclick="resetPw('${esc(u.Id)}','${esc(u.Name)}')">改密</button></td></tr>`;
      }).join(''))}`;
  $('#nu-go').onclick = createUser;
};
async function createUser() {
  const name = $('#nu').value.trim();
  if (!name) return toast('请输入用户名', 1);
  try { await api('/api/emby/users', { method: 'POST', body: JSON.stringify({ name }) });
    toast('用户已创建'); renderPage('users');
  } catch (e) { toast('创建失败: ' + e.message, 1); }
}
async function userCtl(id, action) {
  try { await api(`/api/emby/users/${id}/${action}`, { method: 'POST' });
    toast(action === 'disable' ? '已禁用' : '已启用'); renderPage('users');
  } catch (e) { toast('操作失败: ' + e.message, 1); }
}
async function resetPw(id, name) {
  const pw = prompt(`为 ${name} 设置新密码（至少 6 位）`);
  if (!pw) return;
  try { await api(`/api/emby/users/${id}/password`, { method: 'POST',
    body: JSON.stringify({ new_password: pw }) }); toast('密码已更新');
  } catch (e) { toast('改密失败: ' + e.message, 1); }
}

PAGES.nodes = async () => {
  const [ns, log] = await Promise.all([
    api('/api/nodes').catch(() => []),
    api('/api/dispatch/log?limit=20').catch(() => []),
  ]);
  const online = ns.filter((n) => n.available).length;
  const streams = ns.reduce((a, n) => a + (n.active_streams || 0), 0);
  const egress = ns.reduce((a, n) => a + (n.egress_mbps || 0), 0);
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('⛁', `${online} / ${ns.length}`, '在线节点', '可用于分发')}
      ${stat('▶', streams, '活跃流', '所有节点合计')}
      ${stat('⇅', egress.toFixed(1), '出口 Mbps', '实时带宽')}
    </div>
    ${tableCard('推流节点', '按归一化负载分发', ['节点', '状态', '活跃流', '出口', '权重', '负载', ''],
      ns.map((n) => `<tr><td>${esc(n.name)}</td>
        <td><span class="tag ${n.available ? 'ok' : 'bad'}">${n.available ? '可用' : (n.manually_disabled ? '已下线' : '不健康')}</span></td>
        <td>${n.active_streams}</td><td>${n.egress_mbps} Mbps</td><td>${n.weight}</td><td>${n.normalized_load}</td>
        <td><button class="btn sm ${n.manually_disabled ? '' : 'danger'}"
            onclick="nodeCtl('${esc(n.name)}','${n.manually_disabled ? 'enable' : 'disable'}')">
            ${n.manually_disabled ? '上线' : '下线'}</button></td></tr>`).join(''))}
    ${tableCard('最近分发', '302 调度记录', ['时间', '节点', '负载', '候选', '请求'],
      log.slice().reverse().map((e) => `<tr><td>${new Date(e.ts * 1000).toLocaleTimeString()}</td>
        <td>${esc(e.node || '-')}</td><td>${esc(e.normalized_load ?? '-')}</td>
        <td>${esc(e.candidates)}</td><td>${esc((e.context || '').slice(0, 48))}</td></tr>`).join(''))}`;
};
async function nodeCtl(name, action) {
  try { await api(`/api/nodes/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
    toast(action === 'disable' ? '已下线' : '已上线'); renderPage('nodes');
  } catch (e) { toast('操作失败: ' + e.message, 1); }
}

PAGES.pipeline = async () => {
  const p = await api('/api/pipeline').catch(() => ({ available: false }));
  if (!p.available) { $('#view').innerHTML = `<div class="card"><div class="empty">管线快照不可用</div></div>`; return; }
  const d = p.data, f = d.fallback || {};
  const pct = f.capacity_bytes ? Math.round((f.bytes / f.capacity_bytes) * 100) : 0;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${(d.queues || []).map((q) => stat('⇄', q.items, q.name, `${fmtBytes(q.bytes)} · 最老 ${fmtAge(q.oldest_age_seconds)}`)).join('')}
      ${stat('⛃', fmtBytes(f.bytes), '本地应急仓', `${f.items || 0} 个文件 · ${pct}% 容量`)}
    </div>
    ${tableCard('上传身份配额', `快照 ${Math.round(p.snapshot_age_seconds)}s 前${p.stale ? ' · 已过期' : ''}`,
      ['身份', '状态', '受限起始'],
      (d.quota || []).map((q) => `<tr><td>${esc(q.identity)}</td>
        <td><span class="tag ${q.state === 'ok' ? 'ok' : 'bad'}">${esc(q.state)}</span></td>
        <td>${esc(q.limited_since || '-')}</td></tr>`).join(''))}
    ${card('告警', '管线异常', (d.alerts || []).length
      ? `<div class="card-body flush">${d.alerts.map((a) =>
          `<div class="list-row"><div class="t">${esc(a.message)}</div>
           <span class="tag ${a.level === 'warn' ? 'warn' : 'idle'}">${esc(a.level)}</span></div>`).join('')}</div>`
      : `<div class="empty">无告警</div>`)}`;
};

PAGES.update = async () => {
  const v = await api('/api/update/version').catch(() => ({ version: '?', commit: '?' }));
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('⟳', esc(v.version), '当前版本', 'commit ' + esc(v.commit))}
      ${stat('☁', '<span id="latest">—</span>', '最新版本', '来自代码仓库')}
    </div>
    ${card('版本更新', '更新时服务会自动重启，页面稍后自动刷新',
      `<div class="card-body"><div class="toolbar">
        <button class="btn" id="chk">检查更新</button>
        <button class="btn primary" id="apl" disabled>一键更新</button>
        <span id="upd-flag" class="muted">尚未检查</span>
      </div></div>`)}`;
  $('#chk').onclick = checkUpdate;
  $('#apl').onclick = applyUpdate;
  checkUpdate();
};
async function checkUpdate() {
  try {
    const c = await api('/api/update/check');
    $('#latest').textContent = c.latest || '-';
    $('#upd-flag').innerHTML = c.update_available
      ? '<span class="tag warn">有新版本可用</span>' : '<span class="tag ok">已是最新</span>';
    $('#apl').disabled = !c.update_available;
  } catch (e) { toast('检查失败: ' + e.message, 1); }
}
async function applyUpdate() {
  if (!confirm('确认更新？服务将自动重启。')) return;
  try {
    const r = await api('/api/update/apply', { method: 'POST', body: JSON.stringify({}) });
    toast(`正在更新到 ${r.target}，请稍候…`);
    setTimeout(() => location.reload(), 16000);
  } catch (e) { toast('更新失败: ' + e.message, 1); }
}

/* ---------------- boot ---------------- */
buildNav();
api('/api/update/version').then((v) => { $('#version').textContent = v.version; }).catch(() => {});
api('/api/whoami').then((w) => {
  $('#who').textContent = w.user;
  $('#who-initial').textContent = (w.user || '?').slice(0, 1).toUpperCase();
}).catch(() => {});
go((location.hash || '').replace('#/', '') || 'dashboard');
state.timer = setInterval(() => {
  if (state.page !== 'update') renderPage(state.page);
}, 30000);

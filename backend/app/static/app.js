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
    { id: 'library', icon: '▤', label: '媒体库', sub: '媒体库分布与条目统计' },
    { id: 'imports', icon: '⇪', label: '网盘上片', sub: '网盘链接与云盘目录导入' },
    { id: 'users', icon: '☺', label: '用户管理', sub: '账号、状态与密码' },
  ]},
  { group: '资源服务', items: [
    { id: 'nodes', icon: '⛁', label: '节点管理', sub: '推流节点负载与调度' },
    { id: 'pipeline', icon: '⇄', label: '管线状态', sub: '整理、上传队列与配额' },
    { id: 'mounts', icon: '⛃', label: '挂载管理', sub: '存储挂载健康与缓存占用' },
    { id: 'tasks', icon: '⏱', label: '调度中心', sub: '定时任务运行状态与失败追踪' },
  ]},
  { group: '系统管理', items: [
    { id: 'settings', icon: '⚙', label: '系统设置', sub: '对接 Emby、调度策略与节点配置' },
    { id: 'update', icon: '⟳', label: '版本更新', sub: '检查并应用新版本' },
  ]},
];

/* Sentinel understood by the backend as "keep the stored secret". Lets the
   operator edit a URL without re-typing the API key. */
const SECRET_KEEP = '__KEEP__';

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
  const [sessions, pipe, nodes, libs] = await Promise.all([
    api('/api/emby/sessions').catch(() => []),
    api('/api/pipeline').catch(() => ({ available: false })),
    api('/api/nodes').catch(() => []),
    api('/api/emby/libraries').catch(() => []),
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
      ${stat('▤', libs.length, '媒体库', libs.reduce((a, l) => a + (l.items || 0), 0) + ' 个条目')}
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

PAGES.library = async () => {
  const libs = await api('/api/emby/libraries').catch(() => []);
  const total = libs.reduce((a, l) => a + (l.items || 0), 0);
  const kinds = new Set(libs.map((l) => l.type)).size;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('▤', libs.length, '媒体库数量', '已配置的库')}
      ${stat('≡', total.toLocaleString(), '媒体条目', '电影与剧集合计')}
      ${stat('⛁', kinds, '库类型', '按内容类型划分')}
    </div>
    ${tableCard('媒体库', `${libs.length} 个库`, ['名称', '类型', '条目数', '存储位置'],
      libs.map((l) => `<tr><td>${esc(l.name)}</td>
        <td><span class="tag idle">${esc(l.type)}</span></td>
        <td>${l.items == null ? '<span class="muted">-</span>' : Number(l.items).toLocaleString()}</td>
        <td>${esc(l.locations)} 个路径</td></tr>`).join(''))}`;
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
  const [ns, log, dispatch] = await Promise.all([
    api('/api/nodes').catch(() => []),
    api('/api/dispatch/log?limit=20').catch(() => []),
    api('/api/settings/dispatch').catch(() => ({ policy: '-', load_threshold: 0 })),
  ]);
  const online = ns.filter((n) => n.available).length;
  const streams = ns.reduce((a, n) => a + (n.active_streams || 0), 0);
  const egress = ns.reduce((a, n) => a + (n.egress_mbps || 0), 0);
  const policyLabel = dispatch.policy === 'affinity' ? '文件亲和' : '最低负载';
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('⛁', `${online} / ${ns.length}`, '在线节点', '可用于分发')}
      ${stat('▶', streams, '活跃流', '所有节点合计')}
      ${stat('⇅', egress.toFixed(1), '出口 Mbps', '实时带宽')}
      ${stat('⚖', policyLabel, '调度策略', dispatch.policy === 'affinity'
        ? `占用率阈值 ${Math.round(dispatch.load_threshold * 100)}%` : '按容量占用率择优')}
    </div>
    ${card('新增节点', '节点上需运行 loadprobe 探针',
      `<div class="card-body"><div class="toolbar">
        <input id="nd-name" placeholder="节点名称" style="width:140px">
        <input id="nd-base" placeholder="对外地址 https://node.example.com" style="flex:1;min-width:220px">
        <input id="nd-probe" placeholder="探针地址 http://10.0.0.2:9800/load" style="flex:1;min-width:220px">
        <input id="nd-capacity" type="number" step="1" min="1" value="100" placeholder="并发容量" style="width:110px">
        <button class="btn primary" id="nd-go">添加</button>
      </div></div>`)}
    ${tableCard('推流节点', '按容量占用率分发', ['节点', '状态', '活跃流', '出口', '占用率', ''],
      ns.map((n) => `<tr><td>${esc(n.name)}</td>
        <td><span class="tag ${n.available ? 'ok' : 'bad'}">${n.available ? '可用' : (n.manually_disabled ? '已下线' : '不健康')}</span></td>
        <td>${n.active_streams} / ${esc(n.capacity)}</td><td>${n.egress_mbps} Mbps</td>
        <td>${Math.round((n.utilisation || 0) * 100)}%</td>
        <td><button class="btn sm ${n.manually_disabled ? '' : 'danger'}"
            onclick="nodeCtl('${esc(n.name)}','${n.manually_disabled ? 'enable' : 'disable'}')">
            ${n.manually_disabled ? '上线' : '下线'}</button>
            <button class="btn sm" onclick="editNodeCapacity('${esc(n.name)}',${n.capacity})">改容量</button>
            <button class="btn sm danger" onclick="deleteNode('${esc(n.name)}')">删除</button></td></tr>`).join(''))}
    ${tableCard('最近分发', '302 调度记录', ['时间', '节点', '负载', '候选', '策略', '请求'],
      log.slice().reverse().map((e) => `<tr><td>${new Date(e.ts * 1000).toLocaleTimeString()}</td>
        <td>${esc(e.node || '-')}</td>
        <td>${e.utilisation == null ? '-' : Math.round(e.utilisation * 100) + '%'}</td>
        <td>${esc(e.candidates)}</td>
        <td><span class="tag idle">${esc(e.reason || e.policy || '-')}</span></td>
        <td>${esc((e.context || '').slice(0, 48))}</td></tr>`).join(''))}`;
  $('#nd-go').onclick = addNode;
};
async function nodeCtl(name, action) {
  try { await api(`/api/nodes/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
    toast(action === 'disable' ? '已下线' : '已上线'); renderPage('nodes');
  } catch (e) { toast('操作失败: ' + e.message, 1); }
}
async function addNode() {
  const body = {
    name: $('#nd-name').value.trim(),
    base_url: $('#nd-base').value.trim(),
    probe_url: $('#nd-probe').value.trim(),
    capacity: parseFloat($('#nd-capacity').value) || 100,
  };
  if (!body.name || !body.base_url || !body.probe_url) return toast('请填写完整节点信息', 1);
  try {
    await api('/api/nodes', { method: 'POST', body: JSON.stringify(body) });
    toast('节点已添加'); renderPage('nodes');
  } catch (e) { toast('添加失败: ' + e.message, 1); }
}
async function editNodeCapacity(name, current) {
  const value = prompt(`设置 ${name} 的并发容量（该节点最多同时承载多少路播放）`, current);
  if (value === null) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ capacity: parseFloat(value) }) });
    toast('容量已更新'); renderPage('nodes');
  } catch (e) { toast('更新失败: ' + e.message, 1); }
}
async function deleteNode(name) {
  if (!confirm(`确认删除节点 ${name}？该节点将不再参与播放分发。`)) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast('节点已删除'); renderPage('nodes');
  } catch (e) { toast('删除失败: ' + e.message, 1); }
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

PAGES.mounts = async () => {
  const m = await api('/api/mounts').catch(() => ({ available: false }));
  if (!m.available) {
    $('#view').innerHTML = `<div class="card"><div class="empty">挂载快照不可用</div></div>`;
    return;
  }
  const d = m.data, ms = d.mounts || [];
  const alive = ms.filter((x) => x.alive).length;
  const stuck = ms.reduce((a, x) => a + (x.stuck_processes || 0), 0);
  const cache = ms.reduce((a, x) => a + (x.cache_bytes || 0), 0);
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('⛃', `${alive} / ${ms.length}`, '挂载存活', '可正常读取目录')}
      ${stat('⚠', stuck, '阻塞进程', stuck ? '存在不可中断 I/O' : '无卡死进程')}
      ${stat('⛁', fmtBytes(cache), '缓存占用', 'VFS 本地缓存合计')}
    </div>
    ${tableCard('存储挂载', `快照 ${Math.round(m.snapshot_age_seconds)}s 前${m.stale ? ' · 已过期' : ''}`,
      ['挂载', '类型', '状态', '探测耗时', '阻塞', '缓存', '可用空间'],
      ms.map((x) => {
        const cachePct = x.cache_limit_bytes
          ? Math.round((x.cache_bytes / x.cache_limit_bytes) * 100) : null;
        return `<tr><td>${esc(x.label)}<div class="s muted">${esc((x.options || []).join(','))}</div></td>
          <td>${esc(x.kind)}</td>
          <td><span class="tag ${x.alive ? 'ok' : 'bad'}">${x.alive ? '正常' : '异常'}</span></td>
          <td>${x.readdir_ms == null ? '<span class="muted">超时</span>' : x.readdir_ms + ' ms'}</td>
          <td>${x.stuck_processes ? `<span class="tag bad">${x.stuck_processes}</span>` : '<span class="muted">0</span>'}</td>
          <td>${x.cache_bytes == null ? '<span class="muted">-</span>'
            : `${fmtBytes(x.cache_bytes)}${cachePct == null ? '' : ` <span class="muted">(${cachePct}%)</span>`}`}</td>
          <td>${x.fs_free_bytes == null ? '<span class="muted">-</span>' : fmtBytes(x.fs_free_bytes)}</td></tr>`;
      }).join(''))}
    ${card('告警', '存储层异常', (d.alerts || []).length
      ? `<div class="card-body flush">${d.alerts.map((a) =>
          `<div class="list-row"><div class="t">${esc(a.message)}</div>
           <span class="tag ${a.level === 'warn' ? 'warn' : 'bad'}">${esc(a.level)}</span></div>`).join('')}</div>`
      : `<div class="empty">无告警</div>`)}`;
};

PAGES.tasks = async () => {
  const t = await api('/api/tasks').catch(() => ({ available: false }));
  if (!t.available) {
    $('#view').innerHTML = `<div class="card"><div class="empty">调度快照不可用</div></div>`;
    return;
  }
  const d = t.data, ts = d.tasks || [];
  const failed = ts.filter((x) => x.last_status === 'failed').length;
  const disabled = ts.filter((x) => !x.enabled).length;
  const statusCls = (st) => (st === 'ok' ? 'ok' : st === 'failed' ? 'bad'
    : st === 'unknown' ? 'idle' : 'warn');
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('⏱', ts.length, '任务总数', '快照中的定时任务')}
      ${stat('⚠', failed, '当前失败', failed ? '最近一次运行失败' : '全部正常')}
      ${stat('⏸', disabled, '已禁用', '未纳入调度')}
    </div>
    ${tableCard('定时任务', `快照 ${Math.round(t.snapshot_age_seconds)}s 前${t.stale ? ' · 已过期' : ''}`,
      ['任务名', '计划', '状态', '上次运行', '耗时', '连续失败'],
      ts.map((x) => {
        const ageSec = x.last_run ? (Date.now() / 1000) - x.last_run : null;
        const age = ageSec == null ? '-' : fmtAge(ageSec) + '前';
        const dur = x.last_duration_ms == null
          ? '<span class="muted">-</span>' : `${esc(x.last_duration_ms)} ms`;
        const streak = x.failure_streak > 0
          ? `<span class="tag bad">${esc(x.failure_streak)}</span>`
          : '<span class="muted">0</span>';
        const st = x.last_status || 'unknown';
        return `<tr><td>${esc(x.name)}${x.enabled ? '' : '<div class="s muted">已禁用</div>'}</td>
          <td>${esc(x.schedule)}</td>
          <td><span class="tag ${statusCls(st)}">${esc(st)}</span></td>
          <td>${esc(age)}</td>
          <td>${dur}</td>
          <td>${streak}</td></tr>`;
      }).join(''))}
    ${card('告警', '调度异常', (d.alerts || []).length
      ? `<div class="card-body flush">${d.alerts.map((a) =>
          `<div class="list-row"><div class="t">${esc(a.message)}</div>
           <span class="tag ${a.level === 'warn' ? 'warn' : 'bad'}">${esc(a.level)}</span></div>`).join('')}</div>`
      : `<div class="empty">无告警</div>`)}`;
};

PAGES.settings = async () => {
  const s = await api('/api/settings').catch(() => null);
  if (!s) { $('#view').innerHTML = `<div class="card"><div class="empty">设置加载失败</div></div>`; return; }
  const e = s.emby, d = s.dispatch, p = s.playback;
  const connected = e.enabled && e.api_key_set;
  $('#view').innerHTML = `
    ${s.mock_mode ? card('演示模式', '当前以 MEDIADECK_MOCK=1 运行',
      `<div class="card-body"><div class="muted">所有数据均为模拟值，保存的配置不会连接真实服务。</div></div>`) : ''}
    ${card('Emby 对接', connected ? '已连接' : '尚未连接 — 面板的用户管理与媒体库依赖此配置',
      `<div class="card-body">
        <div class="form-row"><label>服务器地址</label>
          <input id="em-url" value="${esc(e.url)}" placeholder="http://127.0.0.1:8096"></div>
        <div class="form-row"><label>API Key</label>
          <input id="em-key" type="password" placeholder="${e.api_key_set ? esc(e.api_key_masked) + '（留空则不修改）' : '在 Emby 后台「高级 → API 密钥」创建'}"></div>
        <div class="form-row"><label>请求超时</label>
          <input id="em-timeout" type="number" min="1" max="120" value="${esc(e.timeout_seconds)}" style="width:110px"> <span class="muted">秒</span></div>
        <div class="form-row"><label>启用集成</label>
          <input id="em-enabled" type="checkbox" ${e.enabled ? 'checked' : ''}>
          <span class="muted">关闭后面板不再调用 Emby</span></div>
        <div class="form-row"><label>校验证书</label>
          <input id="em-verify" type="checkbox" ${e.verify_ssl ? 'checked' : ''}>
          <span class="muted">自签名证书请取消勾选</span></div>
        <div class="toolbar">
          <button class="btn" id="em-test">测试连接</button>
          <button class="btn primary" id="em-save">保存</button>
          <span id="em-result" class="muted">${connected ? '已配置' : '未配置'}</span>
        </div>
      </div>`)}
    ${card('播放调度策略', '决定同一个文件由哪个推流节点承载',
      `<div class="card-body">
        <div class="form-row"><label>策略</label>
          <select id="dp-policy" style="min-width:220px">
            <option value="affinity" ${d.policy === 'affinity' ? 'selected' : ''}>文件亲和（推荐）</option>
            <option value="least-load" ${d.policy === 'least-load' ? 'selected' : ''}>最低负载</option>
          </select></div>
        <div class="form-row"><label>负载阈值</label>
          <input id="dp-threshold" type="number" step="0.05" min="0.05" max="1" value="${esc(d.load_threshold)}" style="width:110px">
          <span class="muted">节点容量占用率超过此值时改派其他节点（0.8 = 80%）</span></div>
        <div class="muted" style="margin:6px 0 10px">
          文件亲和：同一个文件固定由同一节点服务，只需缓存一份，回源流量不翻倍；
          节点故障或过载时自动顺延。最低负载：每次都挑当前最闲的节点，会导致多节点重复缓存同一文件。
        </div>
        <div class="toolbar"><button class="btn primary" id="dp-save">保存策略</button>
          <span id="dp-result" class="muted"></span></div>
      </div>`)}
    ${card('播放分流（Emby 接管）', p.enabled ? '已启用 — 客户端播放会被 302 分发到推流节点'
      : '未启用 — 当前所有播放仍由 Emby 主机直接吐流',
      `<div class="card-body">
        <div class="muted" style="margin-bottom:12px">
          开启后，客户端的播放请求经面板按文件亲和分发到节点。
          <b>转码、无法识别的条目、无可用节点时会自动回退由 Emby 直接提供</b>，不会因为面板出错导致放不了。
        </div>
        <div class="form-row"><label>启用分流</label>
          <input id="pb-enabled" type="checkbox" ${p.enabled ? 'checked' : ''}>
          <span class="muted">需先配置至少一个推流节点</span></div>
        <div class="form-row"><label>仅直播</label>
          <input id="pb-direct" type="checkbox" ${p.direct_only ? 'checked' : ''}>
          <span class="muted">转码流由 Emby 主机生成，节点上没有，建议保持勾选</span></div>
        <div class="form-row"><label>去除前缀</label>
          <input id="pb-strip" value="${esc(p.strip_prefix)}" placeholder="/media"></div>
        <div class="form-row"><label>路径模板</label>
          <input id="pb-template" value="${esc(p.path_template)}" placeholder="{path}"></div>
        <div class="muted" style="margin:0 0 10px">
          Emby 看到的是 <code>/media/Movies/x.mkv</code>，节点上可能是另一个根目录。
          去除 <code>/media</code> 后套用模板得到节点侧路径；模板必须包含 <code>{path}</code>。
        </div>
        <div class="toolbar">
          <input id="pb-item" placeholder="填入 Emby ItemId 试算" style="width:200px">
          <button class="btn" id="pb-preview">预览路径</button>
          <button class="btn primary" id="pb-save">保存</button>
        </div>
        <div id="pb-result" class="muted" style="margin-top:10px"></div>
      </div>`)}
    ${tableCard('推流节点', `${s.nodes.length} 个已配置 · 在「节点管理」中新增与监控`,
      ['名称', '对外地址', '探针地址', '并发容量', '状态'],
      s.nodes.map((n) => `<tr><td>${esc(n.name)}</td><td>${esc(n.base_url)}</td>
        <td>${esc(n.probe_url)}</td><td>${esc(n.capacity)}</td>
        <td><span class="tag ${n.enabled ? 'ok' : 'idle'}">${n.enabled ? '启用' : '停用'}</span></td></tr>`).join(''))}`;
  $('#em-save').onclick = saveEmby;
  $('#em-test').onclick = testEmby;
  $('#dp-save').onclick = saveDispatch;
  $('#pb-save').onclick = savePlayback;
  $('#pb-preview').onclick = previewPlayback;
};
function playbackPayload() {
  return {
    enabled: $('#pb-enabled').checked,
    direct_only: $('#pb-direct').checked,
    strip_prefix: $('#pb-strip').value.trim(),
    path_template: $('#pb-template').value.trim() || '{path}',
  };
}
async function savePlayback() {
  try {
    await api('/api/settings/playback', { method: 'PUT', body: JSON.stringify(playbackPayload()) });
    toast('播放分流配置已保存');
    renderPage('settings');
  } catch (err) { toast('保存失败: ' + err.message, 1); }
}
async function previewPlayback() {
  const id = $('#pb-item').value.trim();
  const el = $('#pb-result');
  if (!id) { el.innerHTML = '<span class="tag warn">请先填入 ItemId</span>'; return; }
  el.textContent = '正在试算…';
  try {
    const r = await api(`/api/playback/preview?item_id=${encodeURIComponent(id)}`);
    el.innerHTML = r.redirected
      ? `<span class="tag ok">分流到 ${esc(r.node)}</span><br>
         <span class="muted">Emby 路径</span> <code>${esc(r.media_path)}</code><br>
         <span class="muted">节点 URL</span> <code>${esc(r.target)}</code>`
      : `<span class="tag warn">不分流（${esc(r.reason)}）</span><br>
         <span class="muted">将由 Emby 直接提供</span> <code>${esc(r.target)}</code>`;
  } catch (err) {
    el.innerHTML = `<span class="tag bad">试算失败</span> ${esc(err.message)}`;
  }
}
function embyPayload() {
  const key = $('#em-key').value;
  return {
    url: $('#em-url').value.trim(),
    // Empty field means "keep what is stored", so editing the URL alone is safe.
    api_key: key === '' ? SECRET_KEEP : key,
    timeout_seconds: parseFloat($('#em-timeout').value) || 15,
    enabled: $('#em-enabled').checked,
    verify_ssl: $('#em-verify').checked,
  };
}
async function saveEmby() {
  try {
    await api('/api/settings/emby', { method: 'PUT', body: JSON.stringify(embyPayload()) });
    toast('Emby 配置已保存，立即生效');
    renderPage('settings');
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function testEmby() {
  const el = $('#em-result');
  el.textContent = '正在测试…';
  try {
    const r = await api('/api/settings/emby/test', {
      method: 'POST', body: JSON.stringify(embyPayload()) });
    el.innerHTML = `<span class="tag ok">连接成功</span> ${esc(r.server_name || '')} ${esc(r.version || '')}`;
  } catch (e) {
    el.innerHTML = `<span class="tag bad">连接失败</span> ${esc(e.message)}`;
  }
}
async function saveDispatch() {
  try {
    await api('/api/settings/dispatch', { method: 'PUT', body: JSON.stringify({
      policy: $('#dp-policy').value,
      load_threshold: parseFloat($('#dp-threshold').value) || 0.8,
    }) });
    $('#dp-result').innerHTML = '<span class="tag ok">已保存</span>';
    toast('调度策略已生效');
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}

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

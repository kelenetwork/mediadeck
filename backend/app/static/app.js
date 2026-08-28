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
  connectLive(page);
}
async function renderPage(page, manual, live) {
  const fn = PAGES[page];
  if (!fn) { $('#view').innerHTML = '<div class="empty">页面不存在</div>'; return; }
  try {
    await fn();
    $('#last-updated').textContent = '最近更新: ' + new Date().toLocaleTimeString();
    if (manual) toast('已刷新');
  } catch (e) {
    // A push-triggered re-render must stay silent: the operator did not ask
    // for it, so a toast on every transient failure would be noise.
    if (!live) toast('加载失败: ' + e.message, 1);
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
  const [ns, log, dispatch, st] = await Promise.all([
    api('/api/nodes').catch(() => []),
    api('/api/dispatch/log?limit=20').catch(() => []),
    api('/api/settings/dispatch').catch(() => ({ policy: '-', load_threshold: 0 })),
    api('/api/settings').catch(() => ({ integration: {} })),
  ]);
  state.nodes = ns;
  const online = ns.filter((n) => n.available).length;
  const streams = ns.reduce((a, n) => a + (n.active_streams || 0), 0);
  const egress = ns.reduce((a, n) => a + (n.egress_mbps || 0), 0);
  const policyLabel = dispatch.policy === 'affinity' ? '文件亲和' : '最低负载';
  const panelSet = !!(st.integration || {}).panel_public_url;

  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('⛁', `${online} / ${ns.length}`, '在线节点', '可用于分发')}
      ${stat('▶', streams, '活跃流', '所有节点合计')}
      ${stat('⇅', egress.toFixed(1), '出口 Mbps', '实时带宽')}
      ${stat('⚖', policyLabel, '调度策略', dispatch.policy === 'affinity'
        ? `占用率阈值 ${Math.round(dispatch.load_threshold * 100)}%` : '按容量占用率择优')}
    </div>
    ${panelSet ? '' : card('⚠ 尚未填写面板对外地址', '节点安装时需要用它回连面板取配置',
      `<div class="card-body"><div class="muted">请先到「系统设置 → 接入方式」填写面板对外地址，否则无法生成节点安装命令。</div></div>`)}
    ${card('新增节点', '先在面板登记，再用生成的一条命令去装机',
      `<div class="card-body"><div class="toolbar">
        <input id="nd-name" placeholder="节点名称 如 ca1" style="width:130px">
        <input id="nd-base" placeholder="对外地址 https://ca1.example.com" style="flex:1;min-width:220px">
        <input id="nd-probe" placeholder="探针地址 http://127.0.0.1:9800/load" style="flex:1;min-width:200px">
        <input id="nd-capacity" type="number" min="1" value="100" style="width:100px" title="并发容量">
        <button class="btn primary" id="nd-go">添加</button>
      </div>
      <div class="muted" style="margin-top:8px">添加后展开该节点填写「媒体根」，再复制安装命令到新机器执行。</div>
      </div>`)}
    ${ns.length ? ns.map(nodeCard).join('') : card('推流节点', '尚未配置',
      '<div class="card-body"><div class="empty">还没有节点</div></div>')}
    ${tableCard('最近分发', '302 调度记录', ['时间', '节点', '占用', '候选', '策略', '请求'],
      log.slice().reverse().map((e) => `<tr><td>${new Date(e.ts * 1000).toLocaleTimeString()}</td>
        <td>${esc(e.node || '-')}</td>
        <td>${e.utilisation == null ? '-' : Math.round(e.utilisation * 100) + '%'}</td>
        <td>${esc(e.candidates)}</td>
        <td><span class="tag idle">${esc(e.reason || e.policy || '-')}</span></td>
        <td>${esc((e.context || '').slice(0, 44))}</td></tr>`).join(''))}`;
  $('#nd-go').onclick = addNode;
};

/* 一个节点 = 一张卡：健康、媒体根、缓存、签名、安装命令全在这里。
   这些都是「这台机器」的属性，放全局设置里是错的。 */
function nodeCard(n) {
  const pools = n.pools || [];
  const health = n.available ? '<span class="tag ok">可用</span>'
    : (n.manually_disabled ? '<span class="tag idle">已下线</span>'
                           : '<span class="tag bad">不健康</span>');
  const poolRows = pools.length
    ? pools.map((p, i) => `<tr>
        <td>${esc(p.name)}</td><td><code>${esc(p.emby_prefix)}</code></td>
        <td><code>${esc(p.node_path)}</code></td>
        <td><code>${esc(p.rclone_remote)}</code></td>
        <td><code>${esc(p.url_prefix)}</code></td>
        <td><button class="btn sm danger" onclick="delPool('${esc(n.name)}',${i})">删除</button></td>
      </tr>`).join('')
    : '';
  return card(`⛁ ${n.name}`,
    `${n.active_streams}/${n.capacity} 路 · ${Math.round((n.utilisation || 0) * 100)}% · ${n.egress_mbps} Mbps`,
    `<div class="card-body">
      <div class="toolbar" style="margin-bottom:10px">
        ${health}
        <span class="muted">${esc(n.base_url)}</span>
        <span style="flex:1"></span>
        <button class="btn sm" onclick="nodeCtl('${esc(n.name)}','${n.manually_disabled ? 'enable' : 'disable'}')">${n.manually_disabled ? '上线' : '下线'}</button>
        <button class="btn sm" onclick="editNodeCapacity('${esc(n.name)}',${n.capacity})">改容量</button>
        <button class="btn sm danger" onclick="deleteNode('${esc(n.name)}')">删除</button>
      </div>

      <div class="sub" style="margin:12px 0 4px"><b>媒体根映射</b> — Emby 里的路径对应节点上的哪个目录</div>
      ${poolRows
        ? `<table><thead><tr><th>名称</th><th>Emby 路径</th><th>节点路径</th><th>rclone remote</th><th>URL 前缀</th><th></th></tr></thead><tbody>${poolRows}</tbody></table>`
        : '<div class="empty">未配置媒体根 — 该节点当前无法提供任何文件</div>'}
      <div class="toolbar" style="margin-top:8px">
        <input id="pl-name-${esc(n.name)}" placeholder="名称 main" style="width:90px">
        <input id="pl-emby-${esc(n.name)}" placeholder="Emby 路径 /media" style="width:150px">
        <input id="pl-path-${esc(n.name)}" placeholder="节点路径 /mnt/gdrive/Media" style="flex:1;min-width:170px">
        <input id="pl-remote-${esc(n.name)}" placeholder="remote rc2:Media" style="width:150px">
        <input id="pl-url-${esc(n.name)}" placeholder="URL 前缀 /s/main" style="width:120px">
        <button class="btn" onclick="addPool('${esc(n.name)}')">添加媒体根</button>
      </div>

      <div class="sub" style="margin:14px 0 4px"><b>存储与安全</b></div>
      <div class="form-row"><label>缓存目录</label>
        <input id="nc-dir-${esc(n.name)}" value="${esc(n.cache_dir || '')}" placeholder="/var/cache/mediadeck"></div>
      <div class="form-row"><label>缓存上限</label>
        <input id="nc-size-${esc(n.name)}" value="${esc(n.cache_size || '')}" placeholder="2T" style="width:120px">
        <span class="muted">别超过该盘可用空间</span></div>
      <div class="form-row"><label>签名密钥</label>
        <span class="${n.sign_secret_set ? 'tag ok' : 'tag bad'}">${n.sign_secret_set ? '已设置' : '未设置 · 链接永久公开'}</span>
        <span class="muted">${esc(n.sign_secret_masked || '')}</span>
        <button class="btn sm" onclick="rotateNodeSecret('${esc(n.name)}')">重置密钥</button></div>
      <div class="form-row"><label>签名参数</label>
        <input id="nc-argd-${esc(n.name)}" value="${esc(n.sign_arg_digest || 'md5')}" style="width:90px">
        <input id="nc-arge-${esc(n.name)}" value="${esc(n.sign_arg_expires || 'expires')}" style="width:110px">
        <span class="muted">节点 nginx 用的参数名（已有站点常用 k / e）</span></div>
      <div class="form-row"><label>链接有效期</label>
        <input id="nc-ttl-${esc(n.name)}" type="number" min="60" value="${esc(n.sign_ttl_seconds || 21600)}" style="width:120px">
        <span class="muted">秒</span></div>
      <div class="form-row"><label>rclone 配置</label>
        <span class="${n.rclone_conf_set ? 'tag ok' : 'tag warn'}">${n.rclone_conf_set ? '已保存' : '未保存'}</span>
        <button class="btn sm" onclick="editRcloneConf('${esc(n.name)}')">粘贴 rclone.conf</button>
        <span class="muted">保存后装机时自动写入，无需在节点上跑 rclone config</span></div>
      <div class="toolbar">
        <button class="btn primary" onclick="saveNodeStorage('${esc(n.name)}')">保存</button>
        <button class="btn" onclick="showEnroll('${esc(n.name)}')">获取安装命令</button>
      </div>
      <div id="enroll-${esc(n.name)}" style="margin-top:10px"></div>
    </div>`);
}

async function addPool(name) {
  const g = (k) => ($(`#pl-${k}-${CSS.escape(name)}`) || {}).value || '';
  const node = (state.nodes || []).find((n) => n.name === name);
  if (!node) return;
  const pools = (node.pools || []).slice();
  pools.push({
    name: g('name').trim(), emby_prefix: g('emby').trim(),
    node_path: g('path').trim(), rclone_remote: g('remote').trim(),
    url_prefix: g('url').trim() || ('/s/' + g('name').trim()),
  });
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ pools }) });
    toast('媒体根已添加'); renderPage('nodes');
  } catch (e) { toast('添加失败: ' + e.message, 1); }
}
async function delPool(name, index) {
  const node = (state.nodes || []).find((n) => n.name === name);
  if (!node) return;
  const pools = (node.pools || []).filter((_, i) => i !== index);
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ pools }) });
    toast('已删除'); renderPage('nodes');
  } catch (e) { toast('删除失败: ' + e.message, 1); }
}
async function saveNodeStorage(name) {
  const g = (k) => ($(`#nc-${k}-${CSS.escape(name)}`) || {}).value || '';
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({
      cache_dir: g('dir').trim(), cache_size: g('size').trim(),
      sign_arg_digest: g('argd').trim(), sign_arg_expires: g('arge').trim(),
      sign_ttl_seconds: parseInt(g('ttl'), 10) || 21600,
    }) });
    toast('已保存'); renderPage('nodes');
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function rotateNodeSecret(name) {
  if (!confirm(`重置 ${name} 的签名密钥？\n\n已发出的播放链接会立即失效，且必须重新在节点上执行安装命令。`)) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}/rotate-secret`, { method: 'POST' });
    toast('密钥已重置，请重新部署该节点'); renderPage('nodes');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function editRcloneConf(name) {
  const text = prompt(`粘贴该节点使用的 rclone.conf 全文\n（建议为节点单独建 OAuth 身份，避免和主机抢配额）`);
  if (text === null) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ rclone_conf: text }) });
    toast('已保存'); renderPage('nodes');
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function showEnroll(name) {
  const box = $(`#enroll-${CSS.escape(name)}`);
  box.textContent = '生成中…';
  try {
    const r = await api(`/api/nodes/${encodeURIComponent(name)}/enroll`);
    box.innerHTML = `
      <div class="muted" style="margin-bottom:6px">在这台新机器上以 root 执行（其余配置面板已自动带过去）：</div>
      <pre class="codeblock">${esc(r.command)}</pre>
      ${(r.warnings || []).map((w) => `<div class="tag warn" style="margin-top:6px">${esc(w)}</div>`).join('')}
      <div class="muted" style="margin-top:6px">前置：DNS 中该节点域名解析到本机且为 DNS-only（灰云），视频不能走 CDN 代理。</div>`;
  } catch (e) {
    box.innerHTML = `<span class="tag bad">生成失败</span> ${esc(e.message)}`;
  }
}
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
    toast('节点已添加，请继续配置媒体根'); renderPage('nodes');
  } catch (e) { toast('添加失败: ' + e.message, 1); }
}
async function editNodeCapacity(name, current) {
  const value = prompt(`设置 ${name} 的并发容量（最多同时承载多少路播放）`, current);
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
  const e = s.emby, d = s.dispatch, p = s.playback, ig = s.integration;
  const connected = e.enabled && e.api_key_set;
  const mapped = (s.nodes || []).filter((n) => (n.pools || []).length).length;
  $('#view').innerHTML = `
    ${s.mock_mode ? card('演示模式', '当前以 MEDIADECK_MOCK=1 运行',
      `<div class="card-body"><div class="muted">所有数据均为模拟值，保存的配置不会连接真实服务。</div></div>`) : ''}
    ${card('Emby 对接', connected ? '已连接' : '尚未连接 — 用户管理与媒体库依赖此配置',
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
    ${card('接入方式', '你现有的 Emby 域名如何把播放分发到节点',
      `<div class="card-body">
        <div class="muted" style="margin-bottom:12px">
          用户仍然只访问原来的 Emby 地址，客户端不用改任何设置。
          只需在反代里把<b>播放请求</b>转给面板；Web 界面、刮削、图片、转码照旧直接走 Emby。
          <b>面板地址</b>同时也是节点装机时回连取配置的地址，必须填。
        </div>
        <div class="form-row"><label>面板地址</label>
          <input id="ig-panel" value="${esc(ig.panel_public_url)}" placeholder="https://deck.example.com"></div>
        <div class="form-row"><label>Emby 地址</label>
          <input id="ig-emby" value="${esc(ig.emby_public_url)}" placeholder="https://emby.example.com"></div>
        <div class="toolbar">
          <select id="ig-server" style="width:120px">
            <option value="caddy">Caddy</option><option value="nginx">nginx</option>
          </select>
          <button class="btn" id="ig-show">生成反代配置</button>
          <button class="btn primary" id="ig-save">保存</button>
        </div>
        <div id="ig-out" style="margin-top:10px"></div>
      </div>`)}
    ${card('播放调度策略', '决定同一个文件由哪个推流节点承载',
      `<div class="card-body">
        <div class="form-row"><label>策略</label>
          <select id="dp-policy" style="min-width:200px">
            <option value="affinity" ${d.policy === 'affinity' ? 'selected' : ''}>文件亲和（推荐）</option>
            <option value="least-load" ${d.policy === 'least-load' ? 'selected' : ''}>最低负载</option>
          </select></div>
        <div class="form-row"><label>负载阈值</label>
          <input id="dp-threshold" type="number" step="0.05" min="0.05" max="1" value="${esc(d.load_threshold)}" style="width:110px">
          <span class="muted">节点容量占用率超过此值时改派其他节点（0.8 = 80%）</span></div>
        <div class="muted" style="margin:6px 0 10px">
          文件亲和：同一个文件固定由同一节点服务，只缓存一份，回源流量不翻倍；节点故障或过载时自动顺延。
        </div>
        <div class="toolbar"><button class="btn primary" id="dp-save">保存策略</button>
          <span id="dp-result" class="muted"></span></div>
      </div>`)}
    ${card('播放分流（Emby 接管）', p.enabled ? '已启用 — 客户端播放会被 302 分发到推流节点'
      : '未启用 — 当前所有播放仍由 Emby 主机直接吐流',
      `<div class="card-body">
        <div class="muted" style="margin-bottom:12px">
          开启后客户端播放请求经面板按文件亲和分发到节点。
          <b>转码、无法识别的条目、没有能提供该文件的节点时都会自动回退由 Emby 直供</b>，不会因面板出错导致放不了。
        </div>
        <div class="form-row"><label>启用分流</label>
          <input id="pb-enabled" type="checkbox" ${p.enabled ? 'checked' : ''}>
          <span class="muted">已有 ${mapped} 个节点配置了媒体根</span></div>
        <div class="form-row"><label>仅直播</label>
          <input id="pb-direct" type="checkbox" ${p.direct_only ? 'checked' : ''}>
          <span class="muted">转码流由 Emby 主机生成，节点上没有，建议保持勾选</span></div>
        <div class="muted" style="margin:0 0 10px">
          <b>路径映射已移到「节点管理」的每个节点里</b> —— 不同节点挂载的目录和网盘身份都可能不同，
          放在全局会导致一台机器上有的库能放、有的库 404。
        </div>
        <div class="toolbar">
          <input id="pb-item" placeholder="填入 Emby ItemId 试算" style="width:190px">
          <button class="btn" id="pb-preview">预览路径</button>
          <button class="btn primary" id="pb-save">保存</button>
        </div>
        <div id="pb-result" class="muted" style="margin-top:10px"></div>
      </div>`)}
    ${tableCard('推流节点', `${s.nodes.length} 个已配置 · 媒体根、缓存、签名密钥都在「节点管理」里按节点配置`,
      ['名称', '对外地址', '媒体根', '签名', '状态'],
      s.nodes.map((n) => `<tr><td>${esc(n.name)}</td><td>${esc(n.base_url)}</td>
        <td>${(n.pools || []).length ? (n.pools || []).map((x) => esc(x.emby_prefix)).join(', ')
          : '<span class="tag bad">未配置</span>'}</td>
        <td>${n.sign_secret_set ? '<span class="tag ok">已设置</span>' : '<span class="tag bad">未设置</span>'}</td>
        <td><span class="tag ${n.enabled ? 'ok' : 'idle'}">${n.enabled ? '启用' : '停用'}</span></td></tr>`).join(''))}`;
  $('#em-save').onclick = saveEmby;
  $('#em-test').onclick = testEmby;
  $('#dp-save').onclick = saveDispatch;
  $('#pb-save').onclick = savePlayback;
  $('#pb-preview').onclick = previewPlayback;
  $('#ig-save').onclick = saveIntegration;
  $('#ig-show').onclick = showFrontendConfig;
};
async function saveIntegration() {
  try {
    await api('/api/settings/integration', { method: 'PUT', body: JSON.stringify({
      panel_public_url: $('#ig-panel').value.trim(),
      emby_public_url: $('#ig-emby').value.trim(),
    }) });
    toast('接入配置已保存'); renderPage('settings');
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function showFrontendConfig() {
  const el = $('#ig-out');
  el.textContent = '生成中…';
  try {
    const r = await api(`/api/integration/frontend?server=${encodeURIComponent($('#ig-server').value)}`);
    el.innerHTML = `<div class="muted" style="margin-bottom:6px">把下面配置加到你的反代，然后 reload：</div>
      <pre class="codeblock">${esc(r.config)}</pre>`;
  } catch (e) { el.innerHTML = `<span class="tag bad">生成失败</span> ${esc(e.message)}`; }
}
function playbackPayload() {
  return {
    enabled: $('#pb-enabled').checked,
    direct_only: $('#pb-direct').checked,
  };
}
async function savePlayback() {
  try {
    await api('/api/settings/playback', { method: 'PUT', body: JSON.stringify(playbackPayload()) });
    toast('播放分流配置已保存'); renderPage('settings');
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
      ? `<span class="tag ok">分流到 ${esc(r.node)} · ${esc(r.pool)}</span>
         ${r.signed ? '<span class="tag ok">已签名</span>' : '<span class="tag bad">未签名</span>'}<br>
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
    api_key: key === '' ? SECRET_KEEP : key,
    timeout_seconds: parseFloat($('#em-timeout').value) || 15,
    enabled: $('#em-enabled').checked,
    verify_ssl: $('#em-verify').checked,
  };
}
async function saveEmby() {
  try {
    await api('/api/settings/emby', { method: 'PUT', body: JSON.stringify(embyPayload()) });
    toast('Emby 配置已保存，立即生效'); renderPage('settings');
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

/* ---------------- live updates ---------------- */
/* Polling every 30s was wrong in both directions: a stream starting now stayed
   invisible for up to 30s, while an idle panel hammered Emby forever -- and the
   periodic re-render wiped whatever the operator was typing. The server now
   pushes snapshots over SSE and only when something actually changed. */
const LIVE = {
  dashboard: ['nodes', 'sessions', 'pipeline'],
  nodes: ['nodes'],
  pipeline: ['pipeline'],
  tasks: ['tasks'],
  mounts: ['mounts'],
};
const live = { src: null, data: {}, page: null, retry: 0 };

function setLiveState(ok) {
  const el = $('#live-state');
  if (!el) return;
  el.className = 'tag ' + (ok ? 'ok' : 'idle');
  el.textContent = ok ? '实时' : '重连中';
}

function connectLive(page) {
  const topics = LIVE[page];
  if (live.src) { live.src.close(); live.src = null; }
  live.page = page;
  live.data = {};
  if (!topics) { setLiveState(false); return; }

  const src = new EventSource(`/api/stream?topics=${topics.join(',')}`);
  live.src = src;
  src.onopen = () => { live.retry = 0; setLiveState(true); };
  topics.forEach((topic) => {
    src.addEventListener(topic, (ev) => {
      try { live.data[topic] = JSON.parse(ev.data); } catch (e) { return; }
      setLiveState(true);
      // Re-render only the page that asked for this data, and never while a
      // form on it is focused -- that is what used to eat keystrokes.
      if (live.page === state.page && !isEditing()) {
        renderPage(state.page, false, true);
      }
    });
  });
  src.onerror = () => {
    setLiveState(false);
    src.close();
    live.src = null;
    // EventSource retries on its own, but only for transport errors; an auth
    // or proxy failure needs an explicit backoff so we do not spin.
    live.retry = Math.min(live.retry + 1, 6);
    setTimeout(() => { if (live.page === state.page) connectLive(state.page); },
               1000 * live.retry);
  };
}

function isEditing() {
  const el = document.activeElement;
  if (!el) return false;
  const tag = (el.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'select' || tag === 'textarea';
}

/* ---------------- boot ---------------- */
buildNav();
api('/api/update/version').then((v) => { $('#version').textContent = v.version; }).catch(() => {});
api('/api/whoami').then((w) => {
  $('#who').textContent = w.user;
  $('#who-initial').textContent = (w.user || '?').slice(0, 1).toUpperCase();
}).catch(() => {});
go((location.hash || '').replace('#/', '') || 'dashboard');

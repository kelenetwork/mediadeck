"""Node provisioning — turn a bare server into a streaming node.

The panel could previously *register* a node, but nothing told the operator how
to actually build one.  Registering a node that does not exist yet produces
404s, which is worse than not having the feature.

A node needs five things, and every one of them has a detail that silently
breaks playback if it is wrong:

1. **rclone remote + mount** — the node must expose byte-identical files at a
   known root.  Different remote, different bytes, and range requests return
   the wrong data.
2. **VFS cache on a big disk** — streaming without a cache re-reads the cloud
   for every seek.
3. **nginx with secure_link** — serve files with signed, expiring URLs and
   correct range support, or every link handed to a client is a permanent
   public download.
4. **Direct TLS, DNS-only** — video must not traverse the CDN proxy.
5. **loadprobe agent** — otherwise the scheduler cannot see load and the node
   is never selected.

This module renders those as concrete, copy-pasteable config from the values
already stored in the panel, so the operator does not have to translate an
architecture description into shell commands by hand.

Nothing here executes anything or touches a remote host: it emits text the
operator reviews and runs. Secrets appear only in the rendered output the
authenticated operator explicitly requested.
"""
from __future__ import annotations

from urllib.parse import urlparse

LOADPROBE_PORT = 9800


def _host_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.hostname or ""


def nginx_site(node_name: str, base_url: str, media_root: str,
               signing_enabled: bool, secret: str) -> str:
    """nginx vhost serving the media root, optionally with signed URLs.

    ``secure_link_md5`` is computed over the *decoded* ``$uri``; using the
    encoded form breaks every path containing a space or CJK character, which
    is most of a Chinese media library.
    """
    host = _host_of(base_url) or f"{node_name}.example.com"
    media_root = media_root.rstrip("/") or "/srv/media"

    if signing_enabled:
        secure = f"""
        # Signed URLs: the panel generates ?md5=&expires=; nginx verifies here.
        # The digest must match the panel's: md5("<expires><decoded-uri> <secret>")
        secure_link $arg_md5,$arg_expires;
        secure_link_md5 "$secure_link_expires$uri {secret}";

        if ($secure_link = "") {{ return 403; }}   # bad or missing signature
        if ($secure_link = "0") {{ return 410; }}  # signature valid but expired
"""
    else:
        secure = """
        # WARNING: signing is disabled. Any URL handed to a client is a
        # permanent public download link. Enable signing in the panel before
        # exposing this node to untrusted users.
"""

    return f"""# /etc/nginx/sites-available/mediadeck-{node_name}
# Serves media for streaming node "{node_name}".
server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {host};

    # Direct TLS. This hostname must be DNS-only (grey cloud): video traffic
    # must not go through the CDN proxy.
    ssl_certificate     /etc/letsencrypt/live/{host}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{host}/privkey.pem;

    # Media is large and already compressed; buffering it wastes RAM and adds
    # latency to seeks.
    sendfile           on;
    tcp_nopush         on;
    aio                threads;
    directio           16m;
    output_buffers     2 1m;
    proxy_buffering    off;
    keepalive_timeout  300s;
    send_timeout       300s;

    access_log /var/log/nginx/mediadeck-{node_name}.access.log;
    error_log  /var/log/nginx/mediadeck-{node_name}.error.log warn;

    location / {{
{secure}
        root {media_root};

        # Emby clients seek constantly; byte ranges are mandatory.
        add_header Accept-Ranges bytes;
        add_header X-Mediadeck-Node "{node_name}" always;

        # A directory listing would expose the whole library.
        autoindex off;

        try_files $uri =404;
    }}

    location = /healthz {{
        access_log off;
        return 200 "ok\\n";
    }}
}}
"""


def rclone_mount_unit(node_name: str, remote: str, media_root: str,
                      cache_dir: str, cache_size: str) -> str:
    media_root = media_root.rstrip("/") or "/srv/media"
    return f"""# /etc/systemd/system/mediadeck-mount-{node_name}.service
# Read-only mount that backs the media root served by nginx.
[Unit]
Description=mediadeck media mount ({node_name})
After=network-online.target
Wants=network-online.target
# nginx must not start serving before the files are actually there, or it
# caches 404s for the whole library.
Before=nginx.service

[Service]
Type=notify
ExecStartPre=/bin/mkdir -p {media_root} {cache_dir}
ExecStart=/usr/bin/rclone mount {remote}: {media_root} \\
    --config /root/.config/rclone/rclone.conf \\
    --allow-other \\
    --read-only \\
    --dir-cache-time 72h \\
    --poll-interval 15s \\
    --vfs-cache-mode full \\
    --vfs-cache-max-size {cache_size} \\
    --vfs-cache-max-age 168h \\
    --vfs-read-chunk-size 32M \\
    --vfs-read-chunk-size-limit 1G \\
    --vfs-read-ahead 256M \\
    --buffer-size 64M \\
    --cache-dir {cache_dir} \\
    --umask 022 \\
    --log-level INFO
ExecStop=/bin/fusermount3 -uz {media_root}
Restart=on-failure
RestartSec=10
# A stale FUSE mount wedges every reader in uninterruptible sleep.
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


def loadprobe_unit(node_name: str, token: str = "") -> str:
    token_line = f'Environment=LOADPROBE_TOKEN={token}\n' if token else ""
    return f"""# /etc/systemd/system/mediadeck-loadprobe.service
# Reports stream count and egress to the panel; without it the scheduler
# cannot see this node's load and will never dispatch to it.
[Unit]
Description=mediadeck load probe ({node_name})
After=network-online.target

[Service]
Type=simple
{token_line}ExecStart=/usr/bin/python3 /opt/mediadeck-agent/loadprobe.py --port {LOADPROBE_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install_script(node_name: str, base_url: str, media_root: str,
                   remote: str, cache_dir: str, cache_size: str,
                   panel_url: str, signing_enabled: bool, secret: str = "") -> str:
    """Render a one-shot installer that turns a bare server into a node.

    Emitted as text for the operator to review and run; nothing is executed
    here and no remote host is touched.
    """
    host = _host_of(base_url) or f"{node_name}.example.com"
    media_root = media_root.rstrip("/") or "/srv/media"
    panel_url = panel_url.rstrip("/") or "http://PANEL-ADDRESS"
    sign_note = ("已启用签名：nginx 会校验 ?md5= 与 ?expires=，过期链接自动失效"
                 if signing_enabled else
                 "⚠ 未启用签名：任何人拿到链接都能永久下载，请先在面板开启签名")

    site = nginx_site(node_name, base_url, media_root, signing_enabled, secret)
    mount = rclone_mount_unit(node_name, remote, media_root, cache_dir, cache_size)
    probe = loadprobe_unit(node_name)

    return f"""#!/bin/bash
# ===========================================================
# mediadeck 节点安装脚本 — 节点 "{node_name}"
# ===========================================================
# 在这台【新的推流服务器】上以 root 执行。
# 不会碰你的 Emby 主机，也不会改动已有媒体数据。
#
# 签名状态：{sign_note}
#
# 执行前请确认：
#   1. DNS 中 {host} 已解析到本机，且为 DNS-only（灰云）
#      —— 视频流不能走 CDN 代理，否则会被限速/中断
#   2. {cache_dir} 所在磁盘剩余空间 > {cache_size}
#   3. 本机能访问面板 {panel_url}
# ===========================================================
set -euo pipefail

echo "==> 1/6 安装依赖"
apt-get update -qq
apt-get install -y -qq nginx python3 fuse3 curl certbot python3-certbot-nginx

echo "==> 2/6 安装 rclone"
command -v rclone >/dev/null || curl -fsSL https://rclone.org/install.sh | bash

echo "==> 3/6 检查 rclone remote «{remote}»"
# 节点必须能读到与主机【完全相同】的文件。
# 建议为节点单独创建 OAuth 身份，避免与主机争抢网盘 API 配额。
mkdir -p /root/.config/rclone
if ! rclone listremotes --config /root/.config/rclone/rclone.conf 2>/dev/null | grep -qx '{remote}:'; then
  echo "    !! 未找到 remote «{remote}»"
  echo "    !! 请先执行 rclone config 创建，或从主机复制配置："
  echo "    !!   scp 主机:/root/.config/rclone/rclone.conf /root/.config/rclone/"
  exit 1
fi
chmod 600 /root/.config/rclone/rclone.conf

echo "==> 4/6 配置挂载与缓存"
mkdir -p {media_root} {cache_dir} /opt/mediadeck-agent
# allow_other 必须开启，否则 nginx(www-data) 读不到挂载内容 → 全站 403
grep -q '^user_allow_other' /etc/fuse.conf || echo 'user_allow_other' >> /etc/fuse.conf
cat > /etc/systemd/system/mediadeck-mount-{node_name}.service <<'MEDIADECK_MOUNT_EOF'
{mount}
MEDIADECK_MOUNT_EOF
systemctl daemon-reload
systemctl enable --now mediadeck-mount-{node_name}.service
sleep 5
if mountpoint -q {media_root}; then
  echo "    挂载正常：$(ls {media_root} | head -3 | tr '\\n' ' ')"
else
  echo "    !! 挂载失败，请查看： journalctl -u mediadeck-mount-{node_name} -n 50"
  exit 1
fi

echo "==> 5/6 配置 nginx 与证书"
cat > /etc/nginx/sites-available/mediadeck-{node_name} <<'MEDIADECK_NGINX_EOF'
{site}
MEDIADECK_NGINX_EOF
ln -sf /etc/nginx/sites-available/mediadeck-{node_name} /etc/nginx/sites-enabled/
# 先取证书：证书不存在时 nginx 会因 ssl_certificate 缺失而启动失败
if [ ! -d /etc/letsencrypt/live/{host} ]; then
  certbot certonly --nginx -d {host} --non-interactive --agree-tos \\
    --register-unsafely-without-email || {{
      echo "    !! 证书申请失败：请确认 {host} 已指向本机且 80 端口可达"
      exit 1
    }}
fi
nginx -t && systemctl reload nginx

echo "==> 6/6 安装负载探针"
curl -fsSL {panel_url}/agent/loadprobe.py -o /opt/mediadeck-agent/loadprobe.py
cat > /etc/systemd/system/mediadeck-loadprobe.service <<'MEDIADECK_PROBE_EOF'
{probe}
MEDIADECK_PROBE_EOF
systemctl daemon-reload
systemctl enable --now mediadeck-loadprobe.service
sleep 2

echo
echo "==> 自检"
curl -fsS http://127.0.0.1:{LOADPROBE_PORT}/load >/dev/null && echo "    [OK] 负载探针" || echo "    [!!] 探针未响应"
curl -fsS https://{host}/healthz >/dev/null && echo "    [OK] nginx 对外服务" || echo "    [!!] nginx 未响应"
echo
echo "==========================================================="
echo "安装完成。回到面板「节点管理」新增节点，填写："
echo "    节点名称   : {node_name}"
echo "    对外地址   : {base_url}"
echo "    探针地址   : http://<本机内网IP>:{LOADPROBE_PORT}/load"
echo "    并发容量   : 按本机上行带宽估算（如 1Gbps ≈ 40 路 4K）"
echo "==========================================================="
"""


def emby_frontend_snippet(panel_url: str, emby_url: str, server: str = "caddy") -> str:
    """Reverse-proxy rule that puts the panel on the real playback path.

    This is the piece that answers "how does my existing Emby domain dispatch
    to nodes": the operator keeps one public Emby hostname, and only the
    stream requests are handed to the panel.  Everything else — the web UI,
    metadata, images, transcoding — must keep going straight to Emby.
    """
    panel_host = panel_url.rstrip("/") or "http://127.0.0.1:8300"
    emby_host = emby_url.rstrip("/") or "http://127.0.0.1:8096"
    emby_domain = _host_of(emby_url) or "emby.example.com"

    if server == "nginx":
        return f"""# nginx — 在 {emby_domain} 的 server 块里加这一段
# 放在 location / 之前；只有直连播放请求交给面板，其它全部照旧走 Emby。

# 只匹配视频流请求，不影响 Web 界面、图片、转码
location ~ ^/emby/Videos/[^/]+/(stream|original) {{
    proxy_pass {panel_host};
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

    # 面板返回 302 指向节点，必须原样传给客户端，不能让 nginx 自己跟随
    proxy_redirect off;
    proxy_intercept_errors off;
}}

location / {{
    proxy_pass {emby_host};
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
}}
"""

    return f"""# Caddy — {emby_domain} 的站点配置
# 只有直连播放请求交给面板；Web 界面、刮削、图片、转码仍然直接走 Emby。
{emby_domain} {{
    # 匹配视频流请求
    @stream path_regexp stream ^/emby/Videos/[^/]+/(stream|original)

    handle @stream {{
        # 面板会判断能否分流：可以就 302 到节点，不能就 302 回 Emby。
        # 任何异常情况都会回落到 Emby，不会导致播放失败。
        reverse_proxy {panel_host}
    }}

    handle {{
        reverse_proxy {emby_host}
    }}
}}
"""

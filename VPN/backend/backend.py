"""Backend logic for subscriptions and user storage."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config" / "Server_config.json"
USERS_DIR = ROOT_DIR / "users"
XRAY_CONFIG_DIR = ROOT_DIR / "config"
LOGS_DIR = ROOT_DIR / "logs"


def load_config() -> dict:
    # Allow UTF-8 BOM in config file (common on Windows).
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def _get_server(cfg: dict, server_id: str) -> dict:
    for s in cfg.get("servers", []):
        if s.get("id") == server_id:
            return s
    raise ValueError(f"Server not found: {server_id}")


def get_server_by_id(cfg: dict, server_id: str) -> dict | None:
    for s in cfg.get("servers", []):
        if s.get("id") == server_id:
            return s
    return None


def get_sub_base_url(cfg: dict, server: dict) -> str:
    base = str(cfg.get("sub_base_url", "")).strip()
    if base:
        return base.rstrip("/")

    host = str(server.get("host", "")).strip()
    port = cfg.get("sub_port") or server.get("sub_port") or server.get("port")
    scheme = str(server.get("sub_scheme") or cfg.get("sub_scheme") or "http").strip()
    if not host or not port:
        raise ValueError("sub_base_url is empty and server host/port is missing")
    path_prefix = str(cfg.get("sub_path_prefix") or "/api/sub").strip()
    if not path_prefix.startswith("/"):
        path_prefix = "/" + path_prefix
    return f"{scheme}://{host}:{port}{path_prefix}"


def ensure_users_dir() -> None:
    USERS_DIR.mkdir(parents=True, exist_ok=True)


def user_path(user_id: int) -> Path:
    return USERS_DIR / f"{user_id}.json"


def _default_user(user_id: int) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "created_at": int(time.time()),
        "clients": [],
        "subscriptions": [],
        "active_sub_id": None,
        "sub_id": None,
        "notes": "",
    }


def load_user(user_id: int) -> dict[str, Any]:
    ensure_users_dir()
    path = user_path(user_id)
    if not path.exists():
        return _default_user(user_id)
    return json.loads(path.read_text(encoding="utf-8"))


def save_user(user: dict[str, Any]) -> None:
    ensure_users_dir()
    path = user_path(int(user["user_id"]))
    path.write_text(json.dumps(user, ensure_ascii=False, indent=2), encoding="utf-8")


def _subscription_active(sub: dict[str, Any]) -> bool:
    expires_at = sub.get("expires_at")
    if not expires_at:
        return True
    try:
        return int(expires_at) > int(time.time())
    except Exception:
        return False


def _find_client(user: dict[str, Any], server_id: str) -> dict[str, Any] | None:
    for c in user.get("clients", []):
        if c.get("server_id") == server_id:
            return c
    return None


def _ensure_client(user: dict[str, Any], server_id: str) -> dict[str, Any]:
    existing = _find_client(user, server_id)
    if existing:
        return existing
    return _create_client(user, server_id)


def _create_client(user: dict[str, Any], server_id: str) -> dict[str, Any]:
    client = {
        "server_id": server_id,
        "client_id": str(uuid.uuid4()),
        "created_at": int(time.time()),
    }
    user.setdefault("clients", []).append(client)
    return client


def _server_label(cfg: dict, server: dict) -> str:
    flag = str(server.get("flag") or "").strip()
    name = str(server.get("name") or server.get("id") or "server").strip()
    base = " ".join([p for p in [flag, name] if p])
    return base or name


def _normalize_mtproto_secret(secret: str, prefix: str = "") -> str:
    cleaned = str(secret or "").strip().lower()
    cleaned = re.sub(r"\s+", "", cleaned)
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if not cleaned:
        return ""
    if not re.fullmatch(r"[0-9a-f]+", cleaned):
        return ""
    if len(cleaned) % 2 != 0:
        return ""
    if len(cleaned) == 32 and prefix in {"dd", "ee"}:
        cleaned = f"{prefix}{cleaned}"
    if len(cleaned) < 32:
        return ""
    return cleaned


def get_mtproto_proxy_for_server(cfg: dict, server_id: str) -> dict[str, Any] | None:
    server = get_server_by_id(cfg, server_id)
    if not server:
        return None

    raw = server.get("telegram_mtproto") or {}
    if not isinstance(raw, dict):
        return None
    if not bool(raw.get("enabled", False)):
        return None

    host = str(raw.get("host") or server.get("host") or "").strip()
    if not host:
        return None

    try:
        port = int(raw.get("port") or 443)
    except Exception:
        return None
    if port <= 0 or port > 65535:
        return None

    secret_prefix = str(raw.get("secret_prefix") or "").strip().lower()
    secret = _normalize_mtproto_secret(str(raw.get("secret") or ""), prefix=secret_prefix)
    if not secret:
        return None

    tg_link = f"tg://proxy?server={host}&port={port}&secret={secret}"
    web_link = f"https://t.me/proxy?server={host}&port={port}&secret={secret}"
    return {
        "server_id": server_id,
        "server_name": _server_label(cfg, server),
        "host": host,
        "port": port,
        "secret": secret,
        "tg_link": tg_link,
        "web_link": web_link,
    }


def _server_safe_label(cfg: dict, server: dict) -> str:
    brand = str(cfg.get("brand_name") or "HMAO-VPN").strip()
    code = str(server.get("region") or server.get("id") or "").upper()
    code = re.sub(r"[^A-Z0-9-]+", "", code)
    base = " ".join([p for p in [brand, code] if p])
    return base or brand




def _normalize_reality_short_id(value: str) -> str:
    sid = str(value or "").strip().lower()
    sid = re.sub(r"[^0-9a-f]", "", sid)
    if len(sid) % 2 != 0:
        sid = sid[:-1]
    return sid[:16]


def _get_reality_profile(server: dict[str, Any]) -> dict[str, Any] | None:
    security = str(server.get("security") or "none").strip().lower()
    if security != "reality":
        return None

    raw = server.get("reality") or {}
    if not isinstance(raw, dict):
        raw = {}

    server_name = str(
        raw.get("server_name")
        or raw.get("sni")
        or server.get("reality_server_name")
        or ""
    ).strip()

    public_key = str(raw.get("public_key") or server.get("reality_public_key") or "").strip()
    private_key = str(raw.get("private_key") or server.get("reality_private_key") or "").strip()

    short_id = _normalize_reality_short_id(
        str(raw.get("short_id") or server.get("reality_short_id") or "")
    )
    fingerprint = str(raw.get("fingerprint") or server.get("reality_fingerprint") or "chrome").strip()
    spider_x = str(raw.get("spider_x") or server.get("reality_spider_x") or "/").strip() or "/"
    flow = str(raw.get("flow") or server.get("reality_flow") or "").strip()

    dest = str(raw.get("dest") or "").strip()
    if not dest and server_name:
        dest = f"{server_name}:443"

    alpn_raw = raw.get("alpn")
    alpn: list[str] = []
    if isinstance(alpn_raw, list):
        alpn = [str(v).strip() for v in alpn_raw if str(v).strip()]

    return {
        "server_name": server_name,
        "public_key": public_key,
        "private_key": private_key,
        "short_id": short_id,
        "fingerprint": fingerprint or "chrome",
        "spider_x": spider_x,
        "flow": flow,
        "dest": dest,
        "alpn": alpn,
    }

def _build_vless_link(
    server: dict, client_id: str, cfg: dict | None = None, safe_name: bool = False
) -> str:
    host = str(server.get("host", "")).strip()
    port = server.get("port")
    if not host or not port:
        raise ValueError("Server host/port is missing")

    cfg = cfg or load_config()
    if safe_name:
        display_name = _server_safe_label(cfg, server)
    else:
        brand = str(cfg.get("brand_name") or "").strip()
        base_name = _server_label(cfg, server)
        display_name = f"{brand} | {base_name}" if brand else base_name
    name_enc = quote(display_name, safe="")

    network = str(server.get("network") or "tcp").strip().lower()
    security = str(server.get("security") or "none").strip().lower()

    params: list[tuple[str, str]] = [
        ("encryption", "none"),
        ("security", security),
        ("type", network),
    ]

    if security == "reality":
        reality = _get_reality_profile(server)
        if not reality:
            raise ValueError("REALITY settings are missing")
        if not reality.get("server_name"):
            raise ValueError("REALITY server_name/sni is missing")
        if not reality.get("public_key"):
            raise ValueError("REALITY public_key is missing")

        params.append(("sni", str(reality["server_name"])))
        params.append(("fp", str(reality.get("fingerprint") or "chrome")))
        params.append(("pbk", str(reality["public_key"])))

        sid = str(reality.get("short_id") or "")
        if sid:
            params.append(("sid", sid))

        spx = str(reality.get("spider_x") or "")
        if spx:
            params.append(("spx", spx))

        flow = str(reality.get("flow") or "")
        if flow:
            params.append(("flow", flow))

        alpn = reality.get("alpn") or []
        if isinstance(alpn, list) and alpn:
            params.append(("alpn", ",".join(str(v).strip() for v in alpn if str(v).strip())))

    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params if str(v) != "")
    return f"vless://{client_id}@{host}:{port}?{query}#{name_enc}"


def build_sub_url(cfg: dict, server_id: str, sub_id: str) -> str:
    # Prefer global base URL if present, so old subscriptions stay readable
    # even when their original server entry was removed from config.
    base = str(cfg.get("sub_base_url", "")).strip()
    if base:
        return f"{base.rstrip('/')}/{sub_id}"

    server = _get_server(cfg, server_id)
    base_url = get_sub_base_url(cfg, server)
    return f"{base_url}/{sub_id}"


def list_user_subscriptions(user_id: int) -> list[dict[str, Any]]:
    user = load_user(user_id)
    cfg = load_config()
    result: list[dict[str, Any]] = []
    for sub in user.get("subscriptions", []):
        server_id = sub.get("server_id")
        if not server_id:
            continue
        server = get_server_by_id(cfg, server_id)
        server_name = _server_label(cfg, server) if server else server_id
        sub_id = str(sub.get("sub_id", ""))
        client_id = str(sub.get("client_id", ""))
        sub_url = build_sub_url(cfg, server_id, sub_id) if sub_id else ""
        vless_link = ""
        if server and client_id:
            try:
                vless_link = _build_vless_link(server, client_id, cfg)
            except ValueError:
                vless_link = ""
        telegram_proxy = get_mtproto_proxy_for_server(cfg, server_id) if server else None
        active = _subscription_active(sub)
        result.append(
            {
                "sub_id": sub_id,
                "server_id": server_id,
                "server_name": server_name,
                "client_id": client_id,
                "created_at": sub.get("created_at"),
                "months": sub.get("months"),
                "minutes": sub.get("minutes"),
                "expires_at": sub.get("expires_at"),
                "active": active,
                "status": "active" if active else "expired",
                "sub_url": sub_url,
                "vless_link": vless_link,
                "telegram_proxy": telegram_proxy,
            }
        )
    return result


def _collect_clients_for_server(server_id: str) -> list[dict[str, Any]]:
    ensure_users_dir()
    clients: list[dict[str, Any]] = []
    for path in USERS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        active_ids: set[str] = set()
        for sub in data.get("subscriptions", []):
            if sub.get("server_id") != server_id:
                continue
            if not _subscription_active(sub):
                continue
            client_id = sub.get("client_id")
            if client_id:
                active_ids.add(str(client_id))
        for client_id in active_ids:
            clients.append({"id": client_id})
    return clients


def _build_log_config(cfg: dict, server: dict, server_id: str) -> dict:
    mode = str(server.get("log_mode") or cfg.get("log_mode") or "windows").strip().lower()
    if mode in {"none", "minimal", "off"}:
        return {"loglevel": "warning"}
    if mode == "linux":
        log_dir = str(server.get("log_dir") or cfg.get("linux_log_dir") or "/var/log/xray").strip()
        log_dir = log_dir.rstrip("/") or "/var/log/xray"
        return {
            "loglevel": "warning",
            "access": f"{log_dir}/xray_{server_id}_access.log",
            "error": f"{log_dir}/xray_{server_id}_error.log",
        }
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "loglevel": "info",
        "access": str((ROOT_DIR / "logs" / f"xray_{server_id}_access.log").resolve()),
        "error": str((ROOT_DIR / "logs" / f"xray_{server_id}_error.log").resolve()),
    }


def write_xray_config(server_id: str) -> Path:
    cfg = load_config()
    server = _get_server(cfg, server_id)
    clients = _collect_clients_for_server(server_id)

    log_cfg = _build_log_config(cfg, server, server_id)
    inbound_port = server.get("port")
    listen = str(server.get("listen") or "0.0.0.0").strip()
    network = str(server.get("network") or "tcp").strip().lower()
    security = str(server.get("security") or "none").strip().lower()

    stream_settings: dict[str, Any] = {"network": network, "security": security}

    if security == "reality":
        reality = _get_reality_profile(server)
        if not reality:
            raise ValueError(f"REALITY profile is missing for server {server_id}")
        if not reality.get("server_name"):
            raise ValueError(f"REALITY server_name/sni is missing for server {server_id}")
        if not reality.get("private_key"):
            raise ValueError(f"REALITY private_key is missing for server {server_id}")

        short_ids = [str(reality.get("short_id") or "")] if str(reality.get("short_id") or "") else [""]

        stream_settings["realitySettings"] = {
            "show": False,
            "dest": str(reality.get("dest") or f"{reality['server_name']}:443"),
            "xver": 0,
            "serverNames": [str(reality["server_name"])],
            "privateKey": str(reality["private_key"]),
            "shortIds": short_ids,
        }

        flow = str(reality.get("flow") or "")
        if flow:
            clients = [{"id": str(c.get("id")), "flow": flow} for c in clients if c.get("id")]

    inbound = {
        "tag": f"in-{server_id}",
        "listen": listen,
        "port": inbound_port,
        "protocol": server.get("protocol") or "vless",
        "settings": {"clients": clients, "decryption": "none"},
        "streamSettings": stream_settings,
    }

    outbound = {"protocol": "freedom", "settings": {}}
    domain_strategy = server.get("outbound_domain_strategy") or cfg.get("outbound_domain_strategy")
    if domain_strategy:
        outbound["domainStrategy"] = domain_strategy

    config = {
        "log": log_cfg,
        "inbounds": [inbound],
        "outbounds": [outbound],
    }

    dns_servers = server.get("dns_servers") or cfg.get("dns_servers")
    if dns_servers:
        config["dns"] = {"servers": dns_servers}

    XRAY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = XRAY_CONFIG_DIR / f"xray_{server_id}.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _find_user_by_sub_id(sub_id: str) -> dict[str, Any] | None:
    ensure_users_dir()
    for path in USERS_DIR.glob("*.json"):
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(user.get("sub_id") or "") == sub_id:
            return user
    return None


def _find_subscription(sub_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    ensure_users_dir()
    for path in USERS_DIR.glob("*.json"):
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for sub in user.get("subscriptions", []):
            if sub.get("sub_id") == sub_id:
                return user, sub
    return None


def _build_subscription_links(sub_id: str, safe_name: bool = False) -> list[str]:
    user = _find_user_by_sub_id(sub_id)
    if user:
        cfg = load_config()
        links: list[str] = []
        changed = False
        for sub in user.get("subscriptions", []):
            if not _subscription_active(sub):
                continue
            server_id = sub.get("server_id")
            if not server_id:
                continue
            server = get_server_by_id(cfg, str(server_id))
            if not server:
                # Server was removed from config (e.g. location replacement).
                # Skip stale subscription instead of breaking full /sub payload.
                continue
            client_id = sub.get("client_id")
            if not client_id:
                client = _ensure_client(user, server_id)
                client_id = client["client_id"]
                sub["client_id"] = client_id
                changed = True
            try:
                link = _build_vless_link(server, str(client_id), cfg, safe_name=safe_name)
            except ValueError:
                # Skip broken server config so one bad node does not kill full /sub output.
                continue
            links.append(link)
        if changed:
            save_user(user)
        if not links:
            raise PermissionError("subscription expired")
        return links

    found = _find_subscription(sub_id)
    if not found:
        raise KeyError("subscription not found")
    user, sub = found
    if not _subscription_active(sub):
        raise PermissionError("subscription expired")

    cfg = load_config()
    server_id = sub.get("server_id")
    if not server_id:
        raise KeyError("subscription has no server_id")
    server = get_server_by_id(cfg, str(server_id))
    if not server:
        raise KeyError("subscription server not found")

    client_id = sub.get("client_id")
    if not client_id:
        client = _ensure_client(user, server_id)
        save_user(user)
        client_id = client["client_id"]
    return [_build_vless_link(server, str(client_id), cfg, safe_name=safe_name)]


def _format_subscription_payload(links: list[str], fmt: str) -> bytes:
    text = "\n".join(links) + ("\n" if links else "")
    if fmt == "plain":
        return text.encode("utf-8")
    return base64.b64encode(text.encode("utf-8"))


def run_sub_server(bind: str | None = None, port: int | None = None) -> None:
    cfg = load_config()
    bind = bind or str(cfg.get("sub_bind") or "0.0.0.0").strip()
    port = int(port or cfg.get("sub_port") or 8081)
    path_prefix = str(cfg.get("sub_path_prefix") or "/api/sub").strip()
    import_path = str(cfg.get("sub_import_path") or "/import").strip()
    if not path_prefix.startswith("/"):
        path_prefix = "/" + path_prefix
    if not import_path.startswith("/"):
        import_path = "/" + import_path

    class Handler(BaseHTTPRequestHandler):
        def handle(self) -> None:
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError):
                return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == import_path:
                qs = parse_qs(parsed.query)
                raw_url = (qs.get("url", [""])[0] or "").strip()
                if not raw_url.startswith(("http://", "https://")):
                    self.send_response(400)
                    self.end_headers()
                    return
                redirect = f"v2raytun://import/{quote(raw_url, safe='')}"
                self.send_response(302)
                self.send_header("Location", redirect)
                self.end_headers()
                return

            if not parsed.path.startswith(path_prefix + "/"):
                self.send_response(404)
                self.end_headers()
                return

            sub_id = parsed.path[len(path_prefix) + 1 :]
            if not sub_id:
                self.send_response(404)
                self.end_headers()
                return

            qs = parse_qs(parsed.query)
            fmt = (qs.get("format", ["base64"])[0] or "base64").lower()
            if fmt not in {"base64", "plain"}:
                fmt = "base64"
            safe = (qs.get("safe", ["0"])[0] or "0").lower() in {"1", "true", "yes"}

            try:
                links = _build_subscription_links(sub_id, safe_name=safe)
            except PermissionError:
                self.send_response(403)
                self.end_headers()
                return
            except KeyError:
                self.send_response(404)
                self.end_headers()
                return

            payload = _format_subscription_payload(links, fmt)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = HTTPServer((bind, port), Handler)
    print(f"Sub server listening on {bind}:{port} (prefix {path_prefix})")
    server.serve_forever()


def create_subscription_for_user(
    user: dict[str, Any],
    server_id: str,
    months: int | None = None,
    minutes: int | None = None,
    expires_at: int | None = None,
) -> dict[str, Any]:
    """Create a subscription using a preloaded user dict (no I/O)."""
    cfg = load_config()
    server = _get_server(cfg, server_id)

    existing_sub = None
    for _s in user.get("subscriptions", []):
        if _s.get("sub_id"):
            existing_sub = _s.get("sub_id")
            break
    sub_id = str(user.get("sub_id") or existing_sub or uuid.uuid4())
    user["sub_id"] = sub_id
    now = int(time.time())

    client = _create_client(user, server_id)

    sub = {
        "sub_id": sub_id,
        "server_id": server_id,
        "client_id": client["client_id"],
        "created_at": now,
        "months": months,
        "minutes": minutes,
        "expires_at": None,
    }
    if expires_at:
        sub["expires_at"] = int(expires_at)
    elif minutes:
        sub["expires_at"] = now + int(minutes) * 60
    elif months:
        # Simple month length approximation for MVP (30 days).
        sub["expires_at"] = now + int(months) * 30 * 24 * 60 * 60

    user["subscriptions"].append(sub)
    user["active_sub_id"] = sub_id

    vless_link = _build_vless_link(server, client["client_id"], cfg)
    sub_url = build_sub_url(cfg, server_id, sub_id)
    telegram_proxy = get_mtproto_proxy_for_server(cfg, server_id)
    return {"url": sub_url, "sub": sub, "vless": vless_link, "telegram_proxy": telegram_proxy}


def create_subscription(
    user_id: int,
    server_id: str,
    months: int | None = None,
    minutes: int | None = None,
    expires_at: int | None = None,
) -> dict[str, Any]:
    """Create a new subscription record and return its URL."""
    user = load_user(user_id)
    result = create_subscription_for_user(
        user,
        server_id=server_id,
        months=months,
        minutes=minutes,
        expires_at=expires_at,
    )
    save_user(user)
    return result



def extract_sub_id(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    path = parsed.path if parsed.scheme or parsed.netloc else raw
    path = str(path or "").strip().rstrip("/")
    if "/" in path:
        path = path.rsplit("/", 1)[-1]
    if not path:
        return None

    if re.fullmatch(r"[A-Za-z0-9_.:-]{6,256}", path):
        return path
    return None


def _sync_server_for_portal(server_id: str, cfg: dict[str, Any]) -> str:
    server = next((s for s in cfg.get("servers", []) if s.get("id") == server_id), None)
    if not server:
        return f"Server not found: {server_id}"
    config_path = write_xray_config(server_id)
    if server.get("managed_local", False):
        _, msg = maybe_restart_xray(config_path)
        return msg
    _, msg = maybe_deploy_xray(server_id, config_path)
    return msg


def port_subscription_to_user(
    target_user_id: int,
    source_sub_url_or_id: str,
    only_active: bool = True,
) -> dict[str, Any]:
    sub_id = extract_sub_id(source_sub_url_or_id)
    if not sub_id:
        raise ValueError("Invalid subscription URL/ID")

    source_user = _find_user_by_sub_id(sub_id)
    if not source_user:
        raise KeyError("subscription not found")

    target_user = load_user(target_user_id)
    source_uid = int(source_user.get("user_id") or 0)
    if source_uid == int(target_user_id):
        return {
            "sub_id": sub_id,
            "copied": 0,
            "skipped": 0,
            "servers_synced": [],
            "message": "Subscription already belongs to this user.",
        }

    copied = 0
    skipped = 0
    synced_servers: set[str] = set()
    changed = False

    for sub in source_user.get("subscriptions", []):
        if str(sub.get("sub_id") or "") != sub_id:
            continue
        if only_active and not _subscription_active(sub):
            skipped += 1
            continue

        server_id = str(sub.get("server_id") or "").strip()
        if not server_id:
            skipped += 1
            continue

        expires_at = sub.get("expires_at")
        try:
            normalized_exp = int(expires_at) if expires_at else None
        except Exception:
            normalized_exp = None

        duplicate = False
        for existing in target_user.get("subscriptions", []):
            if str(existing.get("server_id") or "") != server_id:
                continue
            existing_exp = existing.get("expires_at")
            try:
                existing_exp_i = int(existing_exp) if existing_exp else None
            except Exception:
                existing_exp_i = None
            if existing_exp_i == normalized_exp and _subscription_active(existing):
                duplicate = True
                break
        if duplicate:
            skipped += 1
            continue

        create_subscription_for_user(
            target_user,
            server_id=server_id,
            expires_at=normalized_exp,
        )
        copied += 1
        changed = True
        synced_servers.add(server_id)

    if changed:
        save_user(target_user)

    cfg = load_config()
    for server_id in sorted(synced_servers):
        try:
            _sync_server_for_portal(server_id, cfg)
        except Exception:
            pass

    return {
        "sub_id": sub_id,
        "copied": copied,
        "skipped": skipped,
        "servers_synced": sorted(synced_servers),
        "message": "Port completed." if copied else "Nothing to port.",
    }

def _resolve_exec(path_value: str | None, default_name: str) -> Path:
    if path_value:
        p = Path(path_value)
        return p if p.is_absolute() else (ROOT_DIR / p)
    return ROOT_DIR / default_name


def maybe_restart_xray(config_path: Path) -> tuple[bool, str]:
    cfg = load_config()
    if not cfg.get("xray_autorestart", False):
        return False, "Xray autorestart is disabled."

    xray_exec = _resolve_exec(cfg.get("xray_exec"), "xray.exe")
    if not xray_exec.exists():
        return True, f"Xray not found: {xray_exec}"

    if not config_path.exists():
        return True, f"Config not found: {config_path}"

    exe_name = xray_exec.name
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/IM", exe_name, "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            subprocess.run(
                ["pkill", "-f", str(xray_exec)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except Exception:
        pass

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    start_log = LOGS_DIR / "xray_start.log"

    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

        out = start_log.open("a", encoding="utf-8")
        subprocess.Popen(
            [str(xray_exec), "run", "-c", str(config_path)],
            stdout=out,
            stderr=out,
            cwd=str(ROOT_DIR),
            creationflags=creationflags,
        )
        return True, "Xray restarted."
    except Exception as exc:
        return True, f"Failed to start Xray: {exc}"


def maybe_deploy_xray(server_id: str, config_path: Path) -> tuple[bool, str]:
    cfg = load_config()
    server = _get_server(cfg, server_id)
    deploy = server.get("deploy") or {}
    if not isinstance(deploy, dict) or deploy.get("type") != "ssh":
        return False, "Deploy is not configured."

    host = str(deploy.get("host") or "").strip()
    port = int(deploy.get("port") or 22)
    user = str(deploy.get("user") or "root").strip()
    key_path = str(os.getenv("HMAO_SSH_KEY_PATH") or deploy.get("key_path") or "").strip()
    remote_path = str(deploy.get("remote_config_path") or "").strip()
    restart_cmd = str(deploy.get("restart_cmd") or "").strip()
    if not host or not remote_path:
        return False, "Deploy config missing host or remote_config_path."

    ssh_opts = [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
    ]

    scp_cmd = ["scp", *ssh_opts]
    if key_path:
        scp_cmd += ["-i", key_path]
    scp_cmd += ["-P", str(port), str(config_path), f"{user}@{host}:{remote_path}"]

    try:
        subprocess.run(scp_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", errors="ignore").strip()
        return False, f"SCP failed: {err or exc}"
    except Exception as exc:
        return False, f"SCP failed: {exc}"

    if restart_cmd:
        ssh_cmd = ["ssh", *ssh_opts]
        if key_path:
            ssh_cmd += ["-i", key_path]
        ssh_cmd += ["-p", str(port), f"{user}@{host}", restart_cmd]
        try:
            subprocess.run(ssh_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or b"").decode("utf-8", errors="ignore").strip()
            return False, f"Restart failed: {err or exc}"
        except Exception as exc:
            return False, f"Restart failed: {exc}"

    return True, "Xray deployed."


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backend utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create-sub", help="Create subscription link")
    p_create.add_argument("--user", type=int, required=True)
    p_create.add_argument("--server", required=True)

    p_run = sub.add_parser("run-sub-server", help="Run /api/sub HTTP server")
    p_run.add_argument("--bind", default=None)
    p_run.add_argument("--port", type=int, default=None)

    args = parser.parse_args()
    if args.cmd == "create-sub":
        result = create_subscription(args.user, args.server)
        print(result["url"])
    elif args.cmd == "run-sub-server":
        run_sub_server(bind=args.bind, port=args.port)

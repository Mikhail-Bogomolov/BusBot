"""Telegram-only proxy: turns a VPN key from Happ / v2rayN / Hiddify (vless://, vmess://, trojan://, ss://)
or a subscription URL into an Xray config with a SOCKS5 inbound. The `tg-proxy` container runs it, the bot
sends only Telegram traffic there; Yandex goes direct.

    python -m busbot.tgproxy "<ключ или ссылка на подписку>" [номер сервера] [--out=xray/config.json]
    docker compose run --rm bot python -m busbot.tgproxy "<ключ>"     # то же на сервере без Python
"""
import base64
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

PORT = 1080
OUT = "xray/config.json"
SUPPORTED = "vless://, vmess://, trojan://, ss://"


def _b64(s: str) -> str:
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4)).decode()


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in ("", None)}


def _on(v: str | None) -> bool:
    return (v or "").lower() in ("1", "true")


def _stream(q: dict[str, str], security: str = "none") -> dict:
    net = q.get("type") or "tcp"
    net = {"raw": "tcp", "splithttp": "xhttp"}.get(net, net)
    sec = q.get("security") or security
    sni, fp, host, path = q.get("sni") or q.get("peer", ""), q.get("fp", ""), q.get("host", ""), q.get("path", "")
    s: dict = {"network": net, "security": sec}
    if sec == "reality":
        if not q.get("pbk"):
            raise ValueError("reality без pbk (публичного ключа)")
        s["realitySettings"] = _clean({"serverName": sni, "fingerprint": fp or "chrome", "publicKey": q["pbk"],
                                       "shortId": q.get("sid", ""), "spiderX": q.get("spx", ""),
                                       "mldsa65Verify": q.get("pqv", "")})
    elif sec == "tls":
        s["tlsSettings"] = _clean({"serverName": sni, "fingerprint": fp,
                                   "alpn": q["alpn"].split(",") if q.get("alpn") else None,
                                   "allowInsecure": True if _on(q.get("allowInsecure") or q.get("insecure")) else None})
    elif sec != "none":
        raise ValueError(f"security={sec} не поддерживается")
    if net == "tcp":
        if q.get("headerType", "none") != "none":
            raise ValueError(f"tcp с headerType={q['headerType']} не поддерживается")
    elif net == "ws":
        s["wsSettings"] = _clean({"path": path or "/", "host": host})
    elif net == "httpupgrade":
        s["httpupgradeSettings"] = _clean({"path": path or "/", "host": host})
    elif net == "grpc":
        s["grpcSettings"] = _clean({"serviceName": q.get("serviceName", ""), "multiMode": q.get("mode") == "multi"})
    elif net == "xhttp":
        s["xhttpSettings"] = _clean({"path": path or "/", "host": host, "mode": q.get("mode") or "auto",
                                     "extra": json.loads(q["extra"]) if q.get("extra") else None})
    else:
        raise ValueError(f"транспорт type={net} не поддерживается")
    return s


def outbound(link: str) -> tuple[str, dict]:
    """(server name, Xray outbound) for one share link. ValueError if the link can't be used."""
    link = link.strip()
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vmess":
        v = json.loads(_b64(link[8:].split("#")[0]))
        if int(v.get("aid") or 0):
            raise ValueError("vmess с alterId>0 Xray больше не поддерживает")
        net = v.get("net") or "tcp"
        q = {k: str(x) for k, x in {"type": net, "security": v.get("tls") or "none", "sni": v.get("sni"),
                                     "host": v.get("host"), "path": v.get("path"), "alpn": v.get("alpn"),
                                     "fp": v.get("fp"), "serviceName": v.get("path") if net == "grpc" else None,
                                     "headerType": v.get("type") if net == "tcp" else None}.items() if x}
        user = {"id": v["id"], "security": v.get("scy") or "auto"}
        return str(v.get("ps", "")), {"protocol": "vmess", "streamSettings": _stream(q),
                                      "settings": {"vnext": [{"address": v["add"], "port": int(v["port"]),
                                                              "users": [user]}]}}
    if scheme not in ("vless", "trojan", "ss"):
        if scheme == "happ":
            raise ValueError("зашифрованная ссылка happ:// не читается: нужен обычный ключ или ссылка на подписку")
        raise ValueError(f"{scheme}:// не поддерживается, нужен один из {SUPPORTED}")
    u = urlsplit(link)
    q = {k: x[0] for k, x in parse_qs(u.query).items()}
    name = unquote(u.fragment)
    if scheme == "ss":
        if q.get("plugin"):
            raise ValueError("ss:// с plugin не поддерживается")
        if "@" in u.netloc:          # SIP002: base64(method:password)@host:port or method:password@host:port
            if u.password is not None:
                method, pw = unquote(u.username), unquote(u.password)
            else:
                method, _, pw = _b64(unquote(u.username)).partition(":")
            host, port = u.hostname, u.port
        else:                        # legacy: base64(method:password@host:port)
            method, _, rest = _b64(unquote(link[5:].split("#")[0].split("?")[0].rstrip("/"))).partition(":")
            pw, _, hp = rest.rpartition("@")
            host, _, p = hp.rpartition(":")
            port = int(p)
        if not (method and pw and host and port):
            raise ValueError("не разобрать ss://")
        return name, {"protocol": "shadowsocks",
                      "settings": {"servers": [{"address": host, "port": port, "method": method, "password": pw}]}}
    if not (u.username and u.hostname and u.port):
        raise ValueError(f"в ключе {scheme}:// нет id/адреса/порта")
    if scheme == "trojan":
        return name, {"protocol": "trojan", "streamSettings": _stream(q, "tls"),
                      "settings": {"servers": [{"address": u.hostname, "port": u.port,
                                                "password": unquote(u.username)}]}}
    user = _clean({"id": unquote(u.username), "encryption": q.get("encryption") or "none", "flow": q.get("flow")})
    return name, {"protocol": "vless", "streamSettings": _stream(q),
                  "settings": {"vnext": [{"address": u.hostname, "port": u.port, "users": [user]}]}}


def links_from_text(text: str) -> list[str]:
    """Share links from a key, a list of keys or a base64 subscription body."""
    text = text.strip()
    if "://" not in text:
        try:
            text = _b64(text)
        except ValueError:
            raise ValueError("не похоже ни на ключ, ни на подписку") from None
    return [x.strip() for x in text.splitlines() if "://" in x]


def links(src: str) -> list[str]:
    src = src.strip()
    if src.lower().startswith(("http://", "https://")):
        r = httpx.get(src, timeout=20, follow_redirects=True, headers={"User-Agent": "busbot-tgproxy"})
        r.raise_for_status()
        return links_from_text(r.text)
    return links_from_text(src)


def config(ob: dict) -> dict:
    """SOCKS5 without auth on PORT: reachable only inside the compose network, the port is never published."""
    return {"log": {"loglevel": "warning"},
            "inbounds": [{"tag": "telegram", "listen": "0.0.0.0", "port": PORT, "protocol": "socks",
                          "settings": {"auth": "noauth", "udp": False}}],
            "outbounds": [{"tag": "proxy", **ob}]}


def describe(ob: dict) -> str:
    """Protocol, transport and server without secrets."""
    st = ob.get("streamSettings", {})
    srv = (ob["settings"].get("vnext") or ob["settings"].get("servers"))[0]
    return " ".join(x for x in (ob["protocol"], st.get("security", ""), st.get("network", ""),
                                f"{srv['address']}:{srv['port']}") if x and x != "none")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")    # server names carry emoji flags; Windows pipes are cp1251
    out = OUT
    args = []
    for a in argv:
        if a.startswith("--out="):
            out = a[6:]
        else:
            args.append(a)
    if not args:
        print(__doc__)
        return 2
    try:
        items = links(args[0])
    except (httpx.HTTPError, ValueError) as e:
        print(f"Ошибка: {e}")
        return 1
    ok: list[tuple[int, str, dict]] = []
    for i, link in enumerate(items, 1):
        try:
            name, ob = outbound(link)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            print(f"{i:>2}. пропущен: {e}")
            continue
        ok.append((i, name, ob))
        print(f"{i:>2}. {name or '—'}  [{describe(ob)}]")
    if not ok:
        print(f"Нет подходящих серверов. Нужен ключ {SUPPORTED} или ссылка на подписку.")
        return 1
    pick = ok[0]
    if len(args) > 1:
        pick = next((x for x in ok if str(x[0]) == args[1]), None)
        if pick is None:
            print(f"Сервера {args[1]} нет среди подходящих")
            return 1
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config(pick[2]), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nВыбран {pick[0]}. {pick[1] or '—'} → {p}\n"
          f"Другой сервер: добавь его номер после ключа. Запуск: docker compose up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""vless_config: разбор ссылки vless:// и сборка конфига Xray; порядок прокси при скачивании."""

import io
import json
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

import downloader
import vless_config as vc

UUID = "11111111-2222-3333-4444-555555555555"
REALITY = (f"vless://{UUID}@example.org:443?type=tcp&security=reality&pbk=PUBKEY&sid=ab12"
           "&sni=www.microsoft.com&fp=chrome&flow=xtls-rprx-vision&spx=%2F#Мой%20сервер")
WS_TLS = (f"vless://{UUID}@cdn.example.org:443?type=ws&security=tls&sni=cdn.example.org"
          "&host=front.example.org&path=%2Fws%3Fed%3D2048&alpn=h2,http/1.1#ws")
GRPC = f"vless://{UUID}@g.example.org:8443?type=grpc&security=tls&serviceName=svc&mode=multi"


# ─── Конфиг ───────────────────────────────────────────────────────────────────

def test_reality_config():
    cfg = vc.build_config(REALITY)
    out = cfg["outbounds"][0]
    user = out["settings"]["vnext"][0]
    assert (user["address"], user["port"]) == ("example.org", 443)
    assert user["users"][0] == {"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}
    stream = out["streamSettings"]
    assert stream["network"] == "tcp" and stream["security"] == "reality"
    assert stream["realitySettings"] == {
        "serverName": "www.microsoft.com", "fingerprint": "chrome",
        "publicKey": "PUBKEY", "shortId": "ab12", "spiderX": "/"}


def test_inbound_is_socks_on_1080_without_udp():
    inbound = vc.build_config(REALITY)["inbounds"][0]
    assert inbound["protocol"] == "socks" and inbound["port"] == 1080
    assert inbound["settings"]["udp"] is False


def test_ws_tls_config():
    stream = vc.build_config(WS_TLS)["outbounds"][0]["streamSettings"]
    assert stream["network"] == "ws" and stream["security"] == "tls"
    assert stream["wsSettings"] == {"path": "/ws?ed=2048", "headers": {"Host": "front.example.org"}}
    assert stream["tlsSettings"]["serverName"] == "cdn.example.org"
    assert stream["tlsSettings"]["alpn"] == ["h2", "http/1.1"]
    # flow не задан в ссылке — в пользователе его нет
    assert "flow" not in vc.build_config(WS_TLS)["outbounds"][0]["settings"]["vnext"][0]["users"][0]


def test_grpc_config():
    stream = vc.build_config(GRPC)["outbounds"][0]["streamSettings"]
    assert stream["grpcSettings"] == {"serviceName": "svc", "multiMode": True}


def test_config_is_json_serializable():
    json.dumps(vc.build_config(REALITY))


def test_config_rejects_empty_server_list():
    with pytest.raises(vc.VlessError, match="Нет ни одного ключа"):
        vc.build_config([])


@pytest.mark.parametrize(
    "url, expected",
    [
        (f"vless://{UUID}@plain.example.org:80", {"network": "tcp", "security": "none"}),
        (
            f"vless://{UUID}@cdn.example.org:443?type=ws&security=tls",
            {
                "network": "ws",
                "security": "tls",
                "tlsSettings": {"serverName": "cdn.example.org", "fingerprint": "chrome"},
                "wsSettings": {"path": "/"},
            },
        ),
    ],
)
def test_config_fills_transport_defaults(url, expected):
    assert vc.build_config(url)["outbounds"][0]["streamSettings"] == expected


@pytest.mark.parametrize("url, fragment", [
    ("", "vless://"),
    ("vmess://abc", "vless://"),
    ("vless://host:443", "uuid"),
    (f"vless://{UUID}@h:443?type=xhttp", "xhttp"),
    (f"vless://{UUID}@h:443?security=xtls", "xtls"),
    (f"vless://{UUID}@h:443?type=tcp&headerType=http", "headerType"),
    (f"vless://{UUID}@h:443?security=reality", "pbk"),
])
def test_bad_links_are_rejected_with_reason(url, fragment):
    with pytest.raises(vc.VlessError, match=fragment):
        vc.build_config(url)


def test_describe_hides_secrets():
    text = vc.describe(REALITY)
    assert "example.org:443" in text and "reality" in text
    assert UUID not in text and "PUBKEY" not in text and "ab12" not in text


def test_main_writes_config_with_private_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vc.os.path, "abspath", lambda p: str(tmp_path / "vless_config.py"))
    monkeypatch.setenv("VLESS_URL", REALITY)
    assert vc.main(["vless_config.py"]) == 0
    cfg = tmp_path / "xray" / "config.json"
    assert json.loads(cfg.read_text(encoding="utf-8"))["outbounds"][0]["protocol"] == "vless"
    assert UUID not in capsys.readouterr().out   # в консоль uuid не печатается


def test_main_without_key_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("VLESS_URL", raising=False)
    monkeypatch.chdir(tmp_path)   # .env здесь нет
    assert vc.main(["vless_config.py"]) == 1


# ─── Подписка ─────────────────────────────────────────────────────────────────

PLACEHOLDER = f"vless://{UUID}@0.0.0.0:2083?type=tcp&security=tls#⬇️%20Внизу%20Домашние%20⬇️"
NL = REALITY.replace("#Мой%20сервер", "#🇳🇱%20Нидерланды")
DE = WS_TLS.replace("#ws", "#🇩🇪%20Германия")
RU = GRPC + "#🇷🇺%20Москва"
SUB_LINES = "\n".join([PLACEHOLDER, NL, DE, RU, "vmess://abc", "vless://broken"])


def test_split_subscription_base64_and_plain():
    encoded = vc.base64.b64encode(SUB_LINES.encode()).decode()
    assert vc.split_subscription(encoded) == vc.split_subscription(SUB_LINES)
    assert len(vc.split_subscription(SUB_LINES)) == 5      # vmess не берём


def test_select_drops_placeholder_and_broken():
    good, rejected = vc.select_links(vc.split_subscription(SUB_LINES))
    assert good == [NL, DE, RU]
    assert any("заглушка" in r for r in rejected) and len(rejected) == 2
    assert not any(UUID in r for r in rejected)


@pytest.mark.parametrize("address", ["example.org:abc", "example.org:65536", "[2001:db8::1"])
def test_select_skips_malformed_address_and_keeps_other_servers(address):
    broken = f"vless://{UUID}@{address}"

    good, rejected = vc.select_links([broken, NL])

    assert good == [NL]
    assert len(rejected) == 1 and "адрес сервера или порт" in rejected[0]
    assert UUID not in rejected[0] and address not in rejected[0]


def test_select_pick_and_skip_by_name():
    links = vc.split_subscription(SUB_LINES)
    assert vc.select_links(links, pick="нидерланды, германия")[0] == [NL, DE]
    assert vc.select_links(links, skip="Москва")[0] == [NL, DE]


def test_resolve_subscription_url(monkeypatch):
    monkeypatch.setattr(vc, "fetch_subscription", lambda url: SUB_LINES)
    links, rejected = vc.resolve_links("https://sub.example/abc", skip="москва")
    assert links == [NL, DE] and rejected


def test_resolve_subscription_without_usable_servers(monkeypatch):
    monkeypatch.setattr(vc, "fetch_subscription", lambda url: PLACEHOLDER)
    with pytest.raises(vc.VlessError, match="ни один"):
        vc.resolve_links("https://sub.example/abc")


@pytest.fixture
def subscription_cli(tmp_path, monkeypatch):
    url = "https://sub.example/secret-token"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(vc, "__file__", str(tmp_path / "vless_config.py"))
    for key in ("VLESS_URL", "VLESS_PICK", "VLESS_SKIP"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        f'export VLESS_URL="{url}"\n'
        'VLESS_PICK="нидерланды, германия, москва"\n'
        "VLESS_SKIP='москва'\n",
        encoding="utf-8",
    )
    urlopen = Mock()
    monkeypatch.setattr(vc.urllib.request, "urlopen", urlopen)
    return url, urlopen, tmp_path / "xray" / "config.json"


def test_cli_fetches_and_filters_base64_subscription(subscription_cli, capsys):
    url, urlopen, config_path = subscription_cli
    encoded = vc.base64.urlsafe_b64encode(SUB_LINES.encode()).rstrip(b"=")
    urlopen.return_value = io.BytesIO(encoded)

    assert vc.main(["vless_config.py"]) == 0

    urlopen.assert_called_once()
    request = urlopen.call_args.args[0]
    assert request.full_url == url
    assert request.get_header("User-agent") == "v2rayN/7.0"
    assert urlopen.call_args.kwargs == {"timeout": 25}
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert [item["settings"]["vnext"][0]["address"] for item in config["outbounds"]] == [
        "example.org", "cdn.example.org",
    ]
    assert config["routing"]["balancers"][0]["strategy"] == {"type": "leastPing"}
    output = capsys.readouterr().out
    assert url not in output and UUID not in output and "PUBKEY" not in output


def test_cli_http_error_preserves_existing_config(subscription_cli, capsys):
    url, urlopen, config_path = subscription_cli
    config_path.parent.mkdir()
    original = "{}"
    config_path.write_text(original, encoding="utf-8")
    urlopen.side_effect = HTTPError(url, 403, "Forbidden", None, None)

    assert vc.main(["vless_config.py"]) == 1

    assert config_path.read_text(encoding="utf-8") == original
    output = capsys.readouterr().out
    assert "Forbidden" in output
    assert url not in output


def test_cli_rejects_subscription_without_vless_links(subscription_cli, capsys):
    _, urlopen, config_path = subscription_cli
    urlopen.return_value = io.BytesIO(b"vmess://unsupported")

    assert vc.main(["vless_config.py"]) == 1

    assert "нет ни одной ссылки vless://" in capsys.readouterr().out
    assert not config_path.exists()


def test_single_key_has_no_balancer():
    cfg = vc.build_config(REALITY)
    assert "routing" not in cfg and "observatory" not in cfg


def test_several_keys_go_behind_balancer():
    cfg = vc.build_config([NL, DE, RU])
    assert [o["tag"] for o in cfg["outbounds"]] == ["proxy-0", "proxy-1", "proxy-2"]
    assert cfg["observatory"]["subjectSelector"] == ["proxy-"]
    balancer = cfg["routing"]["balancers"][0]
    assert balancer["strategy"]["type"] == "leastPing"
    assert balancer["fallbackTag"] == "proxy-0"
    assert cfg["routing"]["rules"][0] == {
        "type": "field", "inboundTag": ["socks-in"], "balancerTag": balancer["tag"]}


# ─── Порядок прокси при скачивании ────────────────────────────────────────────

class _YDL:
    proxies: list = []
    fail_without_proxy = True

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        _YDL.proxies.append(self.opts.get("proxy"))
        if not download:
            return {"duration": 30}
        if _YDL.fail_without_proxy and not self.opts.get("proxy"):
            raise RuntimeError("unable to download video data: HTTP Error 403: Forbidden")
        path = self.opts["outtmpl"].replace("%(id)s", "v").replace("%(ext)s", "mp4")
        open(path, "wb").write(b"\0")
        self._path = path
        return {"id": "v", "ext": "mp4", "title": "T"}

    def prepare_filename(self, info):
        return self._path


@pytest.fixture
def tiktok_env(monkeypatch, tmp_path):
    _YDL.proxies = []
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _YDL)
    monkeypatch.setattr(downloader, "_ensure_h264", lambda p: p)
    monkeypatch.setattr(downloader, "_tmp_path", lambda prefix: str(tmp_path))
    return monkeypatch


def test_tiktok_tries_direct_first_then_proxy(tiktok_env):
    tiktok_env.setattr(downloader, "DOWNLOAD_PROXY", "socks5h://xray:1080")
    downloader._download_tiktok_sync("https://www.tiktok.com/@a/video/1", None)
    assert _YDL.proxies == ["", "", "socks5h://xray:1080", "socks5h://xray:1080"]


def test_tiktok_direct_success_never_touches_proxy(tiktok_env):
    tiktok_env.setattr(downloader, "DOWNLOAD_PROXY", "socks5h://xray:1080")
    tiktok_env.setattr(_YDL, "fail_without_proxy", False)
    downloader._download_tiktok_sync("https://www.tiktok.com/@a/video/1", None)
    assert _YDL.proxies == ["", ""]


def test_tiktok_without_proxy_setting_raises_direct_error(tiktok_env):
    tiktok_env.setattr(downloader, "DOWNLOAD_PROXY", None)
    with pytest.raises(RuntimeError, match="HTTP Error 403"):
        downloader._download_tiktok_sync("https://www.tiktok.com/@a/video/1", None)


def test_default_proxy_applies_to_both_yt_dlp_phases(tiktok_env):
    tiktok_env.setattr(downloader, "DOWNLOAD_PROXY", "socks5h://xray:1080")
    downloader._download_ytdlp_sync("https://www.tiktok.com/@a/video/1", None, "tiktok", "title")
    assert _YDL.proxies == ["socks5h://xray:1080", "socks5h://xray:1080"]


def test_tiktok_duration_limit_does_not_retry_via_proxy(monkeypatch):
    monkeypatch.setattr(downloader, "DOWNLOAD_PROXY", "socks5h://xray:1080")
    url = "https://www.tiktok.com/@a/video/1"
    download = Mock(side_effect=ValueError("Видео слишком длинное"))
    monkeypatch.setattr(downloader, "_download_ytdlp_sync", download)

    with pytest.raises(ValueError, match="слишком длинное"):
        downloader._download_tiktok_sync(url, None)

    download.assert_called_once_with(url, None, "tiktok", "TikTok Video", proxy="")

"""image_resolver：http 直传 / file→base64 / 不可识别。"""

import base64

from astrbot_plugin_self_reply.image_resolver import resolve


def test_http_and_https_passthrough():
    assert resolve("http://example.com/a.jpg") == "http://example.com/a.jpg"
    assert resolve("https://example.com/a.png") == "https://example.com/a.png"


def test_empty_and_garbage_return_none():
    assert resolve(None) is None
    assert resolve("") is None
    assert resolve("   ") is None
    assert resolve("relative/path.jpg") is None
    assert resolve("data:image/png;base64,AAAA") is None


def test_absolute_file_to_base64(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(b"abc")
    out = resolve(str(p))
    assert out == "data:image/png;base64," + base64.b64encode(b"abc").decode()


def test_mime_guessed_from_extension(tmp_path):
    p = tmp_path / "pic.gif"
    p.write_bytes(b"x")
    assert resolve(str(p)).startswith("data:image/gif;base64,")


def test_unknown_extension_falls_back_to_jpeg(tmp_path):
    p = tmp_path / "pic.unknownext"
    p.write_bytes(b"x")
    assert resolve(str(p)).startswith("data:image/jpeg;base64,")


def test_file_url_scheme(tmp_path):
    p = tmp_path / "pic.jpg"
    p.write_bytes(b"zz")
    out = resolve("file://" + str(p))
    assert out == "data:image/jpeg;base64," + base64.b64encode(b"zz").decode()


def test_missing_file_returns_none(tmp_path):
    assert resolve(str(tmp_path / "nope.png")) is None
    assert resolve("file:///nonexistent/nope.png") is None

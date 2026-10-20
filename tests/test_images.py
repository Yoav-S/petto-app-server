from app.images import identify_image


def test_common_logo_formats_are_recognized():
    png = identify_image(b"\x89PNG\r\n\x1a\nfake")
    jpeg = identify_image(b"\xff\xd8\xff\x00")
    gif = identify_image(b"GIF89a" + b"\x00" * 8)
    webp = identify_image(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP")
    svg = identify_image(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    heic = identify_image(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8)
    avif = identify_image(b"\x00\x00\x00\x18ftypavif" + b"\x00" * 8)
    assert png == ("image/png", ".png")
    assert jpeg == ("image/jpeg", ".jpg")
    assert gif == ("image/gif", ".gif")
    assert webp == ("image/webp", ".webp")
    assert svg == ("image/svg+xml", ".svg")
    assert heic == ("image/heic", ".heic")
    assert avif == ("image/avif", ".avif")


def test_svg_with_script_is_rejected():
    assert identify_image(b'<svg><script>alert(1)</script></svg>') is None
    assert identify_image(b"<html><svg></svg></html>") is None
    assert identify_image(b"not an image") is None

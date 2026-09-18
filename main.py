import os
import re
import json
from urllib.parse import urljoin, quote, unquote, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse


app = FastAPI()


# =========================================================
# 設定
# =========================================================

TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=120.0,
    write=120.0,
    pool=15.0
)

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding"
}


# =========================================================
# URL
# =========================================================

def make_proxy_url(url):
    return "/p/" + quote(url, safe="")


def make_frame_url(url):
    return "/f/" + quote(url, safe="")


def decode_target(target):
    return unquote(target)


def valid_target(url):
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.hostname:
            return False

        return True

    except Exception:
        return False


# =========================================================
# URL書き換え
# =========================================================

def rewrite_url(raw, base_url):
    if not raw:
        return raw

    raw = raw.strip()

    if raw.startswith((
        "#",
        "data:",
        "javascript:",
        "mailto:",
        "tel:",
        "blob:"
    )):
        return raw

    # すでにプロキシURLなら二重変換しない
    if raw.startswith("/p/") or raw.startswith("/f/"):
        return raw

    absolute = urljoin(base_url, raw)

    if not valid_target(absolute):
        return raw

    return make_proxy_url(absolute)


# =========================================================
# HTML
# =========================================================

ATTR_PATTERN = re.compile(
    r'(?P<prefix>\b(?:href|src|poster|data|action|cite|formaction)\s*=\s*)'
    r'(?P<quote>["\'])'
    r'(?P<url>.*?)'
    r'(?P=quote)',
    re.IGNORECASE | re.DOTALL
)


SRCSET_PATTERN = re.compile(
    r'(?P<prefix>\bsrcset\s*=\s*)'
    r'(?P<quote>["\'])'
    r'(?P<value>.*?)'
    r'(?P=quote)',
    re.IGNORECASE | re.DOTALL
)


IFRAME_PATTERN = re.compile(
    r'<iframe\b(?P<tag>[^>]*)>',
    re.IGNORECASE | re.DOTALL
)


def rewrite_iframe(match, base_url):
    tag = match.group(0)

    pattern = re.compile(
        r'(?P<prefix>\bsrc\s*=\s*)'
        r'(?P<quote>["\'])'
        r'(?P<url>.*?)'
        r'(?P=quote)',
        re.IGNORECASE | re.DOTALL
    )

    def replace_src(src_match):
        raw = src_match.group("url")

        if raw.startswith("/p/") or raw.startswith("/f/"):
            return src_match.group(0)

        absolute = urljoin(base_url, raw)

        if not valid_target(absolute):
            return src_match.group(0)

        return (
            src_match.group("prefix")
            + src_match.group("quote")
            + make_frame_url(absolute)
            + src_match.group("quote")
        )

    return pattern.sub(replace_src, tag)


def rewrite_srcset(match, base_url):
    value = match.group("value")
    parts = []

    for item in value.split(","):
        item = item.strip()

        if not item:
            continue

        fields = item.split()

        if not fields:
            continue

        fields[0] = rewrite_url(fields[0], base_url)
        parts.append(" ".join(fields))

    return (
        match.group("prefix")
        + match.group("quote")
        + ", ".join(parts)
        + match.group("quote")
    )


def rewrite_css_urls(text, base_url):
    pattern = re.compile(
        r'url\(\s*(?P<quote>["\']?)(?P<url>.*?)(?P=quote)\s*\)',
        re.IGNORECASE | re.DOTALL
    )

    def replace(match):
        raw = match.group("url").strip()

        if raw.startswith("/p/") or raw.startswith("/f/"):
            return match.group(0)

        new_url = rewrite_url(raw, base_url)

        return (
            "url("
            + match.group("quote")
            + new_url
            + match.group("quote")
            + ")"
        )

    return pattern.sub(replace, text)


def rewrite_js(text, base_url):
    patterns = [
        (
            r'(\bfetch\s*\(\s*["\'])([^"\']+)(["\'])',
            lambda m: m.group(1)
            + rewrite_url(m.group(2), base_url)
            + m.group(3)
        ),
        (
            r'(\bimport\s*\(\s*["\'])([^"\']+)(["\'])',
            lambda m: m.group(1)
            + rewrite_url(m.group(2), base_url)
            + m.group(3)
        ),
        (
            r'(\blocation\.href\s*=\s*["\'])([^"\']+)(["\'])',
            lambda m: m.group(1)
            + rewrite_url(m.group(2), base_url)
            + m.group(3)
        ),
        (
            r'(\blocation\.assign\s*\(\s*["\'])([^"\']+)(["\'])',
            lambda m: m.group(1)
            + rewrite_url(m.group(2), base_url)
            + m.group(3)
        ),
        (
            r'(\blocation\.replace\s*\(\s*["\'])([^"\']+)(["\'])',
            lambda m: m.group(1)
            + rewrite_url(m.group(2), base_url)
            + m.group(3)
        )
    ]

    for pattern, replacement in patterns:
        text = re.sub(
            pattern,
            replacement,
            text,
            flags=re.IGNORECASE
        )

    return text


def rewrite_html(text, base_url):
    # iframeを最初に処理
    text = IFRAME_PATTERN.sub(
        lambda m: rewrite_iframe(m, base_url),
        text
    )

    # 通常のhref/src等
    def replace_attr(match):
        raw = match.group("url")

        if raw.startswith("/p/") or raw.startswith("/f/"):
            return match.group(0)

        new_url = rewrite_url(raw, base_url)

        return (
            match.group("prefix")
            + match.group("quote")
            + new_url
            + match.group("quote")
        )

    text = ATTR_PATTERN.sub(replace_attr, text)

    # srcset
    text = SRCSET_PATTERN.sub(
        lambda m: rewrite_srcset(m, base_url),
        text
    )

    # CSS
    text = rewrite_css_urls(text, base_url)

    return text


# =========================================================
# ツールバー
# =========================================================

TOOLBAR_SCRIPT = r"""
(function () {
    "use strict";

    if (window.__PY_PROXY_TOOLBAR__) {
        return;
    }

    window.__PY_PROXY_TOOLBAR__ = true;

    const currentUrl =
        window.__PYTHON_PROXY_TARGET__ || location.href;

    const host = document.createElement("div");

    host.style.position = "fixed";
    host.style.top = "0";
    host.style.left = "0";
    host.style.right = "0";
    host.style.height = "52px";
    host.style.zIndex = "2147483647";

    document.documentElement.appendChild(host);

    const shadow = host.attachShadow({
        mode: "open"
    });

    const style = document.createElement("style");

    style.textContent = `
        * {
            box-sizing: border-box;
        }

        .bar {
            width: 100%;
            height: 52px;
            display: flex;
            align-items: center;
            gap: 5px;
            padding: 6px;
            background: #202124;
            border-bottom: 1px solid #444;
            font-family: Arial, sans-serif;
        }

        button {
            height: 38px;
            min-width: 38px;
            padding: 0 9px;
            border: 0;
            border-radius: 6px;
            background: #3c4043;
            color: white;
            font-size: 17px;
            cursor: pointer;
        }

        button:hover {
            background: #5f6368;
        }

        input {
            flex: 1;
            min-width: 80px;
            height: 38px;
            padding: 0 10px;
            border: 1px solid #5f6368;
            border-radius: 6px;
            background: #303134;
            color: white;
            font-size: 14px;
            outline: none;
        }

        select {
            height: 38px;
            border: 1px solid #5f6368;
            border-radius: 6px;
            background: #303134;
            color: white;
            padding: 0 7px;
            font-size: 13px;
        }

        @media (max-width: 600px) {
            .bar {
                gap: 3px;
                padding: 5px;
            }

            button {
                min-width: 34px;
                padding: 0 6px;
            }

            select {
                max-width: 105px;
            }
        }
    `;

    shadow.appendChild(style);

    const bar = document.createElement("div");
    bar.className = "bar";

    const back = document.createElement("button");
    back.textContent = "←";

    const forward = document.createElement("button");
    forward.textContent = "→";

    const reload = document.createElement("button");
    reload.textContent = "⟳";

    const home = document.createElement("button");
    home.textContent = "⌂";

    const input = document.createElement("input");
    input.type = "text";
    input.value = currentUrl;
    input.placeholder = "URL";

    const go = document.createElement("button");
    go.textContent = "移動";

    const mode = document.createElement("select");

    const bufferOption = document.createElement("option");
    bufferOption.value = "buffer";
    bufferOption.textContent = "バッファ";

    const streamOption = document.createElement("option");
    streamOption.value = "stream";
    streamOption.textContent = "ストリーミング";

    mode.appendChild(bufferOption);
    mode.appendChild(streamOption);

    const savedMode =
        document.cookie
            .split(";")
            .map(x => x.trim())
            .find(x => x.startsWith("pyproxy_mode="));

    if (savedMode) {
        const value = savedMode.split("=")[1];

        if (value === "stream") {
            mode.value = "stream";
        } else {
            mode.value = "buffer";
        }
    } else {
        mode.value = "buffer";
    }

    mode.addEventListener("change", function () {
        document.cookie =
            "pyproxy_mode=" +
            encodeURIComponent(mode.value) +
            "; path=/; max-age=31536000; SameSite=Lax";
    });

    back.onclick = function () {
        history.back();
    };

    forward.onclick = function () {
        history.forward();
    };

    reload.onclick = function () {
        location.reload();
    };

    home.onclick = function () {
        location.href = "/";
    };

    function navigate() {
        let url = input.value.trim();

        if (!url) {
            return;
        }

        if (!/^https?:\/\//i.test(url)) {
            url = "https://" + url;
        }

        location.href =
            "/p/" + encodeURIComponent(url);
    }

    go.onclick = navigate;

    input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
            navigate();
        }
    });

    bar.appendChild(back);
    bar.appendChild(forward);
    bar.appendChild(reload);
    bar.appendChild(home);
    bar.appendChild(input);
    bar.appendChild(go);
    bar.appendChild(mode);

    shadow.appendChild(bar);
})();
"""


def toolbar_html(base_url):
    safe_url = json.dumps(
        base_url,
        ensure_ascii=False
    ).replace("</", "<\\/")

    return (
        "<script>"
        + "window.__PYTHON_PROXY_TARGET__="
        + safe_url
        + ";"
        + TOOLBAR_SCRIPT
        + "</script>"
    )


# =========================================================
# HTMLへツールバー追加
# =========================================================

def inject_toolbar(text, base_url):
    toolbar = toolbar_html(base_url)

    body_pattern = re.compile(
        r"<body\b[^>]*>",
        re.IGNORECASE
    )

    if body_pattern.search(text):
        return body_pattern.sub(
            lambda m: m.group(0) + toolbar,
            text,
            count=1
        )

    return toolbar + text


# =========================================================
# HTTPヘッダー
# =========================================================

def make_request_headers(request):
    headers = {}

    for key, value in request.headers.items():
        lower = key.lower()

        if lower in (
            "host",
            "content-length",
            "accept-encoding"
        ):
            continue

        if lower == "cookie":
            cookies = []

            for item in value.split(";"):
                item = item.strip()

                if not item:
                    continue

                if item.startswith("pyproxy_mode="):
                    continue

                cookies.append(item)

            if cookies:
                headers["cookie"] = "; ".join(cookies)

            continue

        headers[key] = value

    headers["accept-encoding"] = "identity"

    return headers


def make_response_headers(upstream):
    result = {}

    for key, value in upstream.headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue

        result[key] = value

    return result


# =========================================================
# Cookie
# =========================================================

def rewrite_set_cookie(value):
    parts = []

    for part in value.split(";"):
        if part.strip().lower().startswith("domain="):
            continue

        parts.append(part)

    return ";".join(parts)


# =========================================================
# Proxy本体
# =========================================================

async def proxy_request(
    request,
    target,
    add_toolbar
):
    if not valid_target(target):
        return Response(
            "Invalid URL",
            status_code=400,
            media_type="text/plain"
        )

    headers = make_request_headers(request)

    body = await request.body()

    client = httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False
    )

    try:
        upstream = await client.stream(
            request.method,
            target,
            headers=headers,
            content=body
        ).__aenter__()

    except Exception as exc:
        await client.aclose()

        return Response(
            "Proxy error: " + str(exc),
            status_code=502,
            media_type="text/plain"
        )

    status_code = upstream.status_code
    response_headers = make_response_headers(upstream)

    if "location" in response_headers:
        location = response_headers["location"]

        absolute = urljoin(target, location)

        if valid_target(absolute):
            response_headers["location"] = make_proxy_url(
                absolute
            )

    if "set-cookie" in upstream.headers:
        cookies = upstream.headers.get_list("set-cookie")

        response_headers.pop("set-cookie", None)

        # StreamingResponseで複数Cookieを返すための追加処理
        cookie_list = [
            rewrite_set_cookie(cookie)
            for cookie in cookies
        ]
    else:
        cookie_list = []

    content_type = upstream.headers.get(
        "content-type",
        ""
    ).lower()

    is_html = (
        "text/html" in content_type
        or "application/xhtml+xml" in content_type
    )

    is_css = (
        "text/css" in content_type
    )

    is_js = (
        "javascript" in content_type
        or "ecmascript" in content_type
    )

    # HTML/CSS/JSはURL書き換えが必要なのでバッファ
    if is_html or is_css or is_js:
        try:
            data = await upstream.aread()

        except Exception as exc:
            await upstream.aclose()
            await client.aclose()

            return Response(
                "Read error: " + str(exc),
                status_code=502,
                media_type="text/plain"
            )

        await upstream.aclose()
        await client.aclose()

        encoding = "utf-8"

        try:
            text_data = data.decode(encoding)
        except UnicodeDecodeError:
            text_data = data.decode(
                encoding,
                errors="replace"
            )

        if is_html:
            text_data = rewrite_html(
                text_data,
                target
            )

            if add_toolbar:
                text_data = inject_toolbar(
                    text_data,
                    target
                )

        elif is_css:
            text_data = rewrite_css_urls(
                text_data,
                target
            )

        elif is_js:
            text_data = rewrite_js(
                text_data,
                target
            )

        response_headers.pop(
            "content-length",
            None
        )

        response_headers.pop(
            "content-encoding",
            None
        )

        response_headers.pop(
            "transfer-encoding",
            None
        )

        response = Response(
            content=text_data.encode("utf-8"),
            status_code=status_code,
            headers=response_headers
        )

        for cookie in cookie_list:
            response.headers.append(
                "set-cookie",
                cookie
            )

        return response

    # =====================================================
    # ストリーミング
    # =====================================================

    mode = request.cookies.get(
        "pyproxy_mode",
        "buffer"
    )

    if mode == "stream":
        async def iterator():
            try:
                async for chunk in upstream.aiter_bytes(
                    64 * 1024
                ):
                    yield chunk

            finally:
                await upstream.aclose()
                await client.aclose()

        response = StreamingResponse(
            iterator(),
            status_code=status_code,
            headers=response_headers
        )

        for cookie in cookie_list:
            response.headers.append(
                "set-cookie",
                cookie
            )

        return response

    # =====================================================
    # バッファ
    # =====================================================

    try:
        data = await upstream.aread()

    except Exception as exc:
        await upstream.aclose()
        await client.aclose()

        return Response(
            "Read error: " + str(exc),
            status_code=502,
            media_type="text/plain"
        )

    await upstream.aclose()
    await client.aclose()

    response = Response(
        content=data,
        status_code=status_code,
        headers=response_headers
    )

    for cookie in cookie_list:
        response.headers.append(
            "set-cookie",
            cookie
        )

    return response


# =========================================================
# トップページ
# =========================================================

INDEX_HTML = """
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>
<title>Python Web Proxy</title>

<style>
* {
    box-sizing: border-box;
}

html,
body {
    margin: 0;
    padding: 0;
}

body {
    min-height: 100vh;
    background: #202124;
    color: white;
    font-family: Arial, sans-serif;

    display: flex;
    justify-content: center;
    align-items: center;
}

.container {
    width: min(700px, 94vw);
}

h1 {
    text-align: center;
    font-size: 28px;
    margin-bottom: 25px;
}

form {
    display: flex;
    gap: 8px;
}

input {
    flex: 1;
    min-width: 0;
    height: 48px;
    padding: 0 14px;
    border: 1px solid #5f6368;
    border-radius: 7px;
    background: #303134;
    color: white;
    font-size: 16px;
}

button {
    height: 48px;
    padding: 0 18px;
    border: 0;
    border-radius: 7px;
    background: #8ab4f8;
    color: #202124;
    font-weight: bold;
    cursor: pointer;
}

button:hover {
    opacity: 0.9;
}

.info {
    margin-top: 18px;
    color: #bdc1c6;
    font-size: 13px;
    line-height: 1.6;
}
</style>
</head>

<body>

<div class="container">

<h1>Python Web Proxy</h1>

<form id="form">

<input
    id="url"
    type="text"
    placeholder="https://example.com/"
    autocomplete="off"
>

<button type="submit">
    開く
</button>

</form>

<div class="info">
    URLを入力して開いてください。
    ページ上部の操作バーから戻る・進む・再読み込み・
    転送方式の変更ができます。
</div>

</div>

<script>
document.getElementById("form").addEventListener(
    "submit",
    function (event) {
        event.preventDefault();

        let url =
            document.getElementById("url").value.trim();

        if (!url) {
            return;
        }

        if (!/^https?:\\/\\//i.test(url)) {
            url = "https://" + url;
        }

        location.href =
            "/p/" + encodeURIComponent(url);
    }
);
</script>

</body>
</html>
"""


# =========================================================
# Routes
# =========================================================

@app.get("/")
async def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/health")
async def health():
    return {
        "status": "ok"
    }


@app.api_route(
    "/p/{target:path}",
    methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS"
    ]
)
async def proxy(
    target: str,
    request: Request
):
    target = decode_target(target)

    return await proxy_request(
        request,
        target,
        True
    )


@app.api_route(
    "/f/{target:path}",
    methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS"
    ]
)
async def frame_proxy(
    target: str,
    request: Request
):
    target = decode_target(target)

    return await proxy_request(
        request,
        target,
        False
    )


@app.get("/favicon.ico")
async def favicon():
    return Response(
        status_code=204
    )


# =========================================================
# Render起動
# =========================================================

if __name__ == "__main__":
    import uvicorn

    port = int(
        os.environ.get(
            "PORT",
            "8000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
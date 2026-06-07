"""
_session.py — Hỗ trợ cả cookie string thô và appstate JSON
"""
import json
import requests
from _core._utils import parse_cookie_string, dataSplit


def appstate_to_cookie_string(appstate) -> str:
    """Chuyển appstate JSON (list hoặc dict) sang cookie string."""
    if isinstance(appstate, str):
        try:
            appstate = json.loads(appstate)
        except Exception:
            return appstate  # đã là cookie string rồi
    if isinstance(appstate, list):
        # Dạng [{key, value, ...}, ...]
        parts = []
        for item in appstate:
            if isinstance(item, dict):
                k = item.get("key") or item.get("name") or ""
                v = item.get("value", "")
                if k:
                    parts.append(f"{k}={v}")
        return "; ".join(parts)
    if isinstance(appstate, dict):
        # Dạng {key: value, ...}
        return "; ".join(f"{k}={v}" for k, v in appstate.items())
    return str(appstate)


def dataGetHome(setCookies):
    # Nếu là JSON appstate → chuyển sang cookie string
    cookie_str = appstate_to_cookie_string(setCookies)

    mainRequests = {
        "headers": {
            "authority": "www.facebook.com",
            "method": "GET",
            "path": "/",
            "scheme": "https",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "accept-language": "vi-VN,vi;q=0.9,en-US;q=0.6,en;q=0.5",
            "cache-control": "max-age=0",
            "cookie": cookie_str,
            "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        },
        "timeout": 30,
        "url": "https://www.facebook.com/",
        "cookies": parse_cookie_string(cookie_str),
        "verify": True,
    }

    dictValueSaved = {}
    splitDataList = [
        ["fb_dtsg",        'DTSGInitialData",[],{"token":"',  '"'],
        ["fb_dtsg_ag",     'async_get_token":"',               '"'],
        ["jazoest",        "jazoest=",                         '"'],
        ["hash",           'hash":"',                          '"'],
        ["sessionID",      'sessionId":"',                     '"'],
        ["FacebookID",     '"actorID":"',                      '"'],
        ["clientRevision", "client_revision\":",               ","],
    ]

    sendRequests = requests.get(**mainRequests).text
    for i in splitDataList:
        nameValue = i[0]
        try:
            exportValue = dataSplit(i[1], i[2], HTML=sendRequests, defaultValue=True)
        except (IndexError, AttributeError, TypeError):
            exportValue = f"Unable to retrieve {nameValue}"
        dictValueSaved[nameValue] = exportValue

    dictValueSaved["cookieFacebook"] = cookie_str
    dictValueSaved["rawCookies"]     = setCookies  # lưu lại để refresh
    return dictValueSaved

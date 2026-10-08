"""
채용 터미널 웹사이트 (GitHub Pages) 데이터 만들기
- 노션 「📥 채용 알림 검토함」(처리 안 된 것), 「📆 취업 지원 현황」, 「📈 시장 지표」를 읽어서
- _site/data.json 을 만들고, site/index.html 을 _site/ 로 복사한다
- 저장소 비밀값 DASHBOARD_PASSWORD 가 있으면 데이터를 암호화해서 비밀번호를 아는 브라우저에서만 풀리게 하고
  (AES-GCM, PBKDF2-SHA256), 없으면 그대로 공개한다
워크플로: .github/workflows/dashboard_site.yml (1시간마다)
"""
import base64
import datetime
import hashlib
import json
import os
import shutil

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

NOTION_API = "https://api.notion.com/v1"
INBOX_DS = "8095e74e-421b-4db6-a779-a60c6053f455"     # 📥 채용 알림 검토함
TRACKER_DS = "4b154a4c-9165-82f4-a104-077d95d31780"   # 📆 취업 지원 현황
MARKET_DS = "f4d5c974-a581-4315-b912-5ae909c5dd62"    # 📈 시장 지표

# 브라우저(site/index.html)와 똑같이 맞춰야 하는 값
KDF_ITERATIONS = 250_000
KDF_SALT = hashlib.sha256(b"job-terminal-site/v1").digest()[:16]   # 고정 salt → 기기에 키를 기억해 둘 수 있게

OUT = "_site"
KST = datetime.timezone(datetime.timedelta(hours=9))


def _notion(method, path, body=None):
    r = requests.request(method, NOTION_API + path, json=body, timeout=30, headers={
        "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    })
    if r.status_code >= 300:
        raise RuntimeError(f"노션 {r.status_code} {path}: {r.text[:200]}")
    return r.json()


def _query(ds, filter_=None):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if filter_:
            body["filter"] = filter_
        if cursor:
            body["start_cursor"] = cursor
        data = _notion("POST", f"/data_sources/{ds}/query", body)
        rows += data["results"]
        if not data.get("has_more"):
            return rows
        cursor = data["next_cursor"]


def _value(p):
    """노션 속성 하나를 단순한 값으로"""
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v or [])
    if t in ("select", "status"):
        return (v or {}).get("name")
    if t == "multi_select":
        return [x["name"] for x in v or []]
    if t == "date":
        return (v or {}).get("start")
    if t in ("number", "checkbox", "url"):
        return v
    return None


def _rows(pages, keys):
    out = []
    for pg in pages:
        props = pg["properties"]
        row = {"url": pg["url"]}
        for k in keys:
            if k in props:
                row[k] = _value(props[k])
        out.append(row)
    return out


def collect():
    inbox = _rows(_query(INBOX_DS, {"property": "처리", "select": {"is_empty": True}}),
                  ["기업명", "공고명", "마감일", "입사 유형", "회사규모", "링크", "메모", "출처"])
    tracker = _rows(_query(TRACKER_DS),
                    ["기업명", "지원상태", "상태", "마감일", "지원일", "1차", "회사규모", "산업군"])
    market = _rows(_query(MARKET_DS),
                   ["지표", "티커", "그룹", "값", "변동", "단위", "추이", "순서", "메모", "기준시각"])
    return {"builtAt": datetime.datetime.now(KST).isoformat(timespec="minutes"),
            "inbox": inbox, "tracker": tracker, "market": market}


def encrypt(payload: dict, password: str) -> dict:
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=KDF_SALT,
                     iterations=KDF_ITERATIONS).derive(password.encode("utf-8"))
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(payload, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "iter": KDF_ITERATIONS, "salt": b64(KDF_SALT), "iv": b64(iv), "ct": b64(ct)}


def main():
    # 비밀번호(DASHBOARD_PASSWORD)가 있으면 암호화, 없으면 그대로 공개 (사용자 선택: 2026-10 현재 비밀번호 없이 공개)
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if password and len(password) < 8:
        raise SystemExit("DASHBOARD_PASSWORD 가 8자보다 짧아요")
    data = collect()
    os.makedirs(OUT, exist_ok=True)
    shutil.copy("site/index.html", os.path.join(OUT, "index.html"))
    with open(os.path.join(OUT, "data.json"), "w", encoding="utf-8") as f:
        json.dump(encrypt(data, password) if password else {"v": 0, "data": data}, f, ensure_ascii=False)
    mode = "암호화" if password else "공개"
    print(f"[site] ({mode}) 검토함 {len(data['inbox'])} · 지원 현황 {len(data['tracker'])} · 시장 {len(data['market'])} → {OUT}/")


if __name__ == "__main__":
    main()

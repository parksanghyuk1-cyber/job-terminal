"""
채용 터미널 웹사이트의 버튼 처리 (승인 · 제외 · 단계 옮기기 · 삭제) → 노션에 반영
- 사이트(site/index.html)나 채용 터미널 대시보드(PC 연결 프로그램 경유)가 GitHub API로
  .github/workflows/site_action.yml 을 실행하면서 넘긴 값을 받는다
  ACTION   approve | skip | move | delete
  PAGE_ID  노션 페이지 id (검토함 줄 또는 취업 지원 현황 줄)
  VALUE    move 일 때 옮길 단계 (지원 전 · 서류 작성 중 · 지원 완료 · 전형 결과들)
- approve: 검토함 공고를 「취업 지원 현황」에 등록 (본문=검토함 본문 그대로 복사, 전형·근무지·산업군은 Gemini로 채움) → 검토함 처리=등록됨
- skip:    검토함 처리=제외
- move:    지원상태/상태 바꾸기 (채용 터미널 대시보드의 '옮기기'와 같은 규칙)
- delete:  공고를 노션 휴지통으로 (취업 지원 현황·검토함 줄만)
"""
import datetime
import json
import os
import re
import sys
import time

import requests

NOTION_API = "https://api.notion.com/v1"
INBOX_DS = "8095e74e-421b-4db6-a779-a60c6053f455"     # 📥 채용 알림 검토함
TRACKER_DS = "4b154a4c-9165-82f4-a104-077d95d31780"   # 📆 취업 지원 현황
KST = datetime.timezone(datetime.timedelta(hours=9))

APPLY = ["지원 전", "서류 작성 중", "지원 완료"]
RESULTS = ["적성 검사", "필기전형", "1차면접", "최종면접", "서류합격", "필기합격", "면접합격", "최종합격",
           "서류탈락", "역검탈락", "필기탈락", "면접탈락", "최종탈락"]
FINANCE = ["증권", "자산운용", "투자", "은행", "보험", "생명", "손해", "카드", "캐피탈", "금융", "신탁", "저축"]
INDUSTRIES = ["금융", "제조업", "정유", "건설", "식품", "통신", "방산", "물류유통", "컨설팅"]
SIZES = ["대기업", "중견기업", "공기업/공공기관", "외국계", "중소기업", "스타트업", "강소기업", "상장사"]


# ── 노션 API ─────────────────────────────────────────────
def _notion(method, path, body=None):
    r = requests.request(method, NOTION_API + path, json=body, timeout=30, headers={
        "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    })
    if r.status_code >= 300:
        raise RuntimeError(f"노션 {r.status_code} {path}: {r.text[:300]}")
    return r.json()


def _plain(prop):
    items = (prop or {}).get("title") or (prop or {}).get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in items)


def _sel(prop):
    return ((prop or {}).get("select") or (prop or {}).get("status") or {}).get("name")


def _rt(text):
    return [{"type": "text", "text": {"content": text[i:i + 2000]}} for i in range(0, len(text), 2000)]


# ── 본문 복사 (검토함 → 취업 지원 현황) ────────────────────
COPY_TYPES = {"paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item",
              "to_do", "toggle", "quote", "callout", "divider", "code", "bookmark", "embed", "image"}
NESTABLE = {"paragraph", "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout",
            "heading_1", "heading_2", "heading_3"}


def _children(block_id):
    out, cursor = [], None
    while True:
        q = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        data = _notion("GET", f"/blocks/{block_id}/children{q}")
        out += data["results"]
        if not data.get("has_more"):
            return out
        cursor = data["next_cursor"]


def _clean_rt(items):
    out = []
    for it in items or []:
        if it.get("type") == "text":
            t = {"content": it["text"]["content"][:2000]}
            if (it["text"].get("link") or {}).get("url"):
                t["link"] = {"url": it["text"]["link"]["url"]}
        else:   # 멘션·수식은 글자로
            t = {"content": (it.get("plain_text") or "")[:2000]}
            if it.get("href"):
                t["link"] = {"url": it["href"]}
        out.append({"type": "text", "text": t, "annotations": it.get("annotations", {})})
    return out


def _clean_block(b, depth=0):
    t = b.get("type")
    if t not in COPY_TYPES:
        return None
    src = b.get(t) or {}
    if t == "image" and src.get("type") != "external":   # 노션에 올라간 이미지는 주소가 만료돼서 복사 불가 → 건너뜀
        return None
    dst = {}
    for k in ("color", "checked", "is_toggleable", "language", "url"):
        if k in src:
            dst[k] = src[k]
    if "rich_text" in src:
        dst["rich_text"] = _clean_rt(src["rich_text"])
    if t == "callout" and (src.get("icon") or {}).get("type") == "emoji":
        dst["icon"] = src["icon"]
    if t == "image":
        dst = {"type": "external", "external": {"url": src["external"]["url"]}}
    if t == "divider":
        dst = {}
    if b.get("has_children") and t in NESTABLE and depth == 0:
        kids = [c for c in (_clean_block(x, 1) for x in _children(b["id"])) if c]
        if kids:
            dst["children"] = kids[:100]
    return {"object": "block", "type": t, t: dst}


def _text_of(blocks):
    """Gemini에 보낼 글자만 (공고·문항)"""
    lines = []
    for b in blocks:
        t = b["type"]
        lines.append("".join(x["text"]["content"] for x in b[t].get("rich_text", [])))
        for c in b[t].get("children", []):
            lines.append("".join(x["text"]["content"] for x in c[c["type"]].get("rich_text", [])))
    return "\n".join(l for l in lines if l.strip())


# ── Gemini: 전형·근무지·산업군·회사규모 뽑기 (대시보드 승인과 같은 기준) ──
def extract(company, title, size, text):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return {}
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return {}
    prompt = f"""채용공고에서 정보를 뽑아 JSON 하나만 답해 주세요.

회사: {company}
공고명: {title}
지원자가 노리는 직무: 재무·회계·자금·경영기획·전략기획·경영지원 계열 (직무마다 근무지나 전형이 다르면 이 직무 기준)

답 형식: {{"steps": ["서류", "인적성검사", "1차면접"], "location": "서울 (본사)", "industry": "정유", "size": ""}}

규칙:
- steps: 공고에 적힌 전형 절차를 순서대로 짧게. 최종합격·입사·처우협의는 빼기. 5개보다 많으면 5번째 칸에 나머지를 "/"로 합치기. 없으면 [].
- location: 공고에 적힌 근무지만. 없으면 "".
- industry: 주력 사업 하나. 가능하면 이 중에서: {", ".join(INDUSTRIES)}. 맞는 게 없을 때만 짧은 새 이름.
- size: {f'이미 "{size}"이니 ""로 두기.' if size else f'확실할 때만 이 중 하나: {", ".join(SIZES)}. "대기업"은 공정위 공시대상기업집단 소속일 때만. 모르면 "".'}
- 추측하지 말 것. 원문은 이미지에서 자동으로 읽은 글자라 오타가 있을 수 있음.

공고 원문:
{text[:14000] or "(원문 없음)"}"""
    client = genai.Client(api_key=key)   # 변수로 잡아 둬야 요청 중에 닫히지 않는다
    cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
    last = ""
    # 붐빌 때(503 등) 잠깐 쉬었다 다시, 그래도 안 되면 가벼운 모델로
    for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
        for wait in (0, 6, 15):
            time.sleep(wait)
            try:
                out = json.loads(client.models.generate_content(model=model, contents=prompt, config=cfg).text)
                return out if isinstance(out, dict) else {}
            except Exception as e:
                last = str(e)[:150]
                if not re.search(r"\b(429|500|503|UNAVAILABLE|RESOURCE_EXHAUSTED|overloaded)\b", last):
                    break   # 일시적인 오류가 아니면 다음 모델로
    print(f"[gemini] 실패: {last} → 전형·근무지는 비워 둠")
    return {}


# ── 동작 ─────────────────────────────────────────────────
def approve(page_id):
    page = _notion("GET", f"/pages/{page_id}")
    pr = page["properties"]
    if _sel(pr.get("처리")):
        print(f"이미 처리된 공고예요 ({_sel(pr.get('처리'))}) → 그대로 둠")
        return
    company = _plain(pr["기업명"]).strip()
    deadline = (pr.get("마감일", {}).get("date") or {}).get("start")
    kind = _sel(pr.get("입사 유형"))
    size = _sel(pr.get("회사규모")) or ""
    source = _sel(pr.get("출처"))

    same = _notion("POST", f"/data_sources/{TRACKER_DS}/query",
                   {"filter": {"property": "기업명", "title": {"equals": company}}, "page_size": 100})["results"]
    if any(not deadline or ((r["properties"]["마감일"].get("date") or {}).get("start") or "")[:10] == deadline[:10] for r in same):
        print(f"{company}: 이미 취업 지원 현황에 있어요 → 검토함만 정리")
        _notion("PATCH", f"/pages/{page_id}", {"properties": {"처리": {"select": {"name": "등록됨"}}}})
        return

    blocks = [c for c in (_clean_block(b) for b in _children(page_id)) if c]
    info = extract(company, _plain(pr.get("공고명")), size, _text_of(blocks))

    props = {
        "기업명": {"title": _rt(company)},
        "지원상태": {"status": {"name": "지원 전"}},
        "상태": {"status": {"name": "지원 전"}},
    }
    if deadline:
        props["마감일"] = {"date": {"start": deadline}}
    if kind:
        props["입사 유형"] = {"multi_select": [{"name": kind}]}
    steps = [str(s).strip() for s in (info.get("steps") or []) if str(s).strip()]
    if len(steps) > 5:
        steps = steps[:4] + ["/".join(steps[4:])]
    for i, s in enumerate(steps):
        props[f"{i + 1}차"] = {"rich_text": _rt(s)}
    if str(info.get("location") or "").strip():
        props["근무지"] = {"rich_text": _rt(str(info["location"]).strip())}
    size = size or (info.get("size") if info.get("size") in SIZES else "")
    if size:
        props["회사규모"] = {"multi_select": [{"name": size}]}
    industry = "금융" if source == "금융투자협회" or any(w in company for w in FINANCE) else ""
    industry = str(info.get("industry") or "").strip() or industry
    if industry:
        props["산업군"] = {"multi_select": [{"name": industry[:50]}]}

    created = _notion("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": TRACKER_DS},
                                         "properties": props, "children": blocks[:100]})
    for i in range(100, len(blocks), 100):   # 본문이 길면 나눠서 붙인다
        _notion("PATCH", f"/blocks/{created['id']}/children", {"children": blocks[i:i + 100]})
    _notion("PATCH", f"/pages/{page_id}", {"properties": {"처리": {"select": {"name": "등록됨"}}}})
    print(f"{company}: 등록 완료 (전형 {' → '.join(steps) or '-'} · 근무지 {info.get('location') or '-'} · 산업군 {industry or '-'})")


def skip(page_id):
    _notion("PATCH", f"/pages/{page_id}", {"properties": {"처리": {"select": {"name": "제외"}}}})
    print("검토함: 제외로 표시")


def move(page_id, value):
    if value not in APPLY + RESULTS:
        raise SystemExit(f"모르는 단계예요: {value}")
    pr = _notion("GET", f"/pages/{page_id}")["properties"]
    cur_apply, cur_state = _sel(pr.get("지원상태")), _sel(pr.get("상태"))
    props = {}
    if value in APPLY:
        props["지원상태"] = {"status": {"name": value}}
        if value == "지원 완료":
            if not cur_state or cur_state == "지원 전":
                props["상태"] = {"status": {"name": "서류제출"}}
            if not (pr.get("지원일", {}).get("date") or {}).get("start"):
                props["지원일"] = {"date": {"start": datetime.datetime.now(KST).date().isoformat()}}
        elif cur_state and cur_state not in ("지원 전", "서류제출"):
            props["상태"] = {"status": {"name": "지원 전"}}
    else:
        props["상태"] = {"status": {"name": value}}
        if cur_apply != "지원 완료":
            props["지원상태"] = {"status": {"name": "지원 완료"}}
    _notion("PATCH", f"/pages/{page_id}", {"properties": props})
    print(f"단계 옮김 → {value}")


def delete(page_id):
    """공고 삭제 = 노션 휴지통으로 (30일 안에 노션에서 되살릴 수 있음). 채용 DB 두 곳의 줄만 지운다"""
    page = _notion("GET", f"/pages/{page_id}")
    parent = (page.get("parent") or {}).get("data_source_id", "").replace("-", "")
    if parent not in (TRACKER_DS.replace("-", ""), INBOX_DS.replace("-", "")):
        raise SystemExit("취업 지원 현황·검토함의 공고만 지울 수 있어요")
    if page.get("in_trash"):
        print("이미 휴지통에 있어요")
        return
    _notion("PATCH", f"/pages/{page_id}", {"in_trash": True})
    print(f"휴지통으로: {_plain(page['properties'].get('기업명'))}")


def trash_test(page_id):
    """시험용으로 만든 '[테스트] …' 줄만 휴지통으로 (사이트 버튼에는 없음, 수동 실행용)"""
    page = _notion("GET", f"/pages/{page_id}")
    title = _plain(page["properties"].get("기업명"))
    if not title.startswith("[테스트]"):
        raise SystemExit(f"'[테스트]'로 시작하는 줄만 지울 수 있어요: {title}")
    _notion("PATCH", f"/pages/{page_id}", {"in_trash": True})
    print(f"휴지통으로: {title}")


def main():
    action = os.environ.get("ACTION", "").strip()
    raw = os.environ.get("PAGE_ID", "").strip().replace("-", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        raise SystemExit("노션 페이지 id가 이상해요")
    page_id = f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    if action == "approve":
        approve(page_id)
    elif action == "skip":
        skip(page_id)
    elif action == "move":
        move(page_id, os.environ.get("VALUE", "").strip())
    elif action == "delete":
        delete(page_id)
    elif action == "trash_test":
        trash_test(page_id)
    else:
        raise SystemExit(f"모르는 동작이에요: {action}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)

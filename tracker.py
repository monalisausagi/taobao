"""유니패스 통관 상태를 조회해서 상태가 바뀌면 Gmail SMTP로 알림을 보내는 스크립트.

환경변수(GitHub Secrets): WAYBILL_NO, GMAIL_USER, GMAIL_PASS, RECEIVER_EMAIL
 - 발송은 Gmail SMTP, RECEIVER_EMAIL은 네이버 등 어떤 주소여도 됨.
실패 시 debug/ 폴더에 스크린샷, 모든 프레임의 HTML/텍스트, 사유를 저장함.
"""
import hashlib
import html as htmllib
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = "https://unipass.customs.go.kr/csp/index.do"
STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")

DATE_RE = re.compile(r"\d{4}[.\-/]\d{2}[.\-/]\d{2}")
NO_DATA_RE = re.compile(
    r"(조회\s*(결과|내역|데이터)\s*(가|는|이)?\s*(존재하지|없))"
    r"|(조회\s*된\s*(결과|내역|데이터|정보)?\s*(가|이)?\s*없)"
)
SUMMARY_KEYS = [
    "화물관리번호", "통관진행상태", "진행상태", "품명", "품목", "입항일", "입항일자",
    "선박/항공편명", "선박명", "항공편명", "적재항", "포장개수", "중량",
    "화물구분", "특송업체", "B/L", "BL",
]

TABLES_JS = """
() => [...document.querySelectorAll('table')].map(t => ({
  rows: [...t.querySelectorAll('tr')].map(tr =>
    [...tr.children].map(c => ({
      tag: c.tagName,
      text: (c.innerText || '').replace(/\\s+/g, ' ').trim()
    })))
}))
"""


def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"[오류] 환경변수 {name} 가 비어 있어요. Secrets를 확인하세요.")
    return v


# ---------------------------------------------------------------- 파싱 (순수 함수)
def parse_tables(tables):
    """표 목록 -> (요약 dict, 처리단계 리스트). 못 찾으면 steps는 빈 리스트."""
    summary, steps = {}, []
    for t in tables:
        rows = t["rows"]
        header, hidx = None, None
        for i, row in enumerate(rows):
            if any("처리단계" in c["text"] for c in row):
                header, hidx = [c["text"] for c in row], i
                break
        if header is not None:
            for row in rows[hidx + 1:]:
                cells = [c["text"] for c in row]
                if len(cells) < 2 or not any(DATE_RE.search(c) for c in cells):
                    continue
                item = {h or f"col{j}": (cells[j] if j < len(cells) else "")
                        for j, h in enumerate(header)}
                if item not in steps:
                    steps.append(item)
            continue
        # 요약 표: th -> td 쌍
        for row in rows:
            for j, c in enumerate(row[:-1]):
                nxt = row[j + 1]
                if c["tag"] == "TH" and nxt["tag"] == "TD" and c["text"] and nxt["text"]:
                    summary.setdefault(c["text"], nxt["text"])
    wanted = {k: v for k, v in summary.items() if any(w in k for w in SUMMARY_KEYS)}
    return (wanted or dict(list(summary.items())[:12])), steps


def step_time(step):
    for v in step.values():
        m = DATE_RE.search(v)
        if m:
            return v
    return ""


def step_label(step):
    for k, v in step.items():
        if "처리단계" in k:
            return v
    return next(iter(step.values()), "")


# ---------------------------------------------------------------- 브라우저
def all_frames(pages):
    for pg in pages:
        yield from pg.frames


def collect(ctx):
    """열린 모든 페이지/프레임에서 표 데이터와 텍스트 수집."""
    tables, text = [], ""
    for fr in all_frames(ctx.pages):
        try:
            tables += fr.evaluate(TABLES_JS)
            text += "\n" + fr.locator("body").inner_text(timeout=3000)
        except Exception:
            pass
    return tables, text


def dump_debug(ctx, reason, dialogs):
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "reason.txt").write_text(
        f"{reason}\n\n[dialogs]\n" + "\n".join(dialogs), encoding="utf-8")
    for i, pg in enumerate(ctx.pages):
        try:
            pg.screenshot(path=str(DEBUG_DIR / f"page{i}.png"), full_page=True)
        except Exception as e:
            print(f"[경고] 스크린샷 실패: {e}")
        for j, fr in enumerate(pg.frames):
            try:
                (DEBUG_DIR / f"page{i}_frame{j}.html").write_text(fr.content(), encoding="utf-8")
                (DEBUG_DIR / f"page{i}_frame{j}.txt").write_text(
                    fr.locator("body").inner_text(timeout=3000), encoding="utf-8")
            except Exception:
                pass


def scrape(waybill):
    """(요약, 처리단계 리스트) 반환. 조회 결과 없음이면 ({}, []). 파싱 실패는 예외."""
    year = str(datetime.now().year)
    dialogs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="ko-KR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            viewport={"width": 1400, "height": 1000},
        )

        def on_dialog(d):  # alert("조회된 내역이 없습니다") 같은 팝업 대응
            dialogs.append(d.message)
            d.accept()

        ctx.on("page", lambda pg: pg.on("dialog", on_dialog))
        page = ctx.new_page()
        try:
            page.goto(URL, wait_until="networkidle", timeout=60000)
            page.click("label[for='INDEX_cargoS02']")
            page.fill("#INDEX_hblNo", waybill)
            page.select_option("#INDEX_blYy", year)
            page.click("#INDEX_btnRetrieveCargPrgsInfo")

            # 결과 대기: 새 창/같은 페이지/iframe 어디든 '처리단계' 표가 나오거나,
            # '없음' 메시지(alert 포함)가 나올 때까지 최대 40초 폴링
            summary, steps, empty_count = {}, [], 0
            for _ in range(20):
                page.wait_for_timeout(2000)
                if any(NO_DATA_RE.search(m) for m in dialogs):
                    return {}, []
                tables, text = collect(ctx)
                summary, steps = parse_tables(tables)
                if steps:
                    return summary, steps
                # 조회 직후 로딩 전 빈 표와 구분하려고 연속 3회(약 6초) 확인
                empty_count = empty_count + 1 if NO_DATA_RE.search(text) else 0
                if empty_count >= 3:
                    return {}, []
            raise RuntimeError("40초 안에 처리단계 표를 찾지 못했어요")
        except Exception as e:
            dump_debug(ctx, f"{type(e).__name__}: {e}", dialogs)
            raise
        finally:
            browser.close()


# ---------------------------------------------------------------- 메일
def build_mail(waybill, summary, steps):
    latest = steps[-1]
    s_rows = "".join(
        f"<tr><th align='left'>{htmllib.escape(k)}</th><td>{htmllib.escape(v)}</td></tr>"
        for k, v in summary.items())
    cols = list(steps[0].keys())
    head = "".join(f"<th>{htmllib.escape(c)}</th>" for c in cols)
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{htmllib.escape(s.get(c, ''))}</td>" for c in cols) + "</tr>"
        for s in steps)
    html = (
        f"<h3>통관 상태 변경: {htmllib.escape(step_label(latest))}</h3>"
        f"<p>운송장: {htmllib.escape(waybill)}</p>"
        f"<table border='1' cellpadding='4' style='border-collapse:collapse'>{s_rows}</table><br>"
        f"<table border='1' cellpadding='4' style='border-collapse:collapse'>"
        f"<tr>{head}</tr>{body_rows}</table>"
        f"<p><a href='{URL}'>유니패스 열기</a></p>")
    text = (f"운송장: {waybill}\n최신 단계: {step_label(latest)} ({step_time(latest)})\n\n"
            + "\n".join(f"{k}: {v}" for k, v in summary.items())
            + "\n\n[처리 이력]\n"
            + "\n".join(" | ".join(s.values()) for s in steps) + f"\n\n{URL}\n")
    return f"[통관 알림] {step_label(latest)} - {waybill}", text, html


def send_mail(user, password, receiver, subject, text, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, receiver
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


def main():
    waybill = env("WAYBILL_NO")
    user, pw, rcv = env("GMAIL_USER"), env("GMAIL_PASS"), env("RECEIVER_EMAIL")

    try:
        summary, steps = scrape(waybill)
    except Exception as e:
        print(f"[경고] 조회 실패: {e}")
        sys.exit(1)  # 워크플로우가 debug/ 를 업로드하도록 실패 처리

    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}

    if not steps:
        # 조회 결과 없음: 같은 상태가 이어지는 동안은 한 번만 알림 (15분마다 스팸 방지)
        if state.get(waybill) == "NO_DATA":
            print("[변경 없음] 아직 조회되는 정보 없음 (안내 메일 이미 발송)")
            return
        subject = f"[통관 알림] 아직 물건이 준비되지 않았습니다 - {waybill}"
        text = (f"운송장 {waybill} 는 유니패스에서 아직 조회되지 않습니다.\n"
                "물건이 아직 준비되지 않았습니다. (한국 입항 전이거나 세관에 신고되기 전)\n"
                "조회되기 시작하면 다시 알려드릴게요.\n\n"
                "※ 계속 조회되지 않으면 H B/L 번호가 운송장 번호와 다른 건 아닌지 확인해 보세요.\n"
                f"{URL}\n")
        html = "<p>" + htmllib.escape(text).replace("\n", "<br>") + "</p>"
        send_mail(user, pw, rcv, subject, text, html)
        state[waybill] = "NO_DATA"
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        print(f"[알림 발송] {subject}")
        return

    digest = hashlib.sha256(json.dumps(steps, ensure_ascii=False).encode()).hexdigest()
    if state.get(waybill) == digest:
        print(f"[변경 없음] 최근: {step_label(steps[-1])} ({step_time(steps[-1])})")
        return

    subject, text, html = build_mail(waybill, summary, steps)
    send_mail(user, pw, rcv, subject, text, html)
    state[waybill] = digest
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    print(f"[알림 발송] {subject}")


if __name__ == "__main__":
    main()

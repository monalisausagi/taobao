"""유니패스 웹페이지를 직접 조회해서 통관 상태가 바뀌면 Gmail로 알리는 스크립트.
(API 키 불필요 / 대신 웹페이지 구조가 바뀌면 깨질 수 있음)

환경변수(GitHub Secrets):
  WAYBILL_NO, GMAIL_USER, GMAIL_PASS, RECEIVER_EMAIL
"""
import hashlib
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = "https://unipass.customs.go.kr/csp/index.do"
STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")

DATE_RE = re.compile(r"\d{4}[.\-/]\d{2}[.\-/]\d{2}")
KEYWORDS = [
    "입항", "하선", "하기", "반입", "반출", "통관목록", "수입신고",
    "수리", "심사", "보류", "반송", "적하목록",
]


def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"[오류] 환경변수 {name} 가 비어 있어요. Secrets를 확인하세요.")
    return v


def all_frames(page):
    return [page] + [f for f in page.frames if f != page.main_frame]


def find_input(page):
    """H B/L(운송장) 입력칸을 최대한 유연하게 찾는다."""
    selectors = [
        "input[id*='hbl' i]",
        "input[name*='hbl' i]",
        "input[title*='H B/L' i]",
        "input[title*='HBL' i]",
        "input[title*='운송장' i]",
        "input[placeholder*='운송장' i]",
    ]
    for fr in all_frames(page):
        for sel in selectors:
            loc = fr.locator(sel)
            for i in range(loc.count()):
                el = loc.nth(i)
                if el.is_visible():
                    return fr, el
    return None, None


def dump_debug(page, reason):
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
        (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")
        (DEBUG_DIR / "reason.txt").write_text(reason, encoding="utf-8")
    except Exception as e:
        print(f"[경고] 디버그 저장 실패: {e}")


def scrape(waybill):
    """통관 처리 이력 줄들을 리스트로 반환. 실패 시 예외."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 1000},
        )
        page = ctx.new_page()
        try:
            page.goto(URL, wait_until="networkidle", timeout=60000)

            fr, box = find_input(page)
            if box is None:
                # 메뉴를 한 번 눌러서 조회 화면으로 이동해 본다
                for label in ["화물통관진행정보", "화물진행정보", "수입화물진행정보"]:
                    link = page.get_by_text(label, exact=False).first
                    if link.count() and link.is_visible():
                        link.click()
                        page.wait_for_load_state("networkidle")
                        break
                fr, box = find_input(page)
            if box is None:
                raise RuntimeError("운송장 입력칸을 찾지 못했어요 (페이지 구조 변경 가능성)")

            box.fill(waybill)
            btn = fr.get_by_role("button", name=re.compile("조회")).first
            if btn.count():
                btn.click()
            else:
                box.press("Enter")
            page.wait_for_timeout(5000)  # 결과 렌더링 대기

            text = ""
            for f in all_frames(page):
                try:
                    text += "\n" + f.locator("body").inner_text(timeout=5000)
                except Exception:
                    pass

            if "조회된 데이터가 없" in text or "조회결과가 없" in text:
                return []

            lines = []
            for raw in text.splitlines():
                line = re.sub(r"\s+", " ", raw).strip()
                if DATE_RE.search(line) and any(k in line for k in KEYWORDS):
                    if line not in lines:
                        lines.append(line)
            if not lines:
                raise RuntimeError("결과 화면에서 처리단계를 읽지 못했어요")
            return lines
        except (PWTimeout, RuntimeError) as e:
            dump_debug(page, str(e))
            raise
        finally:
            browser.close()


def send_mail(user, password, receiver, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, receiver
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


def main():
    waybill = env("WAYBILL_NO")
    user, pw, rcv = env("GMAIL_USER"), env("GMAIL_PASS"), env("RECEIVER_EMAIL")

    try:
        lines = scrape(waybill)
    except Exception as e:
        print(f"[경고] 조회 실패: {e}")
        sys.exit(1)  # 워크플로우에서 debug 파일을 업로드하도록 실패 처리

    if not lines:
        print("[정보] 아직 조회되는 통관 정보가 없어요.")
        return

    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    if state.get(waybill) == digest:
        print(f"[변경 없음] 최근: {lines[-1]}")
        return

    body = (
        f"운송장: {waybill}\n"
        f"최근 상태: {lines[-1]}\n\n"
        "[조회된 이력]\n" + "\n".join(f"- {l}" for l in lines) + f"\n\n{URL}\n"
    )
    send_mail(user, pw, rcv, f"[통관 알림] {waybill} 상태 변경", body)
    state[waybill] = digest
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    print(f"[알림 발송] {lines[-1]}")


if __name__ == "__main__":
    main()

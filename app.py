"""중국 → 한국 국제택배 추적 웹 앱 v2 (Streamlit + Pydeck)

실행:  streamlit run app.py
필요:  pip install -r requirements.txt   (streamlit, pydeck, requests)

- 경로: 다중 경유지를 Catmull-Rom 스플라인으로 부드럽게 만든 PathLayer (지도 표면에 밀착)
- 현재 위치: 해상=🚢, 육상=🚚 아이콘이 경로 좌표 위에서 단계 진행률만큼 이동
- 지도: Carto 기본 스타일(Mapbox 토큰 불필요), 한글은 TextLayer 대신 툴팁으로만 표시

※ 배송 단계는 송장번호 기반 '데모 데이터'이고 ETA는 거리·단계별 평균시간 규칙 기반 예측입니다.
   실제 연동은 fetch_tracking() 만 교체하세요.
※ 경유지 좌표는 시각적 근사값이라 실제 항로/도로와 약간 다를 수 있습니다.
"""
import hashlib
import math
import time
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pydeck as pdk
import requests
import streamlit as st

KST = ZoneInfo("Asia/Seoul")
st.set_page_config(page_title="중국→한국 택배 추적", page_icon="📦", layout="wide")

# ============================================================================
# 1. 경유지 데이터  (좌표: [경도, 위도])
# ============================================================================
CN_ORIGIN = [117.00, 36.67]   # 중국 산둥성 지난 물류센터
CN_PORT = [120.32, 36.08]     # 칭다오항
PORT = [126.60, 37.45]        # 인천항
DEFAULT_DEST = [129.04, 35.34]

# 중국 내륙 고속도로 구간 (지난 → 칭다오항)
CN_NODES = [CN_ORIGIN, [118.05, 36.81], [119.15, 36.70], [119.75, 36.38],
            [120.00, 36.27], CN_PORT]

# 서해 해상 항로 (칭다오항 → 인천항): 굽이치는 웨이포인트
SEA_NODES = [CN_PORT, [120.60, 35.92], [121.60, 35.78], [122.80, 35.95],
             [123.90, 36.25], [124.90, 36.65], [125.60, 36.95], [126.00, 37.05],
             [126.30, 37.20], [126.45, 37.30], PORT]

# 국내 고속도로 회랑 (인천항에서 출발, 목적지에서 가장 가까운 경유지까지 사용)
_TRUNK = [("인천항", PORT), ("시흥", [126.80, 37.37]), ("수원", [127.02, 37.25])]
_TO_DAEJEON = _TRUNK + [("천안", [127.14, 36.82]), ("대전", [127.40, 36.37])]
CORRIDORS = {
    "경부": _TO_DAEJEON + [("김천", [128.10, 36.13]), ("대구", [128.58, 35.88]),
                          ("영천", [128.93, 35.96]), ("경주", [129.21, 35.85]),
                          ("울산 언양", [129.12, 35.56])],
    "호남": _TO_DAEJEON + [("논산", [127.10, 36.19]), ("전주", [127.15, 35.82]),
                          ("광주", [126.85, 35.16])],
    "영동": _TRUNK + [("이천", [127.44, 37.28]), ("원주", [127.95, 37.34]),
                     ("강릉", [128.90, 37.75])],
}

CITY_FALLBACK = {
    "양산": [129.04, 35.34], "부산": [129.08, 35.18], "울산": [129.31, 35.54],
    "김해": [128.88, 35.23], "창원": [128.68, 35.23], "대구": [128.60, 35.87],
    "대전": [127.38, 36.35], "광주": [126.85, 35.16], "전주": [127.15, 35.82],
    "서울": [126.98, 37.57], "수원": [127.03, 37.26], "인천": [126.71, 37.46],
    "청주": [127.49, 36.64], "포항": [129.37, 36.02], "강릉": [128.90, 37.75],
}

STAGE_MARKER = {0: "🚚", 1: "🛃", 2: "🚢", 3: "⚓", 4: "🏛️", 5: "🚚", 6: "🚚", 7: "🏠"}


# ============================================================================
# 2. 지리 · 경로 유틸
# ============================================================================
def haversine_km(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def path_length_km(path):
    return sum(haversine_km(a, b) for a, b in zip(path, path[1:]))


def smooth(points, n=14):
    """Catmull-Rom 스플라인으로 꺾인 경유지를 부드러운 곡선으로 보간."""
    pts = [points[0]] + list(points) + [points[-1]]
    out = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for k in range(n):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append([
                0.5 * (2 * p1[j] + (-p0[j] + p2[j]) * t
                       + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2
                       + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3)
                for j in (0, 1)])
    out.append(list(points[-1]))
    return out


def point_at(path, frac):
    frac = min(max(frac, 0.0), 1.0)
    target = path_length_km(path) * frac
    acc = 0.0
    for a, b in zip(path, path[1:]):
        seg = haversine_km(a, b)
        if seg > 0 and acc + seg >= target:
            t = (target - acc) / seg
            return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
        acc += seg
    return list(path[-1])


def split_path(path, frac):
    """(지나온 구간, 남은 구간)"""
    if frac <= 0:
        return [], list(path)
    if frac >= 1:
        return list(path), []
    target = path_length_km(path) * frac
    acc = 0.0
    done = [path[0]]
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        seg = haversine_km(a, b)
        if acc + seg < target:
            done.append(b)
            acc += seg
            continue
        t = (target - acc) / seg if seg else 0
        cut = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
        return done + [cut], [cut] + list(path[i + 1:])
    return list(path), []


def build_land_route(dest):
    """인천항 → 목적지에 가장 가까운 고속도로 경유지들 → 목적지 (스무딩 적용)."""
    if haversine_km(PORT, dest) < 30:
        nodes = [PORT, dest]
    else:
        best = None
        for corridor in CORRIDORS.values():
            for idx, (_, xy) in enumerate(corridor):
                d = haversine_km(xy, dest)
                if best is None or d < best[0]:
                    best = (d, corridor, idx)
        _, corridor, idx = best
        nodes = [xy for _, xy in corridor[:idx + 1]]
        if haversine_km(nodes[-1], dest) < 4:
            nodes[-1] = dest
        else:
            nodes.append(dest)
    return smooth(nodes)


@st.cache_data(ttl=86400, show_spinner=False)
def geocode(address):
    """주소 → ([경도, 위도], 출처). 토큰 없는 Nominatim → 간이표 → 기본값 순."""
    address = (address or "").strip()
    if address:
        try:
            res = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": address, "format": "json", "limit": 1,
                        "countrycodes": "kr", "accept-language": "ko"},
                headers={"User-Agent": "china-korea-parcel-tracker-demo/1.0"},
                timeout=6,
            ).json()
            if res:
                return [float(res[0]["lon"]), float(res[0]["lat"])], "검색 결과"
        except Exception:
            pass
        for name, coord in CITY_FALLBACK.items():
            if name in address:
                return coord, f"'{name}' 대표 좌표"
    return DEFAULT_DEST, "기본 좌표(양산)"


def emoji_icon(emoji):
    """이모지를 SVG data-URI 아이콘으로 (TextLayer/폰트 파일 불필요)."""
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='128' height='128'>"
           f"<text x='64' y='98' font-size='92' text-anchor='middle'>{emoji}</text></svg>")
    return {"url": "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg),
            "width": 128, "height": 128, "anchorY": 64}


# ============================================================================
# 3. 단계 · 데이터 소스
# ============================================================================
def build_stages(cn_km, land_km):
    cn_h = max(4.0, cn_km / 50 + 2)
    land_h = max(5.0, land_km / 55 + 3)
    return [
        ("📦", "중국 집화·이송", "물류센터에서 접수해 칭다오항으로 이동 중이에요.", cn_h),
        ("🛃", "중국 출항 통관", "중국 세관 수출 통관 후 선적을 기다려요.", 12),
        ("🚢", "해상 운송", "칭다오항 → 인천항, 서해를 건너는 중이에요.", 30),
        ("⚓", "인천항 도착", "인천항에 하역하고 세관 구역으로 반입해요.", 6),
        ("🏛️", "한국 통관", "관세청 수입 통관 심사 단계예요.", 12),
        ("🚚", "국내 육상 운송", "인천 → 고속도로 → 배송지역 터미널로 이동해요.", land_h),
        ("🏍️", "배송 출발", "담당 기사님이 배송 중이에요.", 4),
        ("🏠", "배송 완료", "수령이 완료됐어요.", 0),
    ]


def fetch_tracking(waybill):
    """현재 단계(0~7)와 단계 내 진행률(0~1). 실제 API 연동 시 이 함수만 교체."""
    h = hashlib.sha256((waybill or "DEMO").encode()).digest()
    return {"stage": h[0] % 7, "progress": h[1] / 256, "source": "데모 데이터"}


# ============================================================================
# 4. 포맷
# ============================================================================
WEEKDAYS = "월화수목금토일"


def fmt_clock(dt):
    ampm = "오전" if dt.hour < 12 else "오후"
    return f"{ampm} {dt.hour % 12 or 12}시" + (f" {dt.minute}분" if dt.minute else "")


def fmt_eta(eta, now):
    diff = (eta.date() - now.date()).days
    day = {0: "오늘", 1: "내일", 2: "모레"}.get(
        diff, f"{eta.month}월 {eta.day}일({WEEKDAYS[eta.weekday()]})")
    return f"{day} {fmt_clock(eta)}"


def fmt_remaining(hours):
    mins = max(0, round(hours * 60))
    d, rest = divmod(mins, 1440)
    h, m = divmod(rest, 60)
    parts = [f"{d}일" if d else "", f"{h}시간" if h else "", f"{m}분" if m and not d else ""]
    return " ".join(p for p in parts if p) or "곧 도착"


def short(dt):
    return dt.strftime("%m/%d %H:%M")


# ============================================================================
# 5. 스타일 · 입력 · 사이드바
# ============================================================================
st.markdown(
    """
<style>
.eta-card{background:linear-gradient(135deg,#2563eb,#7c3aed);color:#fff;border-radius:18px;
  padding:20px 24px;margin:8px 0 4px;box-shadow:0 6px 18px rgba(80,60,200,.25)}
.eta-card .label{font-size:.9rem;opacity:.85}
.eta-card .big{font-size:1.6rem;font-weight:800;margin:4px 0}
.eta-card .sub{font-size:1rem;opacity:.95}
.tl-time{color:#6b7280;font-size:.85rem}
.tl-now{display:inline-block;background:#ef4444;color:#fff;border-radius:999px;
  padding:1px 10px;font-size:.75rem;margin-left:6px;vertical-align:middle}
</style>
""",
    unsafe_allow_html=True,
)
st.title("📦 중국 → 한국 국제택배 실시간 추적")

col_l, col_r = st.columns(2)
waybill = col_l.text_input("송장번호", placeholder="예: 519717613074").strip()
dest_addr = col_r.text_input("최종 도착 주소", value="경상남도 양산시").strip()

# ---- 경로 구성 ----
dest, dest_src = geocode(dest_addr)
CN_PATH = smooth(CN_NODES)
SEA_PATH = smooth(SEA_NODES)
LAND_PATH = build_land_route(dest)
STAGES = build_stages(path_length_km(CN_PATH), path_length_km(LAND_PATH))
D = [s[3] for s in STAGES]
TOTAL_H = sum(D)
land_km = path_length_km(LAND_PATH)

info = fetch_tracking(waybill)

with st.sidebar:
    st.header("⚙️ 데모 설정")
    st.caption("실제 데이터 연동 전, 단계와 이동 위치를 바꿔볼 수 있어요.")
    stage_names = ["자동(송장번호 기준)"] + [f"{i}. {s[1]}" for i, s in enumerate(STAGES)]
    override = st.selectbox("현재 단계 강제 지정", stage_names)
    manual_pct = st.slider("단계 내 진행률 (%)", 0, 100, 50,
                           help="단계를 강제 지정했을 때 아이콘이 경로 위 어디까지 갔는지 조절해요.")
    play = st.button("▶ 처음부터 이동 재생")
    if st.button("🔄 새로고침"):
        st.rerun()

if not waybill:
    st.info("송장번호를 입력하면 맞춤 추적이 시작돼요. 지금은 샘플 화면을 보여드려요.")

if override != stage_names[0]:
    cur = stage_names.index(override) - 1
    prog = 0.0 if cur >= 7 else manual_pct / 100
else:
    cur, prog = info["stage"], info["progress"]


# ============================================================================
# 6. 상태 계산 · 렌더링
# ============================================================================
def from_hours(h):
    """누적 경과시간 → (단계, 단계 내 진행률)"""
    acc = 0.0
    for i in range(7):
        if h < acc + D[i]:
            return i, (h - acc) / D[i]
        acc += D[i]
    return 7, 0.0


def compute_state(cur, prog):
    delivered = cur >= 7
    now = datetime.now(KST)
    remaining = 0.0 if delivered else D[cur] * (1 - prog) + sum(D[cur + 1:7])
    eta = now + timedelta(hours=remaining)
    eta = eta.replace(minute=(eta.minute // 10) * 10, second=0, microsecond=0)
    elapsed = TOTAL_H if delivered else sum(D[:cur]) + D[cur] * prog

    cur_end = now + timedelta(hours=0 if delivered else D[cur] * (1 - prog))
    ends = []
    for i in range(8):
        if delivered or i < cur:
            back = (0 if delivered else D[cur] * prog) + sum(D[i + 1:(7 if delivered else cur)])
            ends.append(now - timedelta(hours=back))
        elif i == cur:
            ends.append(cur_end)
        else:
            ends.append(cur_end + timedelta(hours=sum(D[cur + 1:i + 1])))
    if delivered:
        ends[7] = now
    else:
        ends[6] = ends[7] = eta

    # 경로별 진행 비율
    cn_frac = prog if cur == 0 else (1.0 if cur >= 1 else 0.0)
    sea_frac = 0.0 if cur < 2 else (prog if cur == 2 else 1.0)
    land_frac = (0.0 if cur < 5 else 0.92 * prog if cur == 5
                 else 0.92 + 0.08 * prog if cur == 6 else 1.0)

    # 이동수단 위치
    if cur == 0:
        pos = point_at(CN_PATH, cn_frac)
    elif cur == 1:
        pos = CN_PORT
    elif cur == 2:
        pos = point_at(SEA_PATH, sea_frac)
    elif cur in (3, 4):
        pos = PORT
    elif cur in (5, 6):
        pos = point_at(LAND_PATH, land_frac)
    else:
        pos = dest

    return dict(cur=cur, prog=prog, delivered=delivered, now=now, eta=eta,
                remaining=remaining, elapsed=elapsed, ends=ends, pos=pos,
                cn_frac=cn_frac, sea_frac=sea_frac, land_frac=land_frac)


def build_deck(s):
    cur, prog = s["cur"], s["prog"]
    veh_icon = STAGE_MARKER[cur]
    veh_title = f"{veh_icon} 현재 위치 · {STAGES[cur][1]}"
    pct = 100 if s["delivered"] else int(prog * 100)
    veh_desc = f"{STAGES[cur][2]}<br/>단계 진행 {pct}% · 전체 {int(min(1, s['elapsed'] / TOTAL_H) * 100)}%"

    routes = [
        (CN_PATH, s["cn_frac"], [22, 163, 74, 235], "중국 육상 구간 🚚", "산둥 물류센터 → 칭다오항"),
        (SEA_PATH, s["sea_frac"], [37, 99, 235, 235], "해상 구간 🚢", "칭다오항 → 서해 → 인천항"),
        (LAND_PATH, s["land_frac"], [249, 115, 22, 235], "국내 육상 구간 🚚",
         f"인천항 → 고속도로 → {dest_addr or '도착지'}"),
    ]
    casing, main = [], []
    for path, frac, color, title, desc in routes:
        casing.append({"path": path, "color": [255, 255, 255, 230], "title": title, "desc": desc})
        done_p, rest_p = split_path(path, frac)
        if len(done_p) >= 2:
            main.append({"path": done_p, "color": color, "title": title, "desc": desc + " · 지나온 길"})
        if len(rest_p) >= 2:
            main.append({"path": rest_p, "color": [148, 163, 184, 170], "title": title, "desc": desc + " · 남은 길"})

    points = [
        {"coord": CN_ORIGIN, "color": [34, 197, 94], "radius": 14000,
         "title": "📦 출발지", "desc": "중국 산둥성 지난 물류센터"},
        {"coord": CN_PORT, "color": [14, 165, 233], "radius": 14000,
         "title": "🛃 칭다오항", "desc": "중국 출항 통관 · 선적"},
        {"coord": PORT, "color": [37, 99, 235], "radius": 16000,
         "title": "⚓ 인천항 세관", "desc": "하역 · 수입 통관이 진행되는 곳"},
        {"coord": dest, "color": [239, 68, 68], "radius": 16000,
         "title": "🏠 최종 도착지", "desc": f"{dest_addr or '도착지'} ({dest_src})"},
    ]
    vehicle = [{"coord": s["pos"], "icon_data": emoji_icon(veh_icon),
                "title": veh_title, "desc": veh_desc, "color": [250, 204, 21, 140],
                "radius": 30000}]

    layers = [
        pdk.Layer("PathLayer", data=casing, get_path="path", get_color="color",
                  get_width=4200, width_min_pixels=7, width_max_pixels=12,
                  cap_rounded=True, joint_rounded=True, pickable=False),
        pdk.Layer("PathLayer", data=main, get_path="path", get_color="color",
                  get_width=2600, width_min_pixels=4, width_max_pixels=8,
                  cap_rounded=True, joint_rounded=True, pickable=True),
        pdk.Layer("ScatterplotLayer", data=points, get_position="coord",
                  get_fill_color="color", get_radius="radius", radius_min_pixels=6,
                  radius_max_pixels=14, stroked=True, get_line_color=[255, 255, 255],
                  line_width_min_pixels=2, pickable=True),
        pdk.Layer("ScatterplotLayer", data=vehicle, get_position="coord",
                  get_fill_color="color", get_radius="radius", radius_min_pixels=20,
                  radius_max_pixels=34, pickable=False),  # 아이콘 뒤 후광 (아이콘 실패 대비)
        pdk.Layer("IconLayer", data=vehicle, get_icon="icon_data", get_position="coord",
                  get_size=40, size_min_pixels=30, size_max_pixels=52, pickable=True),
    ]
    return pdk.Deck(
        layers=layers,
        initial_view_state=VIEW,
        map_provider="carto",   # Mapbox 토큰 불필요
        map_style="light",
        tooltip={"html": "<b>{title}</b><br/>{desc}",
                 "style": {"backgroundColor": "#111827", "color": "white", "fontSize": "13px"}},
    )


def render_eta(ph, s):
    cur = s["cur"]
    if s["delivered"]:
        headline, sub = "🎉 배송이 완료됐어요", "수령 확인 완료"
    else:
        headline = f"🤖 AI 예측 도착: {fmt_eta(s['eta'], s['now'])} 도착 예정"
        sub = f"⏱️ 남은 시간 약 {fmt_remaining(s['remaining'])} · 현재 {STAGE_MARKER[cur]} {STAGES[cur][1]}"
    ratio = min(1.0, s["elapsed"] / TOTAL_H)
    with ph.container():
        st.markdown(
            f"""<div class="eta-card"><div class="label">AI 예상 도착 시간 (ETA)</div>
<div class="big">{headline}</div><div class="sub">{sub}</div></div>""",
            unsafe_allow_html=True)
        st.progress(ratio, text=f"전체 진행률 {int(ratio * 100)}%")
        st.caption(f"※ 국내 구간 {land_km:,.0f}km와 단계별 평균 소요시간으로 계산한 규칙 기반 예측이며, "
                   f"배송 단계는 {info['source']}입니다.")


def render_timeline(ph, s):
    cur, delivered = s["cur"], s["delivered"]
    with ph.container():
        st.subheader("🧭 상세 진행 상황")
        for i, (icon, title, desc, _) in enumerate(STAGES):
            is_done = delivered or i < cur
            is_now = (not delivered) and i == cur
            mark = "✅" if is_done else ("🔵" if is_now else "⚪")
            when = (f"완료 {short(s['ends'][i])}" if is_done
                    else f"진행 중 · 예상 완료 {short(s['ends'][i])}" if is_now
                    else f"예정 {short(s['ends'][i])}")
            with st.container(border=is_now):
                c1, c2 = st.columns([1, 7])
                c1.markdown(f"<div style='font-size:2rem;text-align:center'>{icon}</div>",
                            unsafe_allow_html=True)
                badge = "<span class='tl-now'>현재</span>" if is_now else ""
                c2.markdown(
                    f"**{mark} {title}**{badge}<br>{desc}<br><span class='tl-time'>{when}</span>",
                    unsafe_allow_html=True)


# ---- 지도 시야 (전체 경로가 보이도록) ----
_all = CN_PATH + SEA_PATH + LAND_PATH
_lons, _lats = [p[0] for p in _all], [p[1] for p in _all]
_span = max(max(_lons) - min(_lons), (max(_lats) - min(_lats)) * 1.6, 1)
VIEW = pdk.ViewState(
    longitude=(max(_lons) + min(_lons)) / 2, latitude=(max(_lats) + min(_lats)) / 2,
    zoom=min(7, max(3.3, math.log2(430 / _span))), pitch=0, bearing=0)

# ---- 화면 배치 (placeholder로 두면 재생 시 같은 자리에서 갱신) ----
st.subheader("🗺️ 글로벌 경로")
map_ph = st.empty()
st.caption(f"도착지 좌표: {dest_src} · 선/점/아이콘에 마우스를 올리거나 터치하면 설명이 보여요. "
           "(경로는 시각적 근사)")
eta_ph = st.empty()
tl_ph = st.empty()


def draw(s, with_timeline=True):
    map_ph.pydeck_chart(build_deck(s), height=500)
    render_eta(eta_ph, s)
    if with_timeline:
        render_timeline(tl_ph, s)


if play:
    FRAMES = 60
    for k in range(FRAMES + 1):
        c, p = from_hours(TOTAL_H * k / FRAMES)
        draw(compute_state(c, p), with_timeline=(k % 6 == 0 or k == FRAMES))
        time.sleep(0.25)
else:
    draw(compute_state(cur, prog))

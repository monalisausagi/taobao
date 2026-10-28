"""중국 → 한국 국제택배 추적 웹 앱 (Streamlit + Pydeck)

실행:  streamlit run app.py
필요:  pip install streamlit pydeck requests   (requirements.txt 참고)

※ 현재 '배송 단계'는 송장번호로 만든 데모 데이터이고, ETA는 거리·단계별 평균 소요시간으로
  계산하는 규칙 기반 예측입니다. 실제 택배/관세청 API를 붙이려면 fetch_tracking() 만 교체하세요.
※ 지도 타일은 Carto 기본 스타일이라 Mapbox 토큰이 필요 없습니다.
"""
import hashlib
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pydeck as pdk
import requests
import streamlit as st

KST = ZoneInfo("Asia/Seoul")
st.set_page_config(page_title="중국→한국 택배 추적", page_icon="📦", layout="wide")

# ----------------------------------------------------------------------------
# 1. 고정 경로 데이터  (좌표는 모두 [경도, 위도])
# ----------------------------------------------------------------------------
ORIGIN = [120.38, 36.07]          # 중국 칭다오 물류센터
PORT = [126.60, 37.45]            # 인천항
HUB = [127.40, 36.35]             # 국내 중간 허브(대전권)
SEA_PATH = [                      # 칭다오항 → 서해 → 인천항 (육지를 피해 가는 해상 항로)
    ORIGIN, [121.60, 35.90], [123.50, 36.30],
    [125.00, 36.90], [126.00, 37.20], PORT,
]
DEFAULT_DEST = [129.04, 35.34]    # 양산 (지오코딩 실패 시 기본값)

# 지오코딩 실패/오프라인 대비 간이 좌표표
CITY_FALLBACK = {
    "양산": [129.04, 35.34], "부산": [129.08, 35.18], "울산": [129.31, 35.54],
    "김해": [128.88, 35.23], "창원": [128.68, 35.23], "대구": [128.60, 35.87],
    "대전": [127.38, 36.35], "광주": [126.85, 35.16], "전주": [127.15, 35.82],
    "서울": [126.98, 37.57], "수원": [127.03, 37.26], "인천": [126.71, 37.46],
    "청주": [127.49, 36.64], "포항": [129.37, 36.02], "제주": [126.53, 33.50],
}


# ----------------------------------------------------------------------------
# 2. 지리 계산 유틸
# ----------------------------------------------------------------------------
def haversine_km(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(h))


def path_length_km(path):
    return sum(haversine_km(a, b) for a, b in zip(path, path[1:]))


def point_at(path, frac):
    """경로 위에서 전체 길이의 frac(0~1) 지점 좌표."""
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
    """경로를 진행 지점에서 (지나온 구간, 남은 구간)으로 나눈다."""
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


@st.cache_data(ttl=86400, show_spinner=False)
def geocode(address):
    """주소 → ([경도, 위도], 출처). 토큰 없는 OpenStreetMap Nominatim 사용, 실패 시 간이표/기본값."""
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


# ----------------------------------------------------------------------------
# 3. 배송 단계 · 데이터 소스 (실제 API는 fetch_tracking 만 교체)
# ----------------------------------------------------------------------------
def build_stages(land_km):
    land_h = max(5.0, land_km / 55 + 3)  # 간선 운송 + 분류 시간
    return [
        # (아이콘, 제목, 설명, 소요시간[h])
        ("📦", "중국 집화", "칭다오 물류센터에서 물품을 접수했어요.", 6),
        ("🛃", "중국 출항 통관", "중국 세관 수출 통관 후 항구에 반입돼요.", 12),
        ("🚢", "해상 운송", "칭다오항 → 인천항, 서해를 건너는 중이에요.", 30),
        ("⚓", "인천항 도착", "인천항에 하역하고 세관 구역으로 반입해요.", 6),
        ("🏛️", "한국 통관", "관세청 수입 통관 심사 단계예요.", 12),
        ("🚚", "국내 육상 운송", "인천 → 허브 → 배송지역 터미널로 이동해요.", land_h),
        ("🏍️", "배송 출발", "담당 기사님이 배송 중이에요.", 4),
        ("🏠", "배송 완료", "수령이 완료됐어요.", 0),
    ]


def fetch_tracking(waybill):
    """현재 단계 인덱스(0~7)와 해당 단계 진행률(0~1)을 반환.

    지금은 송장번호 해시로 만든 데모 값입니다. 실제 연동 시 여기서 택배사/유니패스
    조회 결과를 받아 같은 형태로 돌려주면 나머지 화면은 그대로 동작합니다.
    """
    h = hashlib.sha256((waybill or "DEMO").encode()).digest()
    return {"stage": h[0] % 7, "progress": h[1] / 256, "source": "데모 데이터"}


# ----------------------------------------------------------------------------
# 4. 표시용 포맷
# ----------------------------------------------------------------------------
WEEKDAYS = "월화수목금토일"


def fmt_clock(dt):
    ampm = "오전" if dt.hour < 12 else "오후"
    h = dt.hour % 12 or 12
    return f"{ampm} {h}시" + (f" {dt.minute}분" if dt.minute else "")


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


# ----------------------------------------------------------------------------
# 5. 화면
# ----------------------------------------------------------------------------
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

# ---- 입력 섹션 (2단) ----
col_l, col_r = st.columns(2)
waybill = col_l.text_input("송장번호", placeholder="예: 519717613074").strip()
dest_addr = col_r.text_input("최종 도착 주소", value="경상남도 양산시").strip()

with st.sidebar:
    st.header("⚙️ 데모 설정")
    st.caption("실제 배송 데이터 연동 전, 화면 확인용으로 단계를 바꿔볼 수 있어요.")
    stage_names = ["자동(송장번호 기준)"] + [
        f"{i}. {s[1]}" for i, s in enumerate(build_stages(300))]
    override = st.selectbox("현재 단계 강제 지정", stage_names)
    if st.button("🔄 새로고침"):
        st.rerun()

if not waybill:
    st.info("송장번호를 입력하면 맞춤 추적이 시작돼요. 지금은 샘플 화면을 보여드려요.")

# ---- 목적지 · 경로 구성 ----
dest, dest_src = geocode(dest_addr)
use_hub = dest[1] < 37.0 and haversine_km(PORT, dest) > 120
LAND_PATH = [PORT] + ([HUB] if use_hub else []) + [dest]
land_km = path_length_km(LAND_PATH)
STAGES = build_stages(land_km)
durations = [s[3] for s in STAGES]
total_h = sum(durations)

# ---- 현재 상태 ----
info = fetch_tracking(waybill)
cur, prog = info["stage"], info["progress"]
if override != stage_names[0]:
    cur, prog = stage_names.index(override) - 1, 0.5 if stage_names.index(override) - 1 < 7 else 0.0
delivered = cur >= 7
now = datetime.now(KST)

remaining_h = 0.0 if delivered else durations[cur] * (1 - prog) + sum(durations[cur + 1:7])
eta = now + timedelta(hours=remaining_h)
eta = eta.replace(minute=(eta.minute // 10) * 10, second=0, microsecond=0)  # 10분 단위
elapsed_h = total_h if delivered else sum(durations[:cur]) + durations[cur] * prog

# ---- 단계별 시각 계산 ----
cur_end = now + timedelta(hours=0 if delivered else durations[cur] * (1 - prog))
stage_end = []
for i in range(len(STAGES)):
    if delivered or i < cur:
        back = (0 if delivered else durations[cur] * prog) + sum(durations[i + 1:cur if not delivered else 7])
        stage_end.append(now - timedelta(hours=back))
    elif i == cur:
        stage_end.append(cur_end)
    else:
        stage_end.append(cur_end + timedelta(hours=sum(durations[cur + 1:i + 1])))
if not delivered:
    stage_end[6] = stage_end[7] = eta  # ETA와 마지막 단계 시각을 일치시킴
else:
    stage_end[7] = now

# ---- 지도 레이어 ----
sea_frac = 0 if cur < 2 else (prog if cur == 2 else 1)
land_frac = 0 if cur < 5 else (prog if cur == 5 else 1)

if cur in (0, 1):
    veh = ORIGIN
elif cur == 2:
    veh = point_at(SEA_PATH, sea_frac)
elif cur in (3, 4):
    veh = PORT
elif cur == 5:
    veh = point_at(LAND_PATH, land_frac)
else:
    veh = dest
veh_icon, veh_title = STAGES[cur][0], STAGES[cur][1]

path_rows = []
for path, frac, on_color, title, desc in [
    (SEA_PATH, sea_frac, [37, 99, 235, 230], "해상 구간 🚢", "중국 칭다오 → 인천항"),
    (LAND_PATH, land_frac, [249, 115, 22, 230], "국내 육상 구간 🚚", f"인천항 → {dest_addr or '도착지'}"),
]:
    done_p, rest_p = split_path(path, frac)
    if len(done_p) >= 2:
        path_rows.append({"path": done_p, "color": on_color, "title": title, "desc": desc + " (지나온 길)"})
    if len(rest_p) >= 2:
        path_rows.append({"path": rest_p, "color": [148, 163, 184, 150], "title": title, "desc": desc + " (남은 길)"})

point_rows = [
    {"coord": ORIGIN, "color": [34, 197, 94], "radius": 20000,
     "title": "📦 출발지", "desc": "중국 칭다오 물류센터"},
    {"coord": PORT, "color": [37, 99, 235], "radius": 20000,
     "title": "⚓ 인천항 세관", "desc": "하역 · 수입 통관이 진행되는 곳"},
    {"coord": dest, "color": [239, 68, 68], "radius": 20000,
     "title": "🏠 최종 도착지", "desc": f"{dest_addr or '도착지'} ({dest_src})"},
    {"coord": veh, "color": [250, 204, 21], "radius": 32000,
     "title": f"{veh_icon} 현재 위치", "desc": f"{veh_title} · 진행 {int(prog * 100) if not delivered else 100}%"},
]
if use_hub:
    point_rows.insert(2, {"coord": HUB, "color": [168, 85, 247], "radius": 16000,
                          "title": "🏭 국내 허브", "desc": "택배 분류 · 간선 환적 거점"})

layers = [
    pdk.Layer("PathLayer", data=path_rows, get_path="path", get_color="color",
              get_width=2500, width_min_pixels=4, width_max_pixels=8,
              cap_rounded=True, joint_rounded=True, pickable=True),
    pdk.Layer("ScatterplotLayer", data=point_rows, get_position="coord",
              get_fill_color="color", get_radius="radius", radius_min_pixels=7,
              radius_max_pixels=22, stroked=True, get_line_color=[255, 255, 255],
              line_width_min_pixels=2, pickable=True),
]

all_pts = [ORIGIN, PORT, dest, veh]
lon_span = max(p[0] for p in all_pts) - min(p[0] for p in all_pts)
lat_span = (max(p[1] for p in all_pts) - min(p[1] for p in all_pts)) * 1.6
zoom = min(8, max(3.5, math.log2(450 / max(lon_span, lat_span, 1))))
view = pdk.ViewState(
    longitude=(max(p[0] for p in all_pts) + min(p[0] for p in all_pts)) / 2,
    latitude=(max(p[1] for p in all_pts) + min(p[1] for p in all_pts)) / 2,
    zoom=zoom, pitch=0, bearing=0,
)

st.subheader("🗺️ 글로벌 경로")
st.pydeck_chart(
    pdk.Deck(
        layers=layers,
        initial_view_state=view,
        map_provider="carto",   # Mapbox 토큰 불필요
        map_style="light",
        tooltip={
            "html": "<b>{title}</b><br/>{desc}",
            "style": {"backgroundColor": "#111827", "color": "white", "fontSize": "13px"},
        },
    ),
    height=480,
)
st.caption(f"도착지 좌표: {dest_src} · 선/점에 마우스를 올리거나 터치하면 설명이 보여요.")

# ---- AI 도착 예측 카드 ----
if delivered:
    headline, sub = "🎉 배송이 완료됐어요", "수령 확인 완료"
else:
    headline = f"🤖 AI 예측 도착: {fmt_eta(eta, now)} 도착 예정"
    sub = f"⏱️ 남은 시간 약 {fmt_remaining(remaining_h)} · 현재 {veh_icon} {veh_title}"
st.markdown(
    f"""<div class="eta-card"><div class="label">AI 예상 도착 시간 (ETA)</div>
<div class="big">{headline}</div><div class="sub">{sub}</div></div>""",
    unsafe_allow_html=True,
)
st.progress(min(1.0, elapsed_h / total_h), text=f"전체 진행률 {int(min(1.0, elapsed_h / total_h) * 100)}%")
st.caption(f"※ 거리({land_km:,.0f}km 국내 구간)와 단계별 평균 소요시간으로 계산한 규칙 기반 예측이며, "
           f"배송 단계는 {info['source']}입니다.")

# ---- Step by Step 타임라인 ----
st.subheader("🧭 상세 진행 상황")
for i, (icon, title, desc, _) in enumerate(STAGES):
    is_done = delivered or i < cur
    is_now = (not delivered) and i == cur
    mark = "✅" if is_done else ("🔵" if is_now else "⚪")
    if is_done:
        when = f"완료 {short(stage_end[i])}"
    elif is_now:
        when = f"진행 중 · 예상 완료 {short(stage_end[i])}"
    else:
        when = f"예정 {short(stage_end[i])}"
    with st.container(border=is_now):
        c1, c2 = st.columns([1, 7])
        c1.markdown(f"<div style='font-size:2rem;text-align:center'>{icon}</div>", unsafe_allow_html=True)
        badge = "<span class='tl-now'>현재</span>" if is_now else ""
        c2.markdown(
            f"**{mark} {title}**{badge}<br>{desc}<br><span class='tl-time'>{when}</span>",
            unsafe_allow_html=True,
        )

"""중국 → 한국 국제택배 추적 웹 앱 v3 (Streamlit + Pydeck)

실행:  streamlit run app.py
필요:  pip install -r requirements.txt   (streamlit, pydeck, requests, searoute)

경로 데이터는 손으로 찍은 좌표가 아니라 실제 라우팅 결과를 씁니다.
  · 육상(중국/한국): OSRM 공개 서버(OpenStreetMap 도로망) → 실제 도로를 따라가는 좌표
  · 해상: searoute 라이브러리(해상 항로망) → 육지를 피해 가는 항로 좌표
  · 위 조회가 실패하면 직선으로 대신 그리고, 지도 아래에 그렇게 표시했다고 알려줍니다.

※ 배송 단계는 송장번호 기반 '데모 데이터'입니다. 실제 연동은 fetch_tracking() 만 교체하세요.
※ 지도 타일은 Carto 기본 스타일이라 Mapbox 토큰이 필요 없고, 한글은 툴팁으로만 표시합니다.
"""
import hashlib
import math
from bisect import bisect_right

import pydeck as pdk
import requests
import streamlit as st

st.set_page_config(page_title="중국→한국 택배 추적", page_icon="📦", layout="wide")

# ============================================================================
# 1. 고정 데이터  (좌표: [경도, 위도])
# ============================================================================
# 출발지 선택지: 이름 -> (물류센터 좌표, 출항 항구 이름, 항구 좌표)
ORIGINS = {
    "산둥 지난 (칭다오항 출항)": ([117.00, 36.67], "칭다오항", [120.32, 36.08]),
    "베이징 (톈진항 출항)": ([116.40, 39.90], "톈진항", [117.75, 38.99]),
    "랴오닝 선양 (다롄항 출항)": ([123.43, 41.80], "다롄항", [121.65, 38.93]),
    "상하이 (양산심수항 출항)": ([121.47, 31.23], "상하이 양산심수항", [122.06, 30.63]),
}
KR_PORT = [126.60, 37.46]   # 인천항
DEFAULT_DEST = [129.04, 35.34]  # 양산
UA = {"User-Agent": "china-korea-parcel-tracker-demo/1.0"}

CITY_FALLBACK = {
    "양산": [129.04, 35.34], "부산": [129.08, 35.18], "울산": [129.31, 35.54],
    "김해": [128.88, 35.23], "창원": [128.68, 35.23], "대구": [128.60, 35.87],
    "대전": [127.38, 36.35], "광주": [126.85, 35.16], "전주": [127.15, 35.82],
    "서울": [126.98, 37.57], "수원": [127.03, 37.26], "인천": [126.71, 37.46],
    "청주": [127.49, 36.64], "포항": [129.37, 36.02], "강릉": [128.90, 37.75],
}

STAGE_MARKER = {0: "🚚", 1: "🛃", 2: "🚢", 3: "⚓", 4: "🏛️", 5: "🚚", 6: "🚚", 7: "🏠"}


def build_stages(origin_name, cn_port_name):
    city = origin_name.split(" (")[0]
    return [
        ("📦", "중국 집화·이송", f"{city} 물류센터에서 접수해 {cn_port_name}으로 이동해요."),
        ("🛃", "중국 출항 통관", "중국 세관 수출 통관 후 선적을 기다려요."),
        ("🚢", "해상 운송", f"{cn_port_name} → 인천항, 서해 항로를 지나는 중이에요."),
        ("⚓", "인천항 도착", "인천항에 하역하고 세관 구역으로 반입해요."),
        ("🏛️", "한국 통관", "관세청 수입 통관 심사 단계예요."),
        ("🚚", "국내 육상 운송", "인천항에서 배송지역 터미널로 도로 이동 중이에요."),
        ("🏍️", "배송 출발", "담당 기사님이 배송 중이에요."),
        ("🏠", "배송 완료", "수령이 완료됐어요."),
    ]


def fetch_tracking(waybill):
    """현재 단계(0~7)와 단계 내 위치(0~1). 실제 API 연동 시 이 함수만 교체하세요."""
    h = hashlib.sha256((waybill or "DEMO").encode()).digest()
    return {"stage": h[0] % 7, "progress": h[1] / 256, "source": "데모 데이터"}


# ============================================================================
# 2. 지리 유틸 (누적거리 기반이라 점이 수천 개여도 빠름)
# ============================================================================
def haversine_km(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def cumulative(path):
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + haversine_km(a, b))
    return cum


def _locate(path, cum, frac):
    frac = min(max(frac, 0.0), 1.0)
    target = cum[-1] * frac
    i = min(max(bisect_right(cum, target) - 1, 0), len(path) - 2)
    seg = cum[i + 1] - cum[i]
    t = (target - cum[i]) / seg if seg else 0.0
    a, b = path[i], path[i + 1]
    return i, [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]


def point_at(path, cum, frac):
    return _locate(path, cum, frac)[1]


def split_path(path, cum, frac):
    """(지나온 구간, 남은 구간)"""
    if frac <= 0:
        return [], list(path)
    if frac >= 1:
        return list(path), []
    i, cut = _locate(path, cum, frac)
    return path[:i + 1] + [cut], [cut] + path[i + 1:]


# ============================================================================
# 3. 라우팅 (실패하면 예외 → 캐시되지 않음 → 바깥에서 직선 대체)
# ============================================================================
@st.cache_data(ttl=7 * 86400, show_spinner=False)
def _osrm_route(a, b):
    url = (f"https://router.project-osrm.org/route/v1/driving/"
           f"{a[0]:.5f},{a[1]:.5f};{b[0]:.5f},{b[1]:.5f}")
    res = requests.get(url, params={"overview": "full", "geometries": "geojson"},
                       headers=UA, timeout=20)
    res.raise_for_status()
    data = res.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM: {data.get('code')}")
    coords = data["routes"][0]["geometry"]["coordinates"]
    return [list(a)] + [[c[0], c[1]] for c in coords] + [list(b)]


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def _sea_route(a, b):
    import searoute as sr  # 해상 항로망 기반 라우팅 (ImportError면 바깥에서 대체)
    try:
        feature = sr.searoute(list(a), list(b), append_orig_dest=True)
    except TypeError:  # 옛 버전
        feature = sr.searoute(list(a), list(b))
    coords = feature["geometry"]["coordinates"]
    path = [[c[0], c[1]] for c in coords]
    if haversine_km(path[0], a) > 1:
        path.insert(0, list(a))
    if haversine_km(path[-1], b) > 1:
        path.append(list(b))
    return path


def land_route(a, b):
    try:
        return _osrm_route(tuple(a), tuple(b)), "도로 라우팅(OSRM)", True
    except Exception:
        return [list(a), list(b)], "직선(도로 조회 실패)", False


def sea_route(a, b):
    try:
        return _sea_route(tuple(a), tuple(b)), "해상 항로망(searoute)", True
    except Exception:
        return [list(a), list(b)], "직선(항로 라이브러리 사용 불가)", False


@st.cache_data(ttl=86400, show_spinner=False)
def _nominatim(address):
    res = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1,
                "countrycodes": "kr", "accept-language": "ko"},
        headers=UA, timeout=8).json()
    if not res:
        raise LookupError("no result")
    return [float(res[0]["lon"]), float(res[0]["lat"])]


def geocode(address):
    address = (address or "").strip()
    if address:
        try:
            return _nominatim(address), "검색 결과"
        except Exception:
            for name, coord in CITY_FALLBACK.items():
                if name in address:
                    return coord, f"'{name}' 대표 좌표"
    return DEFAULT_DEST, "기본 좌표(양산)"


def emoji_icon(emoji):
    """이모지를 SVG data-URI 아이콘으로 (TextLayer/폰트 파일 불필요)."""
    import urllib.parse
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='128' height='128'>"
           f"<text x='64' y='98' font-size='92' text-anchor='middle'>{emoji}</text></svg>")
    return {"url": "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg),
            "width": 128, "height": 128, "anchorY": 64}


# ============================================================================
# 4. 입력 · 사이드바
# ============================================================================
st.markdown(
    """
<style>
.tl-note{color:#6b7280;font-size:.85rem}
.tl-now{display:inline-block;background:#ef4444;color:#fff;border-radius:999px;
  padding:1px 10px;font-size:.75rem;margin-left:6px;vertical-align:middle}
</style>
""",
    unsafe_allow_html=True,
)
st.title("📦 중국 → 한국 국제택배 추적")

c1, c2, c3 = st.columns([1, 1, 1])
waybill = c1.text_input("송장번호", placeholder="예: 519717613074").strip()
origin_key = c2.selectbox("출발지 (중국)", list(ORIGINS))
dest_addr = c3.text_input("최종 도착 주소", value="경상남도 양산시").strip()

origin_xy, cn_port_name, cn_port_xy = ORIGINS[origin_key]
STAGES = build_stages(origin_key, cn_port_name)
info = fetch_tracking(waybill)

with st.sidebar:
    st.header("⚙️ 데모 설정")
    st.caption("실제 데이터 연동 전, 단계와 아이콘 위치를 바꿔볼 수 있어요.")
    stage_names = ["자동(송장번호 기준)"] + [f"{i}. {s[1]}" for i, s in enumerate(STAGES)]
    override = st.selectbox("현재 단계 강제 지정", stage_names)
    manual_pct = st.slider("단계 내 위치 (%)", 0, 100, 50,
                           help="단계를 강제 지정했을 때 아이콘이 해당 구간 어디쯤인지 조절해요.")

if not waybill:
    st.info("송장번호를 입력하면 맞춤 추적이 시작돼요. 지금은 샘플 화면을 보여드려요.")

if override != stage_names[0]:
    cur = stage_names.index(override) - 1
    prog = 0.0 if cur >= 7 else manual_pct / 100
else:
    cur, prog = info["stage"], info["progress"]

# ============================================================================
# 5. 경로 계산
# ============================================================================
dest, dest_src = geocode(dest_addr)
with st.spinner("실제 도로·항로 경로를 계산하는 중..."):
    CN_PATH, cn_src, cn_ok = land_route(origin_xy, cn_port_xy)
    SEA_PATH, sea_src, sea_ok = sea_route(cn_port_xy, KR_PORT)
    KR_PATH, kr_src, kr_ok = land_route(KR_PORT, dest)
CN_CUM, SEA_CUM, KR_CUM = cumulative(CN_PATH), cumulative(SEA_PATH), cumulative(KR_PATH)

# 단계 → 각 경로의 진행 비율 / 이동수단 위치
cn_frac = prog if cur == 0 else (1.0 if cur >= 1 else 0.0)
sea_frac = 0.0 if cur < 2 else (prog if cur == 2 else 1.0)
kr_frac = (0.0 if cur < 5 else 0.9 * prog if cur == 5
           else 0.9 + 0.1 * prog if cur == 6 else 1.0)

if cur == 0:
    pos = point_at(CN_PATH, CN_CUM, cn_frac)
elif cur == 1:
    pos = cn_port_xy
elif cur == 2:
    pos = point_at(SEA_PATH, SEA_CUM, sea_frac)
elif cur in (3, 4):
    pos = KR_PORT
elif cur in (5, 6):
    pos = point_at(KR_PATH, KR_CUM, kr_frac)
else:
    pos = dest

# ============================================================================
# 6. 지도
# ============================================================================
veh_icon = STAGE_MARKER[cur]
pct = 100 if cur >= 7 else int(prog * 100)
vehicle = [{
    "coord": pos, "icon_data": emoji_icon(veh_icon), "color": [250, 204, 21, 140],
    "radius": 30000, "title": f"{veh_icon} 현재 위치 · {STAGES[cur][1]}",
    "desc": f"{STAGES[cur][2]}<br/>단계 {cur + 1}/8 · 구간 진행 {pct}%",
}]

routes = [
    (CN_PATH, CN_CUM, cn_frac, [22, 163, 74, 235], "중국 육상 구간 🚚",
     f"{origin_key.split(' (')[0]} → {cn_port_name} · {cn_src}"),
    (SEA_PATH, SEA_CUM, sea_frac, [37, 99, 235, 235], "해상 구간 🚢",
     f"{cn_port_name} → 인천항 · {sea_src}"),
    (KR_PATH, KR_CUM, kr_frac, [249, 115, 22, 235], "국내 육상 구간 🚚",
     f"인천항 → {dest_addr or '도착지'} · {kr_src}"),
]
casing, main = [], []
for path, cum, frac, color, title, desc in routes:
    casing.append({"path": path, "color": [255, 255, 255, 230], "title": title, "desc": desc})
    done_p, rest_p = split_path(path, cum, frac)
    if len(done_p) >= 2:
        main.append({"path": done_p, "color": color, "title": title, "desc": desc + " · 지나온 길"})
    if len(rest_p) >= 2:
        main.append({"path": rest_p, "color": [148, 163, 184, 170], "title": title, "desc": desc + " · 남은 길"})

points = [
    {"coord": origin_xy, "color": [34, 197, 94], "radius": 14000,
     "title": "📦 출발지", "desc": origin_key.split(" (")[0] + " 물류센터"},
    {"coord": cn_port_xy, "color": [14, 165, 233], "radius": 14000,
     "title": f"🛃 {cn_port_name}", "desc": "중국 출항 통관 · 선적"},
    {"coord": KR_PORT, "color": [37, 99, 235], "radius": 16000,
     "title": "⚓ 인천항 세관", "desc": "하역 · 수입 통관이 진행되는 곳"},
    {"coord": dest, "color": [239, 68, 68], "radius": 16000,
     "title": "🏠 최종 도착지", "desc": f"{dest_addr or '도착지'} ({dest_src})"},
]

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
              radius_max_pixels=34, pickable=False),  # 아이콘 뒤 후광
    pdk.Layer("IconLayer", data=vehicle, get_icon="icon_data", get_position="coord",
              get_size=40, size_min_pixels=30, size_max_pixels=52, pickable=True),
]

_all = [p for path in (CN_PATH, SEA_PATH, KR_PATH) for p in path[::max(1, len(path) // 400)]]
_lons, _lats = [p[0] for p in _all], [p[1] for p in _all]
_span = max(max(_lons) - min(_lons), (max(_lats) - min(_lats)) * 1.6, 1)
view = pdk.ViewState(
    longitude=(max(_lons) + min(_lons)) / 2, latitude=(max(_lats) + min(_lats)) / 2,
    zoom=min(7, max(3.3, math.log2(430 / _span))), pitch=0, bearing=0)

st.pydeck_chart(
    pdk.Deck(
        layers=layers, initial_view_state=view,
        map_provider="carto", map_style="light",   # Mapbox 토큰 불필요
        tooltip={"html": "<b>{title}</b><br/>{desc}",
                 "style": {"backgroundColor": "#111827", "color": "white", "fontSize": "13px"}},
    ),
    height=500,
)
st.caption("선·점·아이콘에 마우스를 올리거나 터치하면 설명이 보여요. "
           f"도착지 좌표: {dest_src} · 경로: 중국 {cn_src} / 해상 {sea_src} / 국내 {kr_src}")
if not (cn_ok and sea_ok and kr_ok):
    st.warning("일부 구간은 라우팅 조회에 실패해 직선으로 표시했어요. 잠시 후 새로고침하면 "
               "다시 시도해요. (해상 구간은 requirements.txt의 searoute 설치가 필요해요.)")

# ============================================================================
# 7. 상세 타임라인
# ============================================================================
st.subheader(f"🧭 현재 단계 {min(cur + 1, 8)}/8 · {veh_icon} {STAGES[cur][1]}")
st.caption(f"송장번호 {waybill or '(샘플)'} · 도착지 {dest_addr or '-'} · 배송 단계는 {info['source']}")
for i, (icon, title, desc) in enumerate(STAGES):
    is_done = cur >= 7 or i < cur
    is_now = cur < 7 and i == cur
    mark = "✅" if is_done else ("🔵" if is_now else "⚪")
    status = "완료" if is_done else ("진행 중" if is_now else "대기")
    with st.container(border=is_now):
        a, b = st.columns([1, 7])
        a.markdown(f"<div style='font-size:2rem;text-align:center'>{icon}</div>",
                   unsafe_allow_html=True)
        badge = "<span class='tl-now'>현재</span>" if is_now else ""
        b.markdown(f"**{mark} {title}**{badge}<br>{desc}<br><span class='tl-note'>{status}</span>",
                   unsafe_allow_html=True)

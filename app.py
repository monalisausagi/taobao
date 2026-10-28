"""중국 → 한국 국제택배 추적 웹 앱 v5 (Streamlit + Pydeck)

실행:  streamlit run app.py
필요:  pip install -r requirements.txt   (streamlit, pydeck, requests, searoute)

[통관 전 1~4단계]  중국 창고 → 칭다오 등 항구(도로) → 서해(해상 항로) → 인천항 → 인천세관(정차)
[통관 후 5~7단계]  국내 송장번호 + 도착 주소가 있을 때 추적:
                   인천세관 → 택배사 메인 허브(정차) → 도착지 권역 허브 → 배송캠프(정차) → 도착지
- 경로: 육상은 OSRM(OpenStreetMap 도로망), 해상은 searoute 항로망. 실패 구간은 직선으로 대신
        그리고 화면에 알립니다.
※ 허브/캠프 좌표는 택배사가 공개한 실제 시설 좌표가 아닌 '대표 위치'입니다. 필요하면
   COURIERS / REGION_CENTERS / ENTRY 좌표를 실제 위치로 고쳐 쓰세요.
※ 배송 단계는 송장번호 기반 '데모 데이터'입니다. 실제 연동은 fetch_tracking() /
   fetch_domestic_tracking() 두 함수만 교체하세요.
"""
import hashlib
import math
import re
import urllib.parse
from bisect import bisect_right

import pydeck as pdk
import requests
import streamlit as st

st.set_page_config(page_title="중국→한국 택배 추적", page_icon="📦", layout="wide")

# ============================================================================
# 1. 고정 데이터  (좌표: [경도, 위도])
# ============================================================================
# 출발지: 이름 -> (창고 좌표, 항구명, 항구 좌표)
ORIGINS = {
    "산둥 지난 (칭다오항 출항)": ([117.00, 36.67], "칭다오항", [120.32, 36.08]),
    "베이징 (톈진항 출항)": ([116.40, 39.90], "톈진항", [117.75, 38.99]),
    "랴오닝 선양 (다롄항 출항)": ([123.43, 41.80], "다롄항", [121.65, 38.93]),
    "상하이 (양산심수항 출항)": ([121.47, 31.23], "상하이 양산심수항", [122.06, 30.63]),
}

# 인천 입국 지점 (하역 지점, 세관 통관 지점)
ENTRY = {"arrive_name": "인천항 부두", "arrive": [126.60, 37.46],
         "customs_name": "인천세관 통관장", "customs": [126.61, 37.47]}

# 국내 택배사: 메인 허브(대표 위치), 용어, 마지막 배송 수단
COURIERS = {
    "CJ대한통운": dict(main=("곤지암 메가허브", [127.30, 37.35]),
                    hub_term="권역 터미널", camp_term="배송캠프", last_icon="🚚"),
    "우체국택배": dict(main=("중부 우편집중국(옥천 인근)", [127.57, 36.30]),
                    hub_term="지역 우편집중국", camp_term="배달 우체국", last_icon="🏍️"),
    "롯데글로벌로지스": dict(main=("롯데글로벌로지스 중부 허브(대전 인근)", [127.38, 36.35]),
                   hub_term="권역 허브", camp_term="배송 서브터미널", last_icon="🚚"),
    "한진택배": dict(main=("한진택배 대전 허브(대표 위치)", [127.40, 36.40]),
                   hub_term="권역 터미널", camp_term="배송 영업소", last_icon="🚚"),
}

# 도착지 권역 → 권역 허브 대표 위치 (도착 주소에서 가장 가까운 권역 선택)
REGION_CENTERS = {
    "수도권": [127.02, 37.45], "대전·충청": [127.38, 36.35], "대구·경북": [128.60, 35.87],
    "부산·경남": [129.08, 35.18], "광주·전라": [126.85, 35.16], "강원 영동": [128.90, 37.75],
}

UA = {"User-Agent": "china-korea-parcel-tracker-demo/1.0"}
CITY_FALLBACK = {
    "양산": [129.04, 35.34], "부산": [129.08, 35.18], "울산": [129.31, 35.54],
    "김해": [128.88, 35.23], "창원": [128.68, 35.23], "대구": [128.60, 35.87],
    "대전": [127.38, 36.35], "광주": [126.85, 35.16], "전주": [127.15, 35.82],
    "서울": [126.98, 37.57], "수원": [127.03, 37.26], "인천": [126.71, 37.46],
    "청주": [127.49, 36.64], "포항": [129.37, 36.02], "강릉": [128.90, 37.75],
}


def fetch_tracking(waybill):
    """국제 송장: 현재 단계(0~4, 5=통관 완료·국내 인계 대기)와 단계 내 위치(0~1).
    지금은 송장번호 해시로 만든 데모 값입니다. 실제 연동 시 이 함수만 교체하세요."""
    h = hashlib.sha256((waybill or "DEMO").encode()).digest()
    return {"stage": h[0] % 6, "progress": h[1] / 256, "source": "데모 데이터"}


def fetch_domestic_tracking(courier, invoice):
    """국내 송장: 현재 단계(5=간선 운송, 6=배송 출발, 7=배송 완료)와 단계 내 위치(0~1).
    지금은 데모 값입니다. 실제 택배사 조회 연동 시 이 함수만 교체하세요."""
    h = hashlib.sha256(f"{courier}:{invoice}".encode()).digest()
    return {"stage": 5 + h[0] % 3, "progress": h[1] / 256, "source": "데모 데이터"}


# ============================================================================
# 2. 지리 · 한국어 유틸
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
    if frac <= 0:
        return [], list(path)
    if frac >= 1:
        return list(path), []
    i, cut = _locate(path, cum, frac)
    return path[:i + 1] + [cut], [cut] + path[i + 1:]


def ro(word):
    """'~로/~으로' 조사 자동 선택 (받침 없음·ㄹ받침 → 로, 그 외 → 으로)."""
    ch = word.strip()[-1:]
    if ch and "가" <= ch <= "힣":
        return word + ("로" if (ord(ch) - 0xAC00) % 28 in (0, 8) else "으로")
    return word + "로"


def emoji_icon(emoji):
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='128' height='128'>"
           f"<text x='64' y='98' font-size='92' text-anchor='middle'>{emoji}</text></svg>")
    return {"url": "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg),
            "width": 128, "height": 128, "anchorY": 64}


# ============================================================================
# 3. 라우팅 (실패하면 예외 → 캐시 안 됨 → 바깥에서 직선 대체)
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
    import searoute as sr
    try:
        feature = sr.searoute(list(a), list(b), append_orig_dest=True)
    except TypeError:
        feature = sr.searoute(list(a), list(b))
    path = [[c[0], c[1]] for c in feature["geometry"]["coordinates"]]
    if haversine_km(path[0], a) > 1:
        path.insert(0, list(a))
    if haversine_km(path[-1], b) > 1:
        path.append(list(b))
    return path


def land_route(a, b):
    if haversine_km(a, b) < 1:
        return [list(a), list(b)], "동일 지점", True
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
    """주소 → ([경도, 위도], 출처). 빈 주소/못 찾은 주소는 (None, 안내문)."""
    address = (address or "").strip()
    if not address:
        return None, "주소 미입력"
    try:
        return _nominatim(address), "검색 결과"
    except Exception:
        for name, coord in CITY_FALLBACK.items():
            if name in address:
                return coord, f"'{name}' 대표 좌표"
    return None, "주소를 찾지 못함"


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

r1a, r1b = st.columns(2)
intl_wb = r1a.text_input(
    "국제 송장번호", value="",
    placeholder="예: 519717613074 (타오바오/해외물류 송장번호)").strip()
dom_wb = r1b.text_input(
    "국내 송장번호 (선택)", value="",
    placeholder="예: 1234-5678-9012 (통관 완료 후 발급 시 입력)",
    help="통관이 끝나 국내 택배사 송장번호가 발급되면 입력하세요. 비워두면 통관 단계까지만 추적해요.").strip()
r2a, r2b = st.columns(2)
courier_key = r2a.selectbox("국내 택배사", list(COURIERS))
origin_key = r2b.selectbox("출발지 (중국)", list(ORIGINS))
dest_addr = st.text_input(
    "최종 도착 주소", value="",
    placeholder="예: 서울특별시 강남구 테헤란로 123 (도로명 주소 입력)").strip()

dom_digits = re.sub(r"[\s-]", "", dom_wb)  # 하이픈·공백 허용
if dom_wb and (not dom_digits.isdigit() or not 10 <= len(dom_digits) <= 13):
    st.warning("국내 송장번호는 보통 숫자 10~13자리예요(하이픈은 있어도 돼요). 입력값을 한 번 확인해 보세요.")

CO = COURIERS[courier_key]
origin_xy, port_name, port_xy = ORIGINS[origin_key]
city = origin_key.split(" (")[0]
main_name, main_xy = CO["main"]

# ---- 도착지 · 권역 허브 · 배송캠프 결정 (주소가 있을 때만) ----
dest, dest_src = geocode(dest_addr)
if dest_addr and dest is None:
    st.warning("입력한 주소의 위치를 찾지 못했어요. 시/구까지 포함한 도로명 주소로 다시 입력해 보세요.")
elif dest is None:
    st.info("도착 주소를 입력하면 통관 이후 국내 허브·캠프 경로까지 지도에 표시돼요.")

dest_label = dest_addr or "도착지"
if dest:
    region_name = min(REGION_CENTERS, key=lambda k: haversine_km(REGION_CENTERS[k], dest))
    region_xy = REGION_CENTERS[region_name]
    if haversine_km(region_xy, main_xy) < 15:  # 메인 허브 권역이면 허브 하나로 충분
        region_xy = main_xy
    dist_rd = haversine_km(region_xy, dest)
    if dist_rd < 8:
        camp_xy = list(region_xy)
    else:  # 도착지 약 3km 앞(허브 쪽)에 배송캠프 대표 위치
        k = 3 / dist_rd
        camp_xy = [dest[0] + (region_xy[0] - dest[0]) * k, dest[1] + (region_xy[1] - dest[1]) * k]
    region_label = f"{region_name} {CO['hub_term']}" if region_xy != main_xy else main_name
    stage5_desc = f"{courier_key} {main_name}에서 분류 후 {ro(region_label)} 이동해요."
    stage6_desc = f"{CO['camp_term']}에서 상차 후 {ro(dest_label)} 배송해요."
else:
    region_xy = camp_xy = None
    region_label = ""
    stage5_desc = f"{courier_key} 메인 허브({main_name})를 거쳐 도착지 권역으로 이동해요."
    stage6_desc = f"{CO['camp_term']}에서 상차 후 도착지로 배송해요."

STAGES = [
    ("📦", "중국 집화", f"{city} 창고에서 물품을 접수해요."),
    ("🛃", "중국 이송·수출통관", f"{ro(port_name)} 이동해 수출 통관을 받아요."),
    ("🚢", "해상 운송", f"{port_name} → 인천항, 서해 항로를 지나는 중이에요."),
    ("⚓", "인천항 도착", f"{ENTRY['arrive_name']}에 도착해 하역해요."),
    ("🏛️", "한국 통관", f"{ENTRY['customs_name']}에서 수입 통관 심사 중이에요."),
    ("🚚", "국내 간선 운송", stage5_desc),
    (CO["last_icon"], "배송 출발", stage6_desc),
    ("🏠", "배송 완료", "수령이 완료됐어요."),
]

with st.sidebar:
    st.header("⚙️ 데모 설정")
    st.caption("실제 데이터 연동 전, 단계와 아이콘 위치를 바꿔볼 수 있어요.")
    stage_names = ["자동(송장번호 기준)"] + [f"{i}. {s[1]}" for i, s in enumerate(STAGES)]
    override = st.selectbox("현재 단계 강제 지정", stage_names)
    manual_pct = st.slider("단계 내 위치 (%)", 0, 100, 50,
                           help="단계 안에서 이동 중인지, 거점에 정차 중인지 조절해요.")

# ---- 현재 단계 결정 ----
intl_info = fetch_tracking(intl_wb)
dom_info = fetch_domestic_tracking(courier_key, dom_wb) if dom_wb else None
forced = override != stage_names[0]
hold = None  # 통관 후 대기 사유: 'invoice'(국내 송장 대기) / 'address'(도착 주소 대기)

if forced:
    cur = stage_names.index(override) - 1
    prog = 0.0 if cur >= 7 else manual_pct / 100
    src_note = "사이드바에서 강제 지정한 단계"
elif dom_info:  # 국내 송장이 있으면 통관은 끝난 것으로 보고 5~7단계를 추적
    cur, prog = dom_info["stage"], dom_info["progress"]
    src_note = f"국내 송장 · {dom_info['source']}"
else:
    cur, prog = intl_info["stage"], intl_info["progress"]
    src_note = f"국제 송장 · {intl_info['source']}"
    if cur >= 5:  # 통관 완료, 국내 송장 대기
        cur, prog, hold = 4, 1.0, "invoice"

if cur >= 5 and dest is None:  # 국내 구간은 도착 주소가 있어야 그릴 수 있음
    cur, prog, hold = 4, 1.0, "address"

if not intl_wb and not dom_wb and not forced:
    st.info("송장번호를 입력하면 맞춤 추적이 시작돼요. 지금은 샘플 화면을 보여드려요.")

# ============================================================================
# 5. 경로 계산 (구간별로 나눠서 거점마다 정차할 수 있게)
# ============================================================================
with st.spinner("실제 도로·항로 경로를 계산하는 중..."):
    cn_path, cn_src, cn_ok = land_route(origin_xy, port_xy)
    sea_path, sea_src, sea_ok = sea_route(port_xy, ENTRY["arrive"])
    oks = [cn_ok, sea_ok]
    if dest:
        k1, k1_src, k1_ok = land_route(ENTRY["customs"], main_xy)
        k2, k2_src, k2_ok = land_route(main_xy, region_xy)
        k3, k3_src, k3_ok = land_route(region_xy, camp_xy)
        k4, k4_src, k4_ok = land_route(camp_xy, dest)
        oks += [k1_ok, k2_ok, k3_ok, k4_ok]

GRAY = [148, 163, 184, 170]
LEGS = {  # id: (경로, 색, 제목, 설명)
    "cn": (cn_path, [22, 163, 74, 235], "중국 육상 구간 🚚", f"{city} 창고 → {port_name} · {cn_src}"),
    "sea": (sea_path, [37, 99, 235, 235], "해상 구간 🚢", f"{port_name} → 인천항 · {sea_src}"),
}
if dest:
    LEGS.update({
        "k1": (k1, [249, 115, 22, 235], "국내 간선 🚚", f"{ENTRY['customs_name']} → {main_name} · {k1_src}"),
        "k2": (k2, [249, 115, 22, 235], "국내 간선 🚚", f"{main_name} → {region_label} · {k2_src}"),
        "k3": (k3, [234, 88, 12, 235], "국내 배송 🚚", f"{region_label} → {CO['camp_term']} · {k3_src}"),
        "k4": (k4, [220, 38, 38, 235], f"마지막 배송 {CO['last_icon']}", f"{CO['camp_term']} → {dest_label} · {k4_src}"),
    })
CUMS = {k: cumulative(v[0]) for k, v in LEGS.items()}


def run_phases(p, leg_ids):
    """leg_ids 구간을 순서대로 이동하되, 구간 사이(거점)에서는 정차한다.
    반환: (위치, {구간: 진행률}, 정차 중인 구간 번호 또는 None, 이동 중인 구간 번호)"""
    lens = [CUMS[i][-1] for i in leg_ids]
    stop_w = 0.12 * sum(lens) if sum(lens) > 0 else 1.0
    segs = []
    for n, _ in enumerate(leg_ids):
        segs.append(("move", n, lens[n]))
        if n < len(leg_ids) - 1:
            segs.append(("stop", n, stop_w))
    total = sum(w for _, _, w in segs)
    target, acc = min(max(p, 0.0), 1.0) * total, 0.0
    for idx, (kind, n, w) in enumerate(segs):
        if target <= acc + w or idx == len(segs) - 1:
            local = min(1.0, max(0.0, (target - acc) / w)) if w else 1.0
            fr = {lid: (1.0 if m < n else 0.0) for m, lid in enumerate(leg_ids)}
            lid = leg_ids[n]
            if kind == "move":
                fr[lid] = local
                return point_at(LEGS[lid][0], CUMS[lid], local), fr, None, n
            fr[lid] = 1.0
            return LEGS[lid][0][-1], fr, n, None
        acc += w
    return LEGS[leg_ids[-1]][0][-1], {lid: 1.0 for lid in leg_ids}, None, None


frac = dict.fromkeys(LEGS, 0.0)
vehicle_icon = STAGES[cur][0]
if cur == 0:
    pos, status = origin_xy, f"{city} 창고에서 집화 중 (정차)"
elif cur == 1:
    frac["cn"] = prog
    pos, status, vehicle_icon = point_at(cn_path, CUMS["cn"], prog), f"{ro(port_name)} 이동 중", "🚚"
elif cur == 2:
    frac.update(cn=1.0, sea=prog)
    pos, status = point_at(sea_path, CUMS["sea"], prog), "서해 위를 이동 중"
elif cur == 3:
    frac.update(cn=1.0, sea=1.0)
    pos, status = ENTRY["arrive"], f"{ENTRY['arrive_name']} 정차 (하역 중)"
elif cur == 4:
    frac.update(cn=1.0, sea=1.0)
    pos = ENTRY["customs"]
    status = {"invoice": "통관 완료 · 국내 송장번호 발급 대기 (세관 정차)",
              "address": "통관 완료 · 도착 주소 입력 대기 (세관 정차)"}.get(
                  hold, f"{ENTRY['customs_name']} 정차 (통관 중)")
elif cur == 5:
    frac.update(cn=1.0, sea=1.0)
    pos, fr, stop, mv = run_phases(prog, ["k1", "k2"])
    frac.update(fr)
    status = f"{main_name} 정차 (분류 중)" if stop is not None else (
        f"{ro(main_name)} 이동 중" if mv == 0 else f"{ro(region_label)} 이동 중")
    vehicle_icon = "🚚"
elif cur == 6:
    frac.update(cn=1.0, sea=1.0, k1=1.0, k2=1.0)
    pos, fr, stop, mv = run_phases(prog, ["k3", "k4"])
    frac.update(fr)
    if stop is not None:
        status, vehicle_icon = f"{CO['camp_term']} 정차 (상차 중)", "🚚"
    elif mv == 0:
        status, vehicle_icon = f"{ro(CO['camp_term'])} 이동 중", "🚚"
    else:
        status, vehicle_icon = "도착지로 배송 중", CO["last_icon"]
else:
    frac = dict.fromkeys(LEGS, 1.0)
    pos, status = dest, "배송 완료"

# 국내 구간(5~7단계)이 실제 추적 중인지: 국내 송장 입력 또는 강제 지정
domestic_tracked = bool(dest) and (bool(dom_info) or (forced and cur >= 5))

# ============================================================================
# 6. 지도
# ============================================================================
vehicle = [{
    "coord": pos, "icon_data": emoji_icon(vehicle_icon), "color": [250, 204, 21, 140],
    "radius": 30000, "title": f"{vehicle_icon} 현재 위치 · {STAGES[cur][1]}",
    "desc": f"{status}<br/>단계 {cur + 1}/8",
}]

casing, main = [], []
for lid, (path, color, title, desc) in LEGS.items():
    casing.append({"path": path, "color": [255, 255, 255, 230], "title": title, "desc": desc})
    done_p, rest_p = split_path(path, CUMS[lid], frac[lid])
    if len(done_p) >= 2:
        main.append({"path": done_p, "color": color, "title": title, "desc": desc + " · 지나온 길"})
    if len(rest_p) >= 2:
        plan = " · 예상 경로(국내 송장 입력 시 추적)" if (lid.startswith("k") and not domestic_tracked) else " · 남은 길"
        main.append({"path": rest_p, "color": GRAY, "title": title, "desc": desc + plan})

note = "대표 위치"
points = [
    {"coord": origin_xy, "color": [34, 197, 94], "radius": 14000, "title": "📦 출발 창고", "desc": f"{city} · {note}"},
    {"coord": port_xy, "color": [14, 165, 233], "radius": 14000, "title": f"🛃 {port_name}", "desc": "중국 출항 통관 · 선적"},
    {"coord": ENTRY["arrive"], "color": [37, 99, 235], "radius": 14000,
     "title": f"⚓ {ENTRY['arrive_name']}", "desc": "하역 지점"},
    {"coord": ENTRY["customs"], "color": [99, 102, 241], "radius": 14000,
     "title": f"🏛️ {ENTRY['customs_name']}", "desc": "수입 통관이 진행되는 곳"},
]
if dest:
    points += [
        {"coord": main_xy, "color": [168, 85, 247], "radius": 15000,
         "title": f"🏭 {main_name}", "desc": f"{courier_key} 메인 허브 · {note}"},
        {"coord": camp_xy, "color": [245, 158, 11], "radius": 12000,
         "title": f"🏪 {CO['camp_term']}", "desc": f"도착지 인근 배송 거점 · {note}"},
        {"coord": dest, "color": [239, 68, 68], "radius": 16000,
         "title": "🏠 최종 도착지", "desc": f"{dest_label} ({dest_src})"},
    ]
    if region_xy != main_xy:
        points.append({"coord": region_xy, "color": [217, 70, 239], "radius": 15000,
                       "title": f"🏭 {region_label}", "desc": f"도착지 권역 거점 · {note}"})

layers = [
    pdk.Layer("PathLayer", data=casing, get_path="path", get_color="color",
              get_width=4200, width_min_pixels=7, width_max_pixels=12,
              cap_rounded=True, joint_rounded=True, pickable=False),
    pdk.Layer("PathLayer", data=main, get_path="path", get_color="color",
              get_width=2600, width_min_pixels=4, width_max_pixels=8,
              cap_rounded=True, joint_rounded=True, pickable=True),
    pdk.Layer("ScatterplotLayer", data=points, get_position="coord",
              get_fill_color="color", get_radius="radius", radius_min_pixels=5,
              radius_max_pixels=12, stroked=True, get_line_color=[255, 255, 255],
              line_width_min_pixels=2, pickable=True),
    pdk.Layer("ScatterplotLayer", data=vehicle, get_position="coord",
              get_fill_color="color", get_radius="radius", radius_min_pixels=20,
              radius_max_pixels=34, pickable=False),
    pdk.Layer("IconLayer", data=vehicle, get_icon="icon_data", get_position="coord",
              get_size=40, size_min_pixels=30, size_max_pixels=52, pickable=True),
]

_all = [p for v in LEGS.values() for p in v[0][::max(1, len(v[0]) // 300)]]
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
           f"도착지 좌표: {dest_src} · 허브/캠프 위치는 실제 시설 좌표가 아닌 대표 위치예요.")
if not all(oks):
    st.warning("일부 구간은 라우팅 조회에 실패해 직선으로 표시했어요. 잠시 후 새로고침하면 다시 시도해요. "
               "(해상 구간은 requirements.txt의 searoute 설치가 필요해요.)")

# ============================================================================
# 7. 상세 타임라인
# ============================================================================
st.subheader(f"🧭 현재 단계 {min(cur + 1, 8)}/8 · {vehicle_icon} {STAGES[cur][1]}")
st.markdown(f"**📍 {status}**")
st.caption(f"국제 송장 {intl_wb or '(미입력)'} · 국내 송장 {dom_wb or '(미입력)'} · {courier_key} · "
           f"도착지 {dest_addr or '(미입력)'} · 기준: {src_note}")
if forced and cur >= 5 and not dom_wb:
    st.caption("※ 국내 송장번호 없이 강제 지정한 데모 상태예요.")
for i, (icon, title, desc) in enumerate(STAGES):
    is_done = cur >= 7 or i < cur
    is_now = cur < 7 and i == cur
    mark = "✅" if is_done else ("🔵" if is_now else "⚪")
    if is_done:
        state_txt = "완료"
    elif is_now:
        state_txt = f"진행 중 · {status}"
    elif i >= 5 and not domestic_tracked:
        state_txt = "대기 · 국내 송장번호와 도착 주소를 입력하면 추적해요"
    else:
        state_txt = "대기"
    with st.container(border=is_now):
        a, b = st.columns([1, 7])
        a.markdown(f"<div style='font-size:2rem;text-align:center'>{icon}</div>",
                   unsafe_allow_html=True)
        badge = "<span class='tl-now'>현재</span>" if is_now else ""
        b.markdown(f"**{mark} {title}**{badge}<br>{desc}<br><span class='tl-note'>{state_txt}</span>",
                   unsafe_allow_html=True)

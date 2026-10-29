"""국제소포추적 (Streamlit + Pydeck) v7

실행:  streamlit run app.py
필요:  pip install -r requirements.txt   (streamlit, pydeck, requests, searoute)

[모드 A] 국제 송장번호 입력 → 해외 창고→항구/공항→해상·항공→인천→세관→택배사 허브→배송캠프→도착지
[모드 B] 국내 송장번호만 입력 → 택배사 메인 허브→권역 허브→배송캠프→도착지

v7 변경점
- 송장번호 칸은 숫자만 남기고 나머지는 자동 삭제
- 주소: 대한민국 영역으로 엄격히 검색하고, 입력한 시·도/시·군·구와 검색 결과가 일치하는지 검증.
        일치하지 않으면 지도에 찍지 않고 경고 (예: 김해 주소가 양산 좌표로 튀는 문제 차단)
- 거점 좌표: 대표 좌표를 OpenStreetMap 이름 검색으로 보정하고, 보정되면 그 좌표를 사용
- 실제 배송 현황: 택배사 조회 화면을 붙여넣으면 그 이력으로 단계·위치를 맞춤 (실시간 API 아님)

※ 키 없이 쓰는 OpenStreetMap(Nominatim)에는 한국 도로명 주소·물류센터 데이터가 일부만 있어
   건물 단위 정밀도가 보장되지 않습니다. 화면에 '정밀도'를 항상 표시해요.
※ 국제(해외) 구간의 단계는 데모 값입니다. 실제 연동은 fetch_tracking() 만 교체하세요.
"""
import hashlib
import math
import re
import time
import urllib.parse
from bisect import bisect_right

import pydeck as pdk
import requests
import streamlit as st

st.set_page_config(page_title="국제소포추적", page_icon="📦", layout="wide")

# ============================================================================
# 1. 고정 데이터  (좌표: [경도, 위도])
# ============================================================================
ORIGINS = {  # 이름 -> 창고, 항구, 공항, 기본 운송방식
    "중국 상하이 (양산심수항 출항)": dict(wh=[121.47, 31.23], port=("상하이 양산심수항", [122.06, 30.63]),
                                 air=("상하이 푸둥공항", [121.81, 31.14]), mode="sea"),
    "중국 산둥 지난 (칭다오항 출항)": dict(wh=[117.00, 36.67], port=("칭다오항", [120.32, 36.08]),
                                  air=("지난 야오창공항", [117.22, 36.86]), mode="sea"),
    "중국 베이징 (톈진항 출항)": dict(wh=[116.40, 39.90], port=("톈진항", [117.75, 38.99]),
                              air=("베이징 서우두공항", [116.60, 40.08]), mode="sea"),
    "중국 선양 (다롄항 출항)": dict(wh=[123.43, 41.80], port=("다롄항", [121.65, 38.93]),
                            air=("선양 타오셴공항", [123.49, 41.64]), mode="sea"),
    "일본 도쿄 (요코하마항 출항)": dict(wh=[139.69, 35.69], port=("요코하마항", [139.65, 35.45]),
                               air=("나리타공항", [140.39, 35.77]), mode="sea"),
    "미국 로스앤젤레스 (LA항 출항)": dict(wh=[-118.24, 34.05], port=("로스앤젤레스항", [-118.27, 33.73]),
                                 air=("LA 국제공항", [-118.41, 33.94]), mode="air"),
}

# 인천 입국 거점: 대표 좌표 + OSM 이름검색어(보정용) + 보정 허용 반경(km)
ENTRY = {
    "sea": {"arrive_name": "인천항 부두", "arrive": [126.60, 37.46],
            "customs_name": "인천세관 통관장", "customs": [126.61, 37.47],
            "customs_q": ["인천본부세관", "인천세관"], "radius": 7},
    "air": {"arrive_name": "인천공항 화물터미널", "arrive": [126.45, 37.46],
            "customs_name": "인천공항세관", "customs": [126.44, 37.47],
            "customs_q": ["인천공항세관"], "radius": 9},
}

# 국내 택배사: 메인 허브(대표 좌표 + OSM 검색어 + 배송 거점 용어 + 이력 키워드)
COURIERS = {
    "CJ대한통운": dict(main=("곤지암 메가허브", [127.30, 37.35]),
                    main_q=["CJ대한통운 곤지암 통합물류센터", "곤지암 통합물류센터"], hub_kw="곤지암",
                    hub_term="권역 터미널", camp_term="배송캠프", last_icon="🚚",
                    camp_q=["CJ대한통운 {sgg}", "CJ대한통운 {core}"]),
    "우체국택배": dict(main=("중부 우편집중국(옥천 인근)", [127.57, 36.30]),
                    main_q=["옥천 우편집중국", "옥천우체국"], hub_kw="옥천|집중국",
                    hub_term="지역 우편집중국", camp_term="배달 우체국", last_icon="🏍️",
                    camp_q=["{core}우체국", "{sgg} 우체국"]),
    "롯데글로벌로지스": dict(main=("롯데글로벌로지스 중부 허브(대전 인근)", [127.38, 36.35]),
                      main_q=["롯데글로벌로지스 대전 메가허브", "롯데택배 대전 허브"], hub_kw="대전",
                      hub_term="권역 허브", camp_term="배송 서브터미널", last_icon="🚚",
                      camp_q=["롯데택배 {sgg}", "롯데택배 {core}"]),
    "한진택배": dict(main=("한진택배 대전 허브(대표 위치)", [127.40, 36.40]),
                   main_q=["한진택배 대전 메가허브", "한진택배 대전 허브"], hub_kw="대전",
                   hub_term="권역 터미널", camp_term="배송 영업소", last_icon="🚚",
                   camp_q=["한진택배 {sgg}", "한진택배 {core}"]),
}

# 도착지 권역 → 권역 허브 대표 위치 (가장 가까운 권역 선택)
REGION_CENTERS = {
    "수도권": [127.02, 37.45], "대전·충청": [127.38, 36.35], "대구·경북": [128.60, 35.87],
    "부산·경남": [129.08, 35.18], "광주·전라": [126.85, 35.16], "강원 영동": [128.90, 37.75],
}

KR_BOX = (124.5, 33.0, 131.9, 38.7)  # 대한민국 영역 (경도min, 위도min, 경도max, 위도max)
NOMI = "https://nominatim.openstreetmap.org/search"
UA = {"User-Agent": "intl-parcel-tracker-demo/1.0"}

# 시·도 표기 보정
ALIASES = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도", "강원도": "강원특별자치도", "전라북도": "전북특별자치도",
    "제주도": "제주특별자치도",
}
CORE_ALIAS = {"경남": "경상남", "경북": "경상북", "전남": "전라남", "전북": "전라북",
              "충남": "충청남", "충북": "충청북"}
SIDO_SUFFIX = r"(특별자치도|특별자치시|특별시|광역시|도)$"


def fetch_tracking(waybill):
    """[모드 A] 국제 송장: 단계(0~4, 5=통관 완료·국내 인계 대기)와 단계 내 위치(0~1).
    지금은 데모 값입니다. 실제 연동 시 이 함수만 교체하세요."""
    h = hashlib.sha256((waybill or "DEMO").encode()).digest()
    return {"stage": h[0] % 6, "progress": h[1] / 256, "source": "데모 데이터"}


def fetch_domestic_tracking(courier, invoice):
    """국내 송장 데모 값(5=간선, 6=배송 출발, 7=완료). 택배사 이력을 붙여넣으면 그쪽이 우선돼요."""
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


def unwrap_to_end(path):
    """날짜변경선을 건너는 경로의 경도를 끝점(한국) 기준으로 연속화 (미국 쪽 경도 > 180)."""
    out = [list(path[-1])]
    prev = path[-1][0]
    for lon, lat in reversed(path[:-1]):
        while lon - prev > 180:
            lon -= 360
        while lon - prev < -180:
            lon += 360
        out.append([lon, lat])
        prev = lon
    return out[::-1]


def great_circle(a, b, n=80):
    la1, lo1, la2, lo2 = map(math.radians, (a[1], a[0], b[1], b[0]))
    d = 2 * math.asin(math.sqrt(math.sin((la2 - la1) / 2) ** 2
                                + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    if d == 0:
        return [list(a), list(b)]
    out = []
    for k in range(n + 1):
        f = k / n
        A, B = math.sin((1 - f) * d) / math.sin(d), math.sin(f * d) / math.sin(d)
        x = A * math.cos(la1) * math.cos(lo1) + B * math.cos(la2) * math.cos(lo2)
        y = A * math.cos(la1) * math.sin(lo1) + B * math.cos(la2) * math.sin(lo2)
        z = A * math.sin(la1) + B * math.sin(la2)
        out.append([math.degrees(math.atan2(y, x)),
                    math.degrees(math.atan2(z, math.sqrt(x * x + y * y)))])
    out[0], out[-1] = list(a), list(b)
    return unwrap_to_end(out)


def ro(word):
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
# 3. 도로·항로 라우팅 (실패하면 예외 → 캐시 안 됨 → 바깥에서 직선 대체)
# ============================================================================
def _norm(p):
    return [p[0] - 360 if p[0] > 180 else p[0], p[1]]


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
    return [[c[0], c[1]] for c in data["routes"][0]["geometry"]["coordinates"]]


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def _sea_route(a, b):
    import searoute as sr
    try:
        feature = sr.searoute(list(a), list(b), append_orig_dest=True)
    except TypeError:
        feature = sr.searoute(list(a), list(b))
    geom = feature["geometry"]
    coords = geom["coordinates"]
    if geom.get("type") == "MultiLineString":
        coords = [c for line in coords for c in line]
    return [[c[0], c[1]] for c in coords]


def land_route(a, b):
    if haversine_km(a, b) < 1:
        return [list(a), list(b)], "동일 지점", True
    off = 360 if a[0] > 180 else 0
    try:
        pts = _osrm_route(tuple(_norm(a)), tuple(_norm(b)))
        return [list(a)] + [[p[0] + off, p[1]] for p in pts] + [list(b)], "도로 라우팅(OSRM)", True
    except Exception:
        return [list(a), list(b)], "직선(도로 조회 실패)", False


def sea_route(a, b):
    try:
        path = unwrap_to_end(_sea_route(tuple(_norm(a)), tuple(_norm(b))))
        if haversine_km(path[0], _norm(a)) > 1:
            path.insert(0, list(a))
        if haversine_km(path[-1], b) > 1:
            path.append(list(b))
        shift = round((a[0] - path[0][0]) / 360) * 360
        return [[p[0] + shift, p[1]] for p in path], "해상 항로망(searoute)", True
    except Exception:
        return [list(a), list(b)], "직선(항로 라이브러리 사용 불가)", False


# ============================================================================
# 4. 주소 검증 지오코딩 (대한민국 한정 + 입력 지역과 결과 일치 검증)
# ============================================================================
_last_call = [0.0]


def _throttle():  # Nominatim 이용 정책: 초당 1회 이하
    wait = 1.05 - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


@st.cache_data(ttl=30 * 86400, show_spinner=False)
def _nomi_query(q, box):
    """box=(경도min, 위도min, 경도max, 위도max)로 검색 범위를 강제(bounded). 실패 시 예외."""
    _throttle()
    params = {"q": q, "format": "jsonv2", "limit": 5, "addressdetails": 1,
              "countrycodes": "kr", "accept-language": "ko", "bounded": 1,
              "viewbox": f"{box[0]},{box[3]},{box[2]},{box[1]}"}
    res = requests.get(NOMI, params=params, headers=UA, timeout=10)
    res.raise_for_status()
    out = []
    for it in res.json():
        out.append({"lat": float(it["lat"]), "lon": float(it["lon"]),
                    "display": it.get("display_name", ""), "atype": it.get("addresstype", ""),
                    "cc": (it.get("address") or {}).get("country_code", "")})
    return out


def canon_core(name):
    c = re.sub(SIDO_SUFFIX, "", name.strip())
    return CORE_ALIAS.get(c, c)


def normalize_address(text):
    s = re.sub(r"\([^)]*\)", " ", text)
    s = re.sub(r"[,\s]+", " ", s).strip()
    toks = s.split(" ") if s else []
    if toks:
        toks[0] = ALIASES.get(toks[0], toks[0])
    return " ".join(toks)


def admin_tokens(norm):
    """입력 주소에서 시·도 / 시·군·구 토큰 추출."""
    sido, sgg = [], []
    cores = {canon_core(v) for v in ALIASES.values()}
    for t in norm.split():
        if re.search(SIDO_SUFFIX, t) and canon_core(t) in cores:
            sido.append(t)
        elif re.fullmatch(r"[가-힣]{1,6}(시|군|구)", t):
            sgg.append(t)
    return sido, sgg


def verify_result(display, sido, sgg):
    parts = [p.strip() for p in display.split(",")]
    cores = {canon_core(p) for p in parts}
    return all(canon_core(t) in cores for t in sido) and all(t in parts for t in sgg)


def level_label(atype):
    a = (atype or "").lower()
    if a in {"building", "house", "apartments", "residential", "commercial", "office", "retail",
             "shop", "amenity", "tourism", "industrial", "yes", "public", "school", "hospital",
             "college", "university", "man_made", "leisure", "place_of_worship"}:
        return "건물·상세 주소", False
    if a in {"road", "highway", "street", "path", "trunk", "primary", "secondary", "tertiary",
             "service", "unclassified", "pedestrian"}:
        return "도로 단위", False
    if a in {"neighbourhood", "quarter", "suburb", "village", "hamlet", "isolated_dwelling",
             "locality", "city_block", "borough"}:
        return "동·리 단위(대략)", True
    if a in {"city_district", "county", "municipality", "city", "town", "district"}:
        return "시·군·구 단위(대략)", True
    if a in {"state", "province", "region"}:
        return "시·도 단위(대략)", True
    return "근사 위치", True


def geocode_kr(address):
    """주소 → dict(status, ...). status: ok / empty / network / not_found / mismatch"""
    raw = (address or "").strip()
    if len(raw) < 2:
        return {"status": "empty" if not raw else "not_found"}
    norm = normalize_address(raw)
    sido, sgg = admin_tokens(norm)
    toks = norm.split()
    if sido or sgg:
        last_admin = max(i for i, t in enumerate(toks) if t in sido + sgg)
        attempts = [" ".join(toks[:n]) for n in range(len(toks), last_admin, -1)][:5]
    else:  # 지역 정보가 없으면 한 번만 검색하고 '확인 필요'로 표시
        attempts = [norm]
    saw_any = False
    try:
        for q in attempts:
            for c in _nomi_query(q, KR_BOX):
                if c["cc"] and c["cc"] != "kr":
                    continue
                if not (KR_BOX[0] <= c["lon"] <= KR_BOX[2] and KR_BOX[1] <= c["lat"] <= KR_BOX[3]):
                    continue
                saw_any = True
                if not (sido or sgg) or verify_result(c["display"], sido, sgg):
                    label, coarse = level_label(c["atype"])
                    parts = [p.strip() for p in c["display"].split(",")]
                    sgg_name = next((p for p in parts if re.fullmatch(r"[가-힣]{1,6}(시|군|구)", p)
                                     and not re.search(SIDO_SUFFIX, p)), (sgg[-1] if sgg else ""))
                    return {"status": "ok", "coord": [c["lon"], c["lat"]], "display": c["display"],
                            "level": label, "coarse": coarse, "dropped": q != norm,
                            "ambiguous": not (sido or sgg), "sgg": sgg_name}
    except Exception:
        return {"status": "network"}
    return {"status": "mismatch" if saw_any else "not_found", "wanted": sido + sgg}


def refine_poi(queries, approx, radius_km):
    """대표 좌표 근처(반경 내)에서 OSM 이름검색으로 실제 시설을 찾으면 그 좌표를 쓴다.
    반환: (좌표, '장소 검색 보정' 또는 '대표 위치', 찾은 이름)"""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.2, math.cos(math.radians(approx[1]))))
    box = (round(approx[0] - dlon, 4), round(approx[1] - dlat, 4),
           round(approx[0] + dlon, 4), round(approx[1] + dlat, 4))
    try:
        for q in queries[:2]:
            hits = [c for c in _nomi_query(q, box)
                    if haversine_km([c["lon"], c["lat"]], approx) <= radius_km]
            if hits:
                best = min(hits, key=lambda c: haversine_km([c["lon"], c["lat"]], approx))
                return [best["lon"], best["lat"]], "장소 검색 보정", best["display"].split(",")[0]
    except Exception:
        pass
    return list(approx), "대표 위치", ""


# ============================================================================
# 5. 택배사 조회 화면 붙여넣기 파서 (실제 배송 현황 반영)
# ============================================================================
TS = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:[ T]*\d{1,2}:\d{2})?")
LEVEL_RULES = [  # 높은 단계부터 검사 (한 줄에 여러 단어가 있으면 높은 단계가 이김)
    (6, r"배송\s*완료|배달\s*완료|인수\s*완료"),
    (5, r"배송\s*출발|배달\s*출발|배달\s*준비|배송\s*중"),
    (4, r"배송지\s*도착|지점\s*도착|영업소\s*도착|캠프\s*도착|서브\s*터미널\s*도착|우체국\s*도착|배달점\s*도착"),
    (3, r"간선\s*(상차|하차)|이동\s*중|허브|터미널|집중국|발송|통과|상차|하차"),
    (2, r"집화|집하|상품\s*인수|접수|수거"),
]


def parse_history(text):
    events = []
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 4:
            continue
        lvl = next((L for L, pat in LEVEL_RULES if re.search(pat, line)), 0)
        if lvl:
            m = TS.search(line)
            events.append({"time": m.group(0).strip() if m else "", "text": line, "level": lvl})
    if not events:
        return None
    return {"level": max(e["level"] for e in events), "events": events}


# ============================================================================
# 6. 입력 UI
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
st.title("국제소포추적")


def _digits_only(key):
    """송장번호 칸: 숫자 이외 문자는 입력 즉시 지운다."""
    raw = st.session_state.get(key, "")
    clean = re.sub(r"\D", "", raw)
    if clean != raw:
        st.session_state["_digit_notice"] = True
    st.session_state[key] = clean


if st.session_state.pop("_digit_notice", False):
    st.warning("송장번호에는 숫자만 입력할 수 있어서 숫자가 아닌 문자는 자동으로 지웠어요.")

r1a, r1b = st.columns(2)
intl_wb = r1a.text_input(
    "국제 송장번호", value="", key="intl_wb", max_chars=30,
    on_change=_digits_only, args=("intl_wb",),
    placeholder="예: 519717613074 (해외 발송 건 조회 시 입력)",
    help="숫자만 입력할 수 있어요.").strip()
dom_wb = r1b.text_input(
    "국내 송장번호", value="", key="dom_wb", max_chars=20,
    on_change=_digits_only, args=("dom_wb",),
    placeholder="예: 1234-5678-9012 (국내 택배사 번호만 있을 때 입력)",
    help="숫자만 입력할 수 있어요(하이픈은 자동 제거). 국제 송장번호 없이 이것만 입력하면 국내 구간만 추적해요.").strip()
r2a, r2b = st.columns(2)
courier_key = r2a.selectbox("국내 택배사", list(COURIERS))
origin_key = r2b.selectbox("해외 출발지", list(ORIGINS))
dest_addr = st.text_input(
    "최종 도착 주소", value="",
    placeholder="예: 서울특별시 강남구 테헤란로 123").strip()
with st.expander("📋 택배사 조회 결과 붙여넣기 (선택) — 실제 배송 현황으로 위치 맞추기"):
    paste = st.text_area(
        "택배사 사이트의 배송 조회 화면 내용을 복사해 붙여넣으세요", value="", height=140,
        placeholder="예)\n2026-10-28 09:12 곤지암Hub 간선상차\n2026-10-28 21:40 부산Hub 간선하차\n"
                    "2026-10-29 07:05 양산캠프 배송출발")

dom_ok = bool(dom_wb) and 10 <= len(dom_wb) <= 13
if dom_wb and not dom_ok:
    st.warning("국내 송장번호는 보통 숫자 10~13자리예요. 입력값을 한 번 확인해 보세요.")

hist = parse_history(paste)
if paste.strip() and not hist:
    st.warning("붙여넣은 내용에서 배송 단계(집화·간선·배송출발·배송완료 등)를 찾지 못했어요. "
               "택배사 조회 화면의 이력 줄을 그대로 복사해 주세요.")

dom_given = bool(dom_wb) or bool(hist)
mode_b = dom_given and not intl_wb  # B: 국내 단독 / A: 국제 송장 있음(없으면 샘플)
CO = COURIERS[courier_key]
main_name, main_xy0 = CO["main"]
O = ORIGINS[origin_key]
SHIFT = 360 if O["wh"][0] < 0 else 0  # 서반구(미국) 좌표는 +360으로 표기해 한국과 이어 붙임


def sh(p):
    return [p[0] + SHIFT, p[1]]


with st.sidebar:
    st.header("⚙️ 데모 설정")
    st.caption("실제 데이터 연동 전, 단계와 아이콘 위치를 바꿔볼 수 있어요.")
    transport_pref = st.selectbox("운송 방식", ["자동", "해상", "항공"],
                                  help="자동: 출발지 기본값(미국발은 항공, 그 외 해상)")
air = (transport_pref == "항공") or (transport_pref == "자동" and O["mode"] == "air")
gate_name, gate_xy = (O["air"][0], sh(O["air"][1])) if air else (O["port"][0], sh(O["port"][1]))
origin_xy = sh(O["wh"])
city = origin_key.split(" (")[0]
entry = dict(ENTRY["air" if air else "sea"])

# ---- 도착 주소 검증 ----
with st.spinner("주소와 물류 거점 위치를 확인하는 중..."):
    geo = geocode_kr(dest_addr)
    dest = geo["coord"] if geo["status"] == "ok" else None
    dest_src = "검증된 주소 검색 결과" if dest else "주소 미확정"

    # 거점 좌표 보정 (대표 좌표 → 장소 검색 결과가 반경 안에 있으면 교체)
    main_xy, main_src, _ = refine_poi(CO["main_q"], main_xy0, 15)
    if not mode_b:
        entry["customs"], customs_src, _ = refine_poi(entry["customs_q"], entry["customs"], entry["radius"])
    else:
        customs_src = "대표 위치"
    camp_found = None
    if dest:
        sgg = geo["sgg"]
        core = re.sub(r"(시|군|구)$", "", sgg) if sgg else ""
        if sgg:
            qs = [q.format(sgg=sgg, core=core) for q in CO["camp_q"]]
            camp_found = refine_poi(qs, dest, 12)

if geo["status"] == "network":
    st.warning("주소 검색 서비스(OpenStreetMap)에 연결하지 못했어요. 잠시 후 다시 시도해 주세요. "
               "(주소 없이도 해외 구간은 계속 볼 수 있어요.)")
elif geo["status"] == "not_found":
    st.warning("입력한 주소를 대한민국에서 찾지 못했어요. 시·도, 시·군·구까지 포함해 다시 입력해 주세요. "
               "예: 서울특별시 강남구 테헤란로 123")
elif geo["status"] == "mismatch":
    st.warning(f"입력한 지역({', '.join(geo.get('wanted', []))})과 일치하는 주소를 찾지 못했어요. "
               "엉뚱한 지역 좌표가 찍히는 것을 막기 위해 지도에 표시하지 않았어요. 철자와 시·군·구를 확인해 주세요.")
elif geo["status"] == "empty":
    st.info("도착 주소를 입력하면 국내 허브·캠프 경로가 지도에 표시돼요.")
else:
    st.success(f"📍 확인된 주소: {geo['display']}  ·  정밀도: {geo['level']}")
    if geo["ambiguous"]:
        st.info("시·도/시·군·구가 없어서 가장 먼저 검색된 위치를 썼어요. 위 '확인된 주소'가 맞는지 확인해 주세요.")
    elif geo["dropped"] or geo["coarse"]:
        st.info(f"번지·상세 주소까지는 찾지 못해 '{geo['level']}' 기준 위치로 표시했어요.")

dest_label = dest_addr or "도착지"
if dest:
    region_name = min(REGION_CENTERS, key=lambda k: haversine_km(REGION_CENTERS[k], dest))
    region_xy = REGION_CENTERS[region_name]
    if haversine_km(region_xy, main_xy) < 15:
        region_xy = main_xy
    dist_rd = haversine_km(region_xy, dest)
    if camp_found and camp_found[1] == "장소 검색 보정":
        camp_xy, camp_src = camp_found[0], f"장소 검색 보정 ({camp_found[2]})"
    elif dist_rd < 8:
        camp_xy, camp_src = list(region_xy), "대표 위치(추정)"
    else:  # 도착지 약 3km 앞(허브 쪽)에 배송캠프 대표 위치
        kk = 3 / dist_rd
        camp_xy = [dest[0] + (region_xy[0] - dest[0]) * kk, dest[1] + (region_xy[1] - dest[1]) * kk]
        camp_src = "대표 위치(추정)"
    region_label = f"{region_name} {CO['hub_term']}" if region_xy != main_xy else main_name
    stage5_desc = f"{courier_key} {main_name}에서 분류 후 {ro(region_label)} 이동해요."
    stage6_desc = f"{CO['camp_term']}에서 상차 후 {ro(dest_label)} 배송해요."
else:
    region_xy = camp_xy = None
    camp_src, region_label = "", ""
    stage5_desc = f"{courier_key} 메인 허브({main_name})를 거쳐 도착지 권역으로 이동해요."
    stage6_desc = f"{CO['camp_term']}에서 상차 후 도착지로 배송해요."

STAGES = [
    ("📦", "해외 집화", f"{city} 창고에서 물품을 접수해요."),
    ("🛃", "해외 이송·수출통관", f"{ro(gate_name)} 이동해 수출 통관을 받아요."),
    ("✈️" if air else "🚢", "항공 운송" if air else "해상 운송",
     f"{gate_name} → 인천공항, 항공편으로 이동 중이에요." if air
     else f"{gate_name} → 인천항, 해상 항로를 지나는 중이에요."),
    ("🛬" if air else "⚓", "인천공항 도착" if air else "인천항 도착",
     f"{entry['arrive_name']}에 도착해 하역해요."),
    ("🏛️", "한국 통관", f"{entry['customs_name']}에서 수입 통관 심사 중이에요."),
    ("🚚", "국내 간선 운송", stage5_desc),
    (CO["last_icon"], "배송 출발", stage6_desc),
    ("🏠", "배송 완료", "수령이 완료됐어요."),
]

with st.sidebar:
    stage_names = ["자동(송장번호/조회내용 기준)"] + [f"{i}. {s[1]}" for i, s in enumerate(STAGES)]
    override = st.selectbox("현재 단계 강제 지정", stage_names)
    manual_pct = st.slider("단계 내 위치 (%)", 0, 100, 50,
                           help="단계 안에서 이동 중인지, 거점에 정차 중인지 조절해요.")

# ============================================================================
# 7. 경로 계산 (구간별로 나눠서 거점마다 정차)
# ============================================================================
GRAY = [148, 163, 184, 170]
LEGS, oks = {}, []


def add_leg(lid, res, color, title, desc_fn):
    path, src, ok = res
    oks.append(ok)
    LEGS[lid] = (path, color, title, desc_fn(src))


with st.spinner("실제 도로·항로 경로를 계산하는 중..."):
    if not mode_b:
        add_leg("o", land_route(origin_xy, gate_xy), [22, 163, 74, 235], "해외 육상 구간 🚚",
                lambda s: f"{city} 창고 → {gate_name} · {s}")
        if air:
            add_leg("tr", (great_circle(gate_xy, entry["arrive"]), "대권 비행경로", True),
                    [147, 51, 234, 235], "항공 구간 ✈️",
                    lambda s: f"{gate_name} → {entry['arrive_name']} · {s}")
        else:
            add_leg("tr", sea_route(gate_xy, entry["arrive"]), [37, 99, 235, 235], "해상 구간 🚢",
                    lambda s: f"{gate_name} → {entry['arrive_name']} · {s}")
    if dest:
        if not mode_b:
            add_leg("k1", land_route(entry["customs"], main_xy), [249, 115, 22, 235], "국내 간선 🚚",
                    lambda s: f"{entry['customs_name']} → {main_name} · {s}")
        add_leg("k2", land_route(main_xy, region_xy), [249, 115, 22, 235], "국내 간선 🚚",
                lambda s: f"{main_name} → {region_label} · {s}")
        add_leg("k3", land_route(region_xy, camp_xy), [234, 88, 12, 235], "국내 배송 🚚",
                lambda s: f"{region_label} → {CO['camp_term']} · {s}")
        add_leg("k4", land_route(camp_xy, dest), [220, 38, 38, 235], f"마지막 배송 {CO['last_icon']}",
                lambda s: f"{CO['camp_term']} → {dest_label} · {s}")
CUMS = {k: cumulative(v[0]) for k, v in LEGS.items()}


def build_segs(leg_ids, lead_stop=False):
    lens = [CUMS[i][-1] for i in leg_ids]
    stop_w = 0.12 * sum(lens) if sum(lens) > 0 else 1.0
    segs = [("lead", -1, stop_w)] if lead_stop else []
    for n in range(len(leg_ids)):
        segs.append(("move", n, lens[n]))
        if n < len(leg_ids) - 1:
            segs.append(("stop", n, stop_w))
    return segs


def p_at(segs, kind, n, local):
    total = sum(w for _, _, w in segs)
    acc = 0.0
    for k, m, w in segs:
        if k == kind and m == n:
            return (acc + w * local) / total if total else 0.0
        acc += w
    return 0.0


def run_phases(p, leg_ids, lead_stop=False):
    """구간을 순서대로 이동하되 거점에서 정차. 반환: (위치, {구간: 진행률}, 정차 거점 번호/None, 이동 구간 번호)"""
    segs = build_segs(leg_ids, lead_stop)
    total = sum(w for _, _, w in segs)
    target, acc = min(max(p, 0.0), 1.0) * total, 0.0
    for idx, (kind, n, w) in enumerate(segs):
        if target <= acc + w or idx == len(segs) - 1:
            local = min(1.0, max(0.0, (target - acc) / w)) if w else 1.0
            if kind == "lead":
                return LEGS[leg_ids[0]][0][0], {lid: 0.0 for lid in leg_ids}, -1, None
            fr = {lid: (1.0 if m < n else 0.0) for m, lid in enumerate(leg_ids)}
            lid = leg_ids[n]
            if kind == "move":
                fr[lid] = local
                return point_at(LEGS[lid][0], CUMS[lid], local), fr, None, n
            fr[lid] = 1.0
            return LEGS[lid][0][-1], fr, n, None
        acc += w
    return LEGS[leg_ids[-1]][0][-1], {lid: 1.0 for lid in leg_ids}, None, None


def history_to_state(level, hub_hit):
    """택배사 이력의 최고 단계 → (우리 단계, 단계 내 위치)"""
    if level >= 6:
        return 7, 0.0
    if level == 5:
        return 6, p_at(build_segs(["k3", "k4"]), "move", 1, 0.5)
    if level == 4:
        return 6, p_at(build_segs(["k3", "k4"]), "stop", 0, 0.5)
    if level == 3:
        ids = ["k2"] if mode_b else ["k1", "k2"]
        segs = build_segs(ids, lead_stop=mode_b)
        if hub_hit:
            return 5, (p_at(segs, "lead", -1, 0.5) if mode_b else p_at(segs, "stop", 0, 0.5))
        return 5, p_at(segs, "move", len(ids) - 1, 0.5)
    return 5, 0.0


# ============================================================================
# 8. 현재 단계 결정
# ============================================================================
forced = override != stage_names[0]
hold = None  # 'invoice'(국내 송장/이력 대기) / 'address'(도착 주소 대기)
src_note = ""
dom_info = fetch_domestic_tracking(courier_key, dom_wb) if dom_wb else None
hub_hit = bool(hist) and any(re.search(CO["hub_kw"], e["text"]) for e in hist["events"])

if forced:
    cur = stage_names.index(override) - 1
    if mode_b:
        cur = max(5, cur)
    prog = 0.0 if cur >= 7 else manual_pct / 100
    src_note = "사이드바에서 강제 지정한 단계"
elif dom_given:
    if dest and hist:
        cur, prog = history_to_state(hist["level"], hub_hit)
        src_note = "붙여넣은 택배사 이력 기준 (실제 상태)"
    elif dom_info:
        cur, prog = dom_info["stage"], dom_info["progress"]
        src_note = "국내 송장 · 데모 데이터 (택배사 이력을 붙여넣으면 실제 상태로 표시)"
    else:
        cur, prog = 5, 0.0
        src_note = "붙여넣은 택배사 이력"
elif not mode_b:
    intl_info = fetch_tracking(intl_wb)
    cur, prog = intl_info["stage"], intl_info["progress"]
    src_note = f"국제 송장 · {intl_info['source']}"
    if cur >= 5:
        cur, prog, hold = 4, 1.0, "invoice"
if cur >= 5 and dest is None:  # 국내 구간은 검증된 도착 주소가 있어야 그릴 수 있음
    cur, prog, hold = (5, 0.0, "address") if mode_b else (4, 1.0, "address")

if not intl_wb and not dom_given and not forced:
    st.info("송장번호를 입력하면 맞춤 추적이 시작돼요. 지금은 샘플 화면을 보여드려요.")

# ============================================================================
# 9. 현재 위치 · 상태
# ============================================================================
frac = dict.fromkeys(LEGS, 0.0)


def setf(**kw):
    frac.update({k: v for k, v in kw.items() if k in frac})


vehicle_icon = STAGES[cur][0]
if cur == 0:
    pos, status = origin_xy, f"{city} 창고에서 집화 중 (정차)"
elif cur == 1:
    setf(o=prog)
    pos, status, vehicle_icon = point_at(LEGS["o"][0], CUMS["o"], prog), f"{ro(gate_name)} 이동 중", "🚚"
elif cur == 2:
    setf(o=1.0, tr=prog)
    pos = point_at(LEGS["tr"][0], CUMS["tr"], prog)
    status = "항공편으로 이동 중" if air else "바다 위를 이동 중"
elif cur == 3:
    setf(o=1.0, tr=1.0)
    pos, status = entry["arrive"], f"{entry['arrive_name']} 정차 (하역 중)"
elif cur == 4:
    setf(o=1.0, tr=1.0)
    pos = entry["customs"]
    status = {"invoice": "통관 완료 · 국내 송장번호 발급 대기 (세관 정차)",
              "address": "통관 완료 · 도착 주소 입력 대기 (세관 정차)"}.get(
                  hold, f"{entry['customs_name']} 정차 (통관 중)")
elif cur == 5:
    if hold == "address":
        pos, status = main_xy, f"{main_name} 시작 거점 · 도착 주소 입력 대기"
    else:
        setf(o=1.0, tr=1.0)
        if mode_b:
            pos, fr, stop, mv = run_phases(prog, ["k2"], lead_stop=True)
        else:
            pos, fr, stop, mv = run_phases(prog, ["k1", "k2"])
        frac.update(fr)
        if stop is not None:
            status = f"{main_name} 정차 (분류 중)"
        elif mv == 0 and not mode_b:
            status = f"{ro(main_name)} 이동 중"
        else:
            status = f"{ro(region_label)} 이동 중"
    vehicle_icon = "🚚"
elif cur == 6:
    setf(o=1.0, tr=1.0, k1=1.0, k2=1.0)
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

domestic_tracked = bool(dest) and (mode_b or dom_given or (forced and cur >= 5))

# ============================================================================
# 10. 지도
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
        plan = (" · 예상 경로(국내 송장/조회 이력 입력 시 추적)"
                if (lid.startswith("k") and not domestic_tracked) else " · 남은 길")
        main.append({"path": rest_p, "color": GRAY, "title": title, "desc": desc + plan})

points = []
if not mode_b:
    points += [
        {"coord": origin_xy, "color": [34, 197, 94], "radius": 14000,
         "title": "📦 출발 창고", "desc": f"{city} · 대표 위치"},
        {"coord": gate_xy, "color": [14, 165, 233], "radius": 14000,
         "title": f"🛃 {gate_name}", "desc": "해외 출항 통관 · 선적"},
        {"coord": entry["arrive"], "color": [37, 99, 235], "radius": 14000,
         "title": f"{STAGES[3][0]} {entry['arrive_name']}", "desc": "하역 지점 · 대표 위치"},
        {"coord": entry["customs"], "color": [99, 102, 241], "radius": 14000,
         "title": f"🏛️ {entry['customs_name']}", "desc": f"수입 통관 · 좌표: {customs_src}"},
    ]
points.append({"coord": main_xy, "color": [168, 85, 247], "radius": 15000,
               "title": f"🏭 {main_name}",
               "desc": f"{courier_key} 메인 허브 · 좌표: {main_src}" + (" · 국내 구간 시작점" if mode_b else "")})
if dest:
    points += [
        {"coord": camp_xy, "color": [245, 158, 11], "radius": 12000,
         "title": f"🏪 {CO['camp_term']}", "desc": f"도착지 인근 배송 거점 · 좌표: {camp_src}"},
        {"coord": dest, "color": [239, 68, 68], "radius": 16000,
         "title": "🏠 최종 도착지", "desc": f"{geo['display']}<br/>정밀도: {geo['level']}"},
    ]
    if region_xy != main_xy:
        points.append({"coord": region_xy, "color": [217, 70, 239], "radius": 15000,
                       "title": f"🏭 {region_label}", "desc": "도착지 권역 거점 · 대표 위치"})

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

_all = [p for v in LEGS.values() for p in v[0][::max(1, len(v[0]) // 300)]] + [main_xy] + (
    [camp_xy, dest] if dest else [])
_lons, _lats = [p[0] for p in _all], [p[1] for p in _all]
_span = max(max(_lons) - min(_lons), (max(_lats) - min(_lats)) * 1.6, 0.05)
view = pdk.ViewState(
    longitude=(max(_lons) + min(_lons)) / 2, latitude=(max(_lats) + min(_lats)) / 2,
    zoom=min(10, max(1.2, math.log2(430 / _span))), pitch=0, bearing=0)

st.pydeck_chart(
    pdk.Deck(
        layers=layers, initial_view_state=view,
        map_provider="carto", map_style="light",   # Mapbox 토큰 불필요
        tooltip={"html": "<b>{title}</b><br/>{desc}",
                 "style": {"backgroundColor": "#111827", "color": "white", "fontSize": "13px"}},
    ),
    height=500,
)
mode_txt = ("모드 B · 국내 단독 추적" if mode_b
            else "모드 A · 전체 구간 추적" + ("" if intl_wb else " (샘플)"))
st.caption(f"🧭 {mode_txt} · 선·점·아이콘에 마우스를 올리거나 터치하면 설명이 보여요. "
           "'대표 위치'는 실제 시설 좌표가 아닌 근사값이고, '장소 검색 보정'은 OSM에서 찾은 실제 시설 좌표예요.")
if not all(oks):
    st.warning("일부 구간은 라우팅 조회에 실패해 직선으로 표시했어요. 잠시 후 새로고침하면 다시 시도해요. "
               "(해상 구간은 requirements.txt의 searoute 설치가 필요해요.)")

# ============================================================================
# 11. 상세 타임라인
# ============================================================================
st.subheader(f"현재 단계 {min(cur + 1, 8)}/8 · {vehicle_icon} {STAGES[cur][1]}")
st.markdown(f"**📍 {status}**")
st.caption(f"국제 송장 {intl_wb or '(미입력)'} · 국내 송장 {dom_wb or '(미입력)'} · {courier_key} · "
           f"도착지 {dest_addr or '(미입력)'} · 기준: {src_note or '샘플'}")
if forced and cur >= 5 and not dom_given:
    st.caption("※ 국내 송장번호 없이 강제 지정한 데모 상태예요.")
if mode_b:
    st.caption("※ 국내 송장번호(또는 조회 이력)만 있어서 해외 구간(1~5단계)은 생략하고, 택배사 메인 허브부터 보여드려요.")
if hist:
    with st.expander(f"🧾 붙여넣은 택배사 이력 ({len(hist['events'])}건 인식)"):
        for e in hist["events"]:
            st.markdown(f"- `{e['time'] or '-'}` {e['text']}")

for i, (icon, title, desc) in enumerate(STAGES):
    if mode_b and i < 5:
        continue
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

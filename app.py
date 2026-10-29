"""국제소포추적 (Streamlit + Pydeck) v8

실행:  streamlit run app.py
필요:  pip install -r requirements.txt   (streamlit, pydeck, requests, searoute)
선택:  카카오 REST API 키(무료)를 넣으면 번지·건물번호 단위 핀포인트 주소 검색 + 거점(물류센터) 위치 보정
       - 방법 1) .streamlit/secrets.toml 에  KAKAO_REST_API_KEY = "발급받은키"
       - 방법 2) 사이드바 '카카오 REST API 키' 칸에 입력(세션에만 저장)
       키가 없으면 OpenStreetMap(Nominatim)으로 검색하며, 한국 번지 데이터가 일부만 있어
       도로·행정구역 단위로 떨어질 수 있어요(정밀도를 화면에 항상 표시).

[모드 A] 국제 송장번호 입력 → 해외 창고 → 항구/공항 → 해상·항공 → 인천 → 세관 → 택배사 허브 → 배송캠프 → 도착지
[모드 B] 국내 송장번호만 입력 → 택배사 메인 허브 → 권역 허브 → 배송캠프 → 도착지

※ 송장번호 검증은 '형식'(숫자·자릿수)만 합니다. 존재 여부·실제 배송 상태는 택배 조회 API 없이는 알 수 없어서
   국제 단계는 데모 값이고, 국내는 택배사 조회 화면을 붙여넣으면 그 이력으로 맞춰요.
"""
import hashlib
import math
import os
import re
import time
import urllib.parse
from bisect import bisect_right

import pydeck as pdk
import requests
import streamlit as st

st.set_page_config(page_title="국제소포추적", page_icon="📦", layout="wide")

# ============================================================================
# 1. 고정 데이터  (좌표: [경도, 위도], 모두 '대표 위치'이며 도시·항구·공항 단위 근사값)
# ============================================================================
def _o(country, wh, port_name, port, air_name, air, mode="sea"):
    return dict(country=country, wh=wh, port=(port_name, port), air=(air_name, air), mode=mode)


ORIGINS = {
    # ---- 중국 ----
    "상하이 (양산심수항 출항)": _o("중국", [121.47, 31.23], "상하이 양산심수항", [122.06, 30.63], "상하이 푸둥공항", [121.81, 31.14]),
    "웨이하이 (웨이하이항 출항)": _o("중국", [122.12, 37.51], "웨이하이항", [122.20, 37.53], "웨이하이 다수이포공항", [122.23, 37.19]),
    "칭다오 (칭다오항 출항)": _o("중국", [120.30, 36.20], "칭다오항", [120.32, 36.08], "칭다오 자오둥공항", [120.38, 36.36]),
    "옌타이/연태 (옌타이항 출항)": _o("중국", [121.45, 37.46], "옌타이항", [121.40, 37.56], "옌타이 펑라이공항", [120.99, 37.66]),
    "톈진 (톈진항 출항)": _o("중국", [117.20, 39.13], "톈진항", [117.75, 38.99], "톈진 빈하이공항", [117.35, 39.12]),
    "다롄 (다롄항 출항)": _o("중국", [121.58, 38.95], "다롄항", [121.65, 38.93], "다롄 저우수이쯔공항", [121.54, 38.97]),
    "베이징 (톈진항 출항)": _o("중국", [116.40, 39.90], "톈진항", [117.75, 38.99], "베이징 서우두공항", [116.60, 40.08]),
    "선양 (다롄항 출항)": _o("중국", [123.43, 41.80], "다롄항", [121.65, 38.93], "선양 타오셴공항", [123.49, 41.64]),
    "닝보 (닝보저우산항 출항)": _o("중국", [121.55, 29.87], "닝보저우산항", [121.85, 29.93], "닝보 리서공항", [121.46, 29.83]),
    "이우 (닝보저우산항 출항)": _o("중국", [120.07, 29.31], "닝보저우산항", [121.85, 29.93], "이우공항", [120.03, 29.34]),
    "광저우 (난사항 출항)": _o("중국", [113.26, 23.13], "광저우 난사항", [113.60, 22.75], "광저우 바이윈공항", [113.30, 23.39]),
    "선전 (옌톈항 출항)": _o("중국", [114.06, 22.54], "선전 옌톈항", [114.28, 22.58], "선전 바오안공항", [113.81, 22.64]),
    # ---- 일본 ----
    "도쿄 (요코하마항 출항)": _o("일본", [139.69, 35.69], "요코하마항", [139.65, 35.45], "나리타공항", [140.39, 35.77]),
    "오사카 (오사카항 출항)": _o("일본", [135.50, 34.69], "오사카항", [135.42, 34.65], "간사이공항", [135.23, 34.43]),
    "후쿠오카 (하카타항 출항)": _o("일본", [130.40, 33.59], "하카타항", [130.40, 33.62], "후쿠오카공항", [130.45, 33.59]),
    "나고야 (나고야항 출항)": _o("일본", [136.91, 35.18], "나고야항", [136.84, 35.07], "주부 센트레아공항", [136.81, 34.86]),
    # ---- 미국 ----
    "로스앤젤레스 (LA항 출항)": _o("미국", [-118.24, 34.05], "로스앤젤레스항", [-118.27, 33.73], "LA 국제공항", [-118.41, 33.94], "air"),
    "뉴욕 (뉴욕·뉴저지항 출항)": _o("미국", [-74.00, 40.71], "뉴욕·뉴저지항", [-74.15, 40.68], "JFK 공항", [-73.78, 40.64], "air"),
    "시애틀 (시애틀항 출항)": _o("미국", [-122.33, 47.61], "시애틀항", [-122.34, 47.58], "시택 공항", [-122.31, 47.45], "air"),
    "샌프란시스코 (오클랜드항 출항)": _o("미국", [-122.42, 37.77], "오클랜드항", [-122.32, 37.80], "샌프란시스코 공항", [-122.38, 37.62], "air"),
    # ---- 기타 아시아·세계 ----
    "타이베이 (지룽항 출항)": _o("대만", [121.56, 25.04], "지룽항", [121.74, 25.13], "타오위안공항", [121.23, 25.08]),
    "가오슝 (가오슝항 출항)": _o("대만", [120.30, 22.63], "가오슝항", [120.28, 22.61], "가오슝공항", [120.35, 22.58]),
    "홍콩 (콰이칭항 출항)": _o("홍콩", [114.17, 22.30], "콰이칭 컨테이너항", [114.12, 22.31], "홍콩 국제공항", [113.92, 22.31], "air"),
    "호치민 (깟라이항 출항)": _o("베트남", [106.70, 10.78], "깟라이항", [106.79, 10.76], "탄손낫공항", [106.66, 10.82]),
    "방콕 (램차방항 출항)": _o("태국", [100.50, 13.76], "램차방항", [100.88, 13.08], "수완나품공항", [100.75, 13.69]),
    "싱가포르 (싱가포르항 출항)": _o("싱가포르", [103.82, 1.35], "싱가포르항", [103.85, 1.27], "창이공항", [103.99, 1.36], "air"),
    "함부르크 (함부르크항 출항)": _o("독일", [9.99, 53.55], "함부르크항", [9.97, 53.54], "함부르크공항", [9.99, 53.63], "air"),
    "런던 (런던 게이트웨이항 출항)": _o("영국", [-0.13, 51.51], "런던 게이트웨이항", [0.50, 51.50], "히스로공항", [-0.46, 51.47], "air"),
    "시드니 (보태니베이항 출항)": _o("호주", [151.21, -33.87], "보태니베이항", [151.21, -33.97], "시드니공항", [151.18, -33.94], "air"),
    "밴쿠버 (밴쿠버항 출항)": _o("캐나다", [-123.12, 49.28], "밴쿠버항", [-123.07, 49.29], "밴쿠버공항", [-123.18, 49.19], "air"),
}
COUNTRIES = list(dict.fromkeys(v["country"] for v in ORIGINS.values()))
CITIES = {c: [k for k, v in ORIGINS.items() if v["country"] == c] for c in COUNTRIES}

# 구매처/해외 배송업체 → 흔한 출발지 (추정 매핑, 선택 시 국가·출발지 자동 설정)
PLATFORMS = {
    "선택 안 함": None,
    "타오바오·1688·알리익스프레스 (중국 이우)": ("중국", "이우 (닝보저우산항 출항)"),
    "징둥·티몰 (중국 상하이)": ("중국", "상하이 (양산심수항 출항)"),
    "아마존·이베이 (미국 로스앤젤레스)": ("미국", "로스앤젤레스 (LA항 출항)"),
    "라쿠텐·야후재팬 (일본 도쿄)": ("일본", "도쿄 (요코하마항 출항)"),
    "아마존 유럽 (독일 함부르크)": ("독일", "함부르크 (함부르크항 출항)"),
    "큐텐·쇼피 (싱가포르)": ("싱가포르", "싱가포르 (싱가포르항 출항)"),
}

# 인천 입국 거점: 대표 좌표 + 장소검색어(보정용) + 보정 허용 반경(km)
ENTRY = {
    "sea": {"arrive_name": "인천항 부두", "arrive": [126.60, 37.46],
            "customs_name": "인천세관 통관장", "customs": [126.61, 37.47],
            "customs_q": ["인천본부세관", "인천세관"], "radius": 7},
    "air": {"arrive_name": "인천공항 화물터미널", "arrive": [126.45, 37.46],
            "customs_name": "인천공항세관", "customs": [126.44, 37.47],
            "customs_q": ["인천공항세관"], "radius": 9},
}

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

REGION_CENTERS = {
    "수도권": [127.02, 37.45], "대전·충청": [127.38, 36.35], "대구·경북": [128.60, 35.87],
    "부산·경남": [129.08, 35.18], "광주·전라": [126.85, 35.16], "강원 영동": [128.90, 37.75],
}

STAGE_NAMES = ["해외 집화", "해외 이송·수출통관", "해상/항공 운송", "인천 도착", "한국 통관",
               "국내 간선 운송", "배송 출발", "배송 완료"]

KR_BOX = (124.5, 33.0, 131.9, 38.7)  # 대한민국 영역 (경도min, 위도min, 경도max, 위도max)
NOMI = "https://nominatim.openstreetmap.org/search"
KAKAO_ADDR = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD = "https://dapi.kakao.com/v2/local/search/keyword.json"
UA = {"User-Agent": "intl-parcel-tracker-demo/1.0"}

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
    """[모드 A] 국제 송장 데모: 단계(0~4, 5=통관 완료·국내 인계 대기)와 단계 내 위치(0~1).
    실제 연동 시 이 함수만 교체하세요."""
    h = hashlib.sha256((waybill or "DEMO").encode()).digest()
    return {"stage": h[0] % 6, "progress": h[1] / 256, "source": "데모 데이터"}


def fetch_domestic_tracking(courier, invoice):
    """국내 송장 데모(5=간선, 6=배송 출발, 7=완료). 택배사 이력을 붙여넣으면 그쪽이 우선돼요."""
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
    """날짜변경선을 건너는 경로의 경도를 끝점(한국) 기준으로 연속화."""
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
    """항공 구간(대권 경로). a,b는 실제 경도(-180~180). 끝점 기준으로 연속화해 반환."""
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
#    경도가 ±180을 벗어나는 표기(미국·유럽발 경로)는 API 호출 때만 -360n 하고 결과에 다시 더한다.
# ============================================================================
def _wrap(lon):
    return ((lon + 180) % 360) - 180


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
    off = round((a[0] - _wrap(a[0])) / 360) * 360
    try:
        pts = _osrm_route((_wrap(a[0]), a[1]), (_wrap(b[0]), b[1]))
        return [list(a)] + [[p[0] + off, p[1]] for p in pts] + [list(b)], "도로 라우팅(OSRM)", True
    except Exception:
        return [list(a), list(b)], "직선(도로 조회 실패)", False


def sea_route(a, b):
    """a,b: 실제 경도 좌표. 반환 경로는 끝점(b) 기준으로 연속화된 경도."""
    try:
        mid = _sea_route(tuple(a), tuple(b))
        path = unwrap_to_end([list(a)] + mid + [list(b)])
        return path, "해상 항로망(searoute)", True
    except Exception:
        return unwrap_to_end([list(a), list(b)]), "직선(항로 라이브러리 사용 불가)", False


# ============================================================================
# 4. 주소 검증 지오코딩 (대한민국 한정 + 입력 지역과 결과 일치 검증 + 번지·건물번호 정밀 탐색)
# ============================================================================
_last_call = [0.0]


def _throttle():  # Nominatim 이용 정책: 초당 1회 이하
    wait = 1.05 - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def kakao_key():
    key = (st.session_state.get("kakao_key_input") or "").strip()
    if not key:
        try:
            key = str(st.secrets.get("KAKAO_REST_API_KEY", "")).strip()
        except Exception:
            key = ""
    return key or os.environ.get("KAKAO_REST_API_KEY", "").strip()


@st.cache_data(ttl=30 * 86400, show_spinner=False)
def _nomi_query(q, box):
    """box=(경도min, 위도min, 경도max, 위도max) 범위를 강제(bounded)해서 검색. 실패 시 예외."""
    _throttle()
    params = {"q": q, "format": "jsonv2", "limit": 5, "addressdetails": 1,
              "countrycodes": "kr", "accept-language": "ko", "bounded": 1,
              "viewbox": f"{box[0]},{box[3]},{box[2]},{box[1]}"}
    res = requests.get(NOMI, params=params, headers=UA, timeout=10)
    res.raise_for_status()
    out = []
    for it in res.json():
        ad = it.get("address") or {}
        out.append({"lat": float(it["lat"]), "lon": float(it["lon"]),
                    "display": it.get("display_name", ""), "atype": it.get("addresstype", ""),
                    "cc": ad.get("country_code", ""), "house": ad.get("house_number", "")})
    return out


@st.cache_data(ttl=7 * 86400, show_spinner=False)
def _kakao_address(q, key):
    res = requests.get(KAKAO_ADDR, params={"query": q, "size": 5},
                       headers={"Authorization": f"KakaoAK {key}"}, timeout=8)
    res.raise_for_status()
    out = []
    for d in res.json().get("documents", []):
        road, jibun = d.get("road_address") or {}, d.get("address") or {}
        src = road or jibun
        out.append({"lat": float(d["y"]), "lon": float(d["x"]),
                    "display": road.get("address_name") or d.get("address_name", ""),
                    "parts": [src.get("region_1depth_name", ""), src.get("region_2depth_name", ""),
                              src.get("region_3depth_name", "")],
                    "atype": d.get("address_type", "")})
    return out


@st.cache_data(ttl=30 * 86400, show_spinner=False)
def _kakao_keyword(q, x, y, radius_m, key):
    res = requests.get(KAKAO_KEYWORD, params={"query": q, "x": x, "y": y, "radius": radius_m, "size": 5},
                       headers={"Authorization": f"KakaoAK {key}"}, timeout=8)
    res.raise_for_status()
    return [{"lat": float(d["y"]), "lon": float(d["x"]), "name": d.get("place_name", "")}
            for d in res.json().get("documents", [])]


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


def verify_parts(parts, sido, sgg):
    """검색 결과의 행정구역 조각들이 입력한 시·도, 시·군·구와 모두 일치하는지."""
    flat = [w for p in parts for w in re.split(r"[,\s]+", p) if w]
    cores = {canon_core(w) for w in flat}
    return all(canon_core(t) in cores for t in sido) and all(t in flat for t in sgg)


def level_label(atype, house=""):
    a = (atype or "").upper()
    if a == "ROAD_ADDR":
        return "도로명 주소(건물번호 핀포인트)", False
    if a == "REGION_ADDR":
        return "지번 주소(번지 핀포인트)", False
    if a == "ROAD":
        return "도로 단위(번호 미확인)", True
    if a == "REGION":
        return "행정구역 단위(대략)", True
    a = a.lower()
    if house or a in {"building", "house", "apartments", "residential", "commercial", "office",
                      "retail", "shop", "amenity", "industrial", "yes", "public", "school"}:
        return "건물·번지 단위(OSM)", False
    if a in {"road", "highway", "street", "path", "trunk", "primary", "secondary", "tertiary",
             "service", "unclassified", "pedestrian"}:
        return "도로 단위(번호 미확인)", True
    if a in {"neighbourhood", "quarter", "suburb", "village", "hamlet", "isolated_dwelling",
             "locality", "city_block", "borough"}:
        return "동·리 단위(대략)", True
    if a in {"city_district", "county", "municipality", "city", "town", "district"}:
        return "시·군·구 단위(대략)", True
    if a in {"state", "province", "region"}:
        return "시·도 단위(대략)", True
    return "근사 위치", True


def _attempt_queries(norm):
    """가장 구체적인 질의부터: 전체 → 동·호·층 제거 → 뒤에서부터 한 토큰씩 줄이기 (시·군·구는 유지)."""
    toks = norm.split()
    sido, sgg = admin_tokens(norm)
    admin = sido + sgg
    last_admin = max((i for i, t in enumerate(toks) if t in admin), default=-1)
    no_detail = [t for t in toks if not re.fullmatch(r"\d+(-\d+)?(동|호|층)", t)]
    qs = [" ".join(toks), " ".join(no_detail)]
    if admin:
        qs += [" ".join(no_detail[:n]) for n in range(len(no_detail) - 1,
                                                       max(last_admin, 0), -1)]
    out = []
    for q in qs:
        if q and q not in out:
            out.append(q)
    return out[:6]


def _osm_variants(q):
    """OSM은 '작은 단위 → 큰 단위' 쉼표 순서를 더 잘 해석하는 경우가 있어 두 형식을 모두 시도."""
    toks = q.split()
    variants = [q]
    m = re.search(r"([가-힣0-9]+(?:로|길|대로)\s?\d+(?:-\d+)?)$", q)  # '테헤란로 123' 덩어리 유지
    if m and len(toks) > 2:
        head = q[:m.start()].split()
        variants.append(", ".join([m.group(1)] + list(reversed(head))))
    elif len(toks) > 1:
        variants.append(", ".join(reversed(toks)))
    return variants


def _candidates_kakao(q, key):
    out = []
    for c in _kakao_address(q, key):
        label, coarse = level_label(c["atype"])
        out.append({**c, "level": label, "coarse": coarse, "pinpoint": not coarse,
                    "parts": c["parts"], "in_kr": True})
    return out


def _candidates_osm(q, variants=True):
    out, seen = [], set()
    for v in (_osm_variants(q) if variants else [q]):
        for c in _nomi_query(v, KR_BOX):
            if c["cc"] and c["cc"] != "kr":
                continue
            sig = (round(c["lat"], 6), round(c["lon"], 6))
            if sig in seen:
                continue
            seen.add(sig)
            label, coarse = level_label(c["atype"], c["house"])
            parts = [p.strip() for p in c["display"].split(",")]
            out.append({**c, "level": label, "coarse": coarse, "pinpoint": not coarse,
                        "parts": parts})
    return out


def geocode_kr(address):
    """주소 → dict(status, ...). status: ok / empty / network / not_found / mismatch"""
    raw = (address or "").strip()
    if not raw:
        return {"status": "empty"}
    if len(raw) < 2:
        return {"status": "not_found"}
    norm = normalize_address(raw)
    sido, sgg = admin_tokens(norm)
    attempts = _attempt_queries(norm) if (sido or sgg) else [norm]
    key = kakao_key()
    provider, note = ("카카오", "") if key else ("OpenStreetMap", "")
    saw_any = False

    def run(provider):
        nonlocal saw_any
        for qi, q in enumerate(attempts):
            cands = (_candidates_kakao(q, key) if provider == "카카오"
                     else _candidates_osm(q, variants=qi < 2))
            for c in cands:
                if not (KR_BOX[0] <= c["lon"] <= KR_BOX[2] and KR_BOX[1] <= c["lat"] <= KR_BOX[3]):
                    continue
                saw_any = True
                if (sido or sgg) and not verify_parts(c["parts"], sido, sgg):
                    continue
                flat = [w for p in c["parts"] for w in re.split(r"[,\s]+", p) if w]
                sgg_name = next((w for w in flat if re.fullmatch(r"[가-힣]{1,6}(시|군|구)", w)
                                 and not re.search(SIDO_SUFFIX, w)), (sgg[-1] if sgg else ""))
                return {"status": "ok", "coord": [c["lon"], c["lat"]], "display": c["display"],
                        "level": c["level"], "coarse": c["coarse"], "pinpoint": c["pinpoint"],
                        "dropped": q != attempts[0],
                        "ambiguous": not (sido or sgg), "sgg": sgg_name, "provider": provider,
                        "note": note}
        return None

    try:
        res = run(provider)
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", 0)
        if provider == "카카오" and code in (401, 403):  # 키가 잘못된 경우 OSM으로 대체
            note = "카카오 API 키가 올바르지 않아 OpenStreetMap으로 대신 검색했어요."
            try:
                res = run("OpenStreetMap")
            except Exception:
                return {"status": "network"}
        else:
            return {"status": "network"}
    except Exception:
        return {"status": "network"}
    if res:
        return res
    return {"status": "mismatch" if saw_any else "not_found", "wanted": sido + sgg, "note": note}


def refine_poi(queries, approx, radius_km):
    """대표 좌표 근처(반경 내)에서 실제 시설을 찾으면 그 좌표를 쓴다 (카카오 키 있으면 카카오, 없으면 OSM).
    반환: (좌표, '장소 검색 보정' 또는 '대표 위치', 찾은 이름)"""
    key = kakao_key()
    if key:
        try:
            for q in queries[:2]:
                hits = [h for h in _kakao_keyword(q, approx[0], approx[1],
                                                  min(int(radius_km * 1000), 20000), key)
                        if haversine_km([h["lon"], h["lat"]], approx) <= radius_km]
                if hits:
                    b = min(hits, key=lambda h: haversine_km([h["lon"], h["lat"]], approx))
                    return [b["lon"], b["lat"]], "장소 검색 보정(카카오)", b["name"]
        except Exception:
            pass
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.2, math.cos(math.radians(approx[1]))))
    box = (round(approx[0] - dlon, 4), round(approx[1] - dlat, 4),
           round(approx[0] + dlon, 4), round(approx[1] + dlat, 4))
    try:
        for q in queries[:2]:
            hits = [c for c in _nomi_query(q, box)
                    if haversine_km([c["lon"], c["lat"]], approx) <= radius_km]
            if hits:
                b = min(hits, key=lambda c: haversine_km([c["lon"], c["lat"]], approx))
                return [b["lon"], b["lat"]], "장소 검색 보정(OSM)", b["display"].split(",")[0]
    except Exception:
        pass
    return list(approx), "대표 위치", ""


# ============================================================================
# 5. 택배사 조회 화면 붙여넣기 파서 (실제 배송 현황 반영)
# ============================================================================
TS = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:[ T]*\d{1,2}:\d{2})?")
LEVEL_RULES = [
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
# 6. 송장번호 형식 검증
# ============================================================================
INTL_LEN = (10, 20)  # 해외 송장: 숫자 10~20자리
DOM_LEN = (10, 13)   # 국내 송장: 숫자 10~13자리 (CJ·롯데·한진 10~12, 우체국 13)


def valid_invoice(num, lo, hi):
    """숫자만, 자릿수 범위 안, 같은 숫자 반복(0000000000 등) 아님."""
    return bool(re.fullmatch(r"[0-9]+", num)) and lo <= len(num) <= hi and len(set(num)) > 1


# ============================================================================
# 7. 입력 UI
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
    clean = re.sub(r"[^0-9]", "", raw)
    if clean != raw:
        st.session_state["_digit_notice"] = True
    st.session_state[key] = clean


def _on_platform():
    t = PLATFORMS.get(st.session_state.get("platform"))
    if t:
        st.session_state["country"], st.session_state["city"] = t


def _on_country():
    st.session_state["city"] = CITIES[st.session_state["country"]][0]


if st.session_state.pop("_digit_notice", False):
    st.warning("송장번호에는 숫자만 입력할 수 있어서 숫자가 아닌 문자는 자동으로 지웠어요.")

r1a, r1b = st.columns(2)
intl_wb = r1a.text_input(
    "국제 송장번호", value="", key="intl_wb", max_chars=30,
    on_change=_digits_only, args=("intl_wb",),
    placeholder="예: 519717613074 (해외 발송 건 조회 시 입력)",
    help="숫자만 입력할 수 있어요. 형식(자릿수)만 검증하고, 실제 존재 여부는 택배 조회 연동 후 확인할 수 있어요.").strip()
dom_wb = r1b.text_input(
    "국내 송장번호", value="", key="dom_wb", max_chars=30,
    on_change=_digits_only, args=("dom_wb",),
    placeholder="예: 1234-5678-9012 (국내 택배사 번호만 있을 때 입력)",
    help="숫자만 입력할 수 있어요(하이픈은 자동 제거). 국제 송장번호 없이 이것만 입력하면 국내 구간만 추적해요.").strip()
r2a, r2b = st.columns(2)
courier_key = r2a.selectbox("국내 택배사", list(COURIERS))
r2b.selectbox("구매처 / 해외 배송업체 (선택)", list(PLATFORMS), key="platform", on_change=_on_platform,
              help="고르면 흔한 출발국·출발지로 자동 설정돼요(추정). 송장번호 숫자만으로는 출발지를 알 수 없어요.")
r3a, r3b = st.columns(2)
country = r3a.selectbox("출발 국가", COUNTRIES, key="country", on_change=_on_country)
_opts = CITIES[country]
if st.session_state.get("city") not in _opts:
    st.session_state["city"] = _opts[0]
origin_key = r3b.selectbox("출발지 (도시·출항 항구)", _opts, key="city")
dest_addr = st.text_input(
    "최종 도착 주소", value="",
    placeholder="예: 서울특별시 강남구 테헤란로 123",
    help="도로명 주소는 건물번호까지, 지번 주소는 번지까지 입력하면 핀포인트로 찾아요. 동·호수는 자동으로 제외해요.").strip()
with st.expander("📋 택배사 조회 결과 붙여넣기 (선택) — 실제 배송 현황으로 위치 맞추기"):
    paste = st.text_area(
        "택배사 사이트의 배송 조회 화면 내용을 복사해 붙여넣으세요", value="", height=140,
        placeholder="예)\n2026-10-28 09:12 곤지암Hub 간선상차\n2026-10-28 21:40 부산Hub 간선하차\n"
                    "2026-10-29 07:05 양산캠프 배송출발")

# ---- 사이드바: 데모 설정 (유효성 검증 전에 먼저 그려서 항상 보이게) ----
with st.sidebar:
    st.header("⚙️ 설정")
    st.caption("실제 데이터 연동 전, 단계와 아이콘 위치를 바꿔볼 수 있어요. (송장번호는 형식에 맞게 입력해야 해요)")
    transport_pref = st.selectbox("운송 방식", ["자동", "해상", "항공"],
                                  help="자동: 출발지 기본값(중국·일본·대만·동남아 해상, 그 외 항공)")
    stage_choices = ["자동(송장번호/조회내용 기준)"] + [f"{i}. {n}" for i, n in enumerate(STAGE_NAMES)]
    override = st.selectbox("현재 단계 강제 지정", stage_choices)
    manual_pct = st.slider("단계 내 위치 (%)", 0, 100, 50,
                           help="단계 안에서 이동 중인지, 거점에 정차 중인지 조절해요.")
    st.text_input("카카오 REST API 키 (선택)", key="kakao_key_input", type="password",
                  help="입력하면 번지·건물번호 단위 핀포인트 주소 검색과 거점 위치 보정이 정확해져요. "
                       "세션에만 저장돼요. 배포 시에는 secrets에 KAKAO_REST_API_KEY로 넣는 걸 권장해요.")

# ---- 조회 버튼 + 송장번호 유효성 검증 (통과 못 하면 여기서 중단) ----
if st.button("🔍 조회하기", type="primary"):
    st.session_state["queried"] = True
if not st.session_state.get("queried"):
    st.info("국제 또는 국내 송장번호(숫자)를 입력하고 '조회하기'를 눌러주세요.")
    st.stop()

intl_ok = valid_invoice(intl_wb, *INTL_LEN)
dom_ok = valid_invoice(dom_wb, *DOM_LEN)
if (not intl_wb and not dom_wb) or (intl_wb and not intl_ok) or (dom_wb and not dom_ok):
    st.error("유효하지 않은 송장번호입니다. 올바른 번호를 입력해주세요.")
    st.caption(f"국제 송장번호는 숫자 {INTL_LEN[0]}~{INTL_LEN[1]}자리, 국내 송장번호는 숫자 "
               f"{DOM_LEN[0]}~{DOM_LEN[1]}자리여야 해요(같은 숫자만 반복되는 번호는 안 돼요). "
               "둘 중 하나는 반드시 입력해야 조회할 수 있어요.")
    st.stop()

hist = parse_history(paste) if dom_ok else None
if paste.strip() and not dom_ok:
    st.warning("택배사 조회 결과를 반영하려면 국내 송장번호도 함께 입력해 주세요.")
elif paste.strip() and not hist:
    st.warning("붙여넣은 내용에서 배송 단계(집화·간선·배송출발·배송완료 등)를 찾지 못했어요. "
               "택배사 조회 화면의 이력 줄을 그대로 복사해 주세요.")

dom_given = dom_ok
mode_b = dom_ok and not intl_ok  # B: 국내 단독 / A: 국제 송장 있음
CO = COURIERS[courier_key]
main_name, main_xy0 = CO["main"]
O = ORIGINS[origin_key]
air = (transport_pref == "항공") or (transport_pref == "자동" and O["mode"] == "air")
gate_name, gate_real = O["air"] if air else O["port"]
city = origin_key.split(" (")[0]
entry = dict(ENTRY["air" if air else "sea"])

# ============================================================================
# 8. 주소 검증 · 거점 보정
# ============================================================================
with st.spinner("주소와 물류 거점 위치를 확인하는 중..."):
    geo = geocode_kr(dest_addr)
    dest = geo["coord"] if geo["status"] == "ok" else None

    main_xy, main_src, _ = refine_poi(CO["main_q"], main_xy0, 15)
    if not mode_b:
        entry["customs"], customs_src, _ = refine_poi(entry["customs_q"], entry["customs"], entry["radius"])
    else:
        customs_src = "대표 위치"
    camp_found = None
    if dest and geo.get("sgg"):
        sgg_name = geo["sgg"]
        core = re.sub(r"(시|군|구)$", "", sgg_name)
        camp_found = refine_poi([q.format(sgg=sgg_name, core=core) for q in CO["camp_q"]], dest, 12)

if geo.get("note"):
    st.info(geo["note"])
if geo["status"] == "network":
    st.warning("주소 검색 서비스에 연결하지 못했어요. 잠시 후 다시 시도해 주세요. "
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
    (st.success if geo["pinpoint"] else st.info)(
        f"📍 확인된 주소: {geo['display']}  ·  정밀도: {geo['level']}  ·  검색: {geo['provider']}")
    if geo["ambiguous"]:
        st.info("시·도/시·군·구가 없어서 가장 먼저 검색된 위치를 썼어요. 위 '확인된 주소'가 맞는지 확인해 주세요.")
    elif not geo["pinpoint"]:
        tip = (" 카카오 REST API 키(무료)를 사이드바에 넣으면 건물번호 단위까지 찾을 수 있어요."
               if geo["provider"] == "OpenStreetMap" else "")
        st.info(f"번지·건물번호까지는 찾지 못해 '{geo['level']}' 기준 위치로 표시했어요. "
                f"건물번호나 번지를 포함해 입력했는지 확인해 보세요.{tip}")

dest_label = dest_addr or "도착지"
if dest:
    region_name = min(REGION_CENTERS, key=lambda k: haversine_km(REGION_CENTERS[k], dest))
    region_xy = REGION_CENTERS[region_name]
    if haversine_km(region_xy, main_xy) < 15:
        region_xy = main_xy
    dist_rd = haversine_km(region_xy, dest)
    if camp_found and camp_found[1].startswith("장소 검색 보정"):
        camp_xy, camp_src = camp_found[0], f"{camp_found[1]} · {camp_found[2]}"
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

# ============================================================================
# 9. 경로 계산 (구간별로 나눠서 거점마다 정차)
# ============================================================================
GRAY = [148, 163, 184, 170]
LEGS, oks = {}, []
OFF = 0  # 서·남반구 출발지를 한국과 이어 붙이기 위한 경도 오프셋(360의 배수)
origin_xy, gate_xy = list(O["wh"]), list(gate_real)


def add_leg(lid, res, color, title, desc_fn):
    path, src, ok = res
    oks.append(ok)
    LEGS[lid] = (path, color, title, desc_fn(src))


with st.spinner("실제 도로·항로 경로를 계산하는 중..."):
    if not mode_b:
        if air:
            tr = (great_circle(gate_real, entry["arrive"]), "대권 비행경로", True)
        else:
            tr = sea_route(gate_real, entry["arrive"])
        OFF = round((tr[0][0][0] - gate_real[0]) / 360) * 360
        origin_xy = [O["wh"][0] + OFF, O["wh"][1]]
        gate_xy = [gate_real[0] + OFF, gate_real[1]]
        add_leg("o", land_route(origin_xy, gate_xy), [22, 163, 74, 235], "해외 육상 구간 🚚",
                lambda s: f"{city} 창고 → {gate_name} · {s}")
        add_leg("tr", tr, [147, 51, 234, 235] if air else [37, 99, 235, 235],
                "항공 구간 ✈️" if air else "해상 구간 🚢",
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
# 10. 현재 단계 결정
# ============================================================================
forced = override != stage_choices[0]
hold = None  # 'invoice'(국내 송장 대기) / 'address'(도착 주소 대기)
src_note = ""
dom_info = fetch_domestic_tracking(courier_key, dom_wb) if dom_ok else None
hub_hit = bool(hist) and any(re.search(CO["hub_kw"], e["text"]) for e in hist["events"])

if forced:
    cur = stage_choices.index(override) - 1
    if mode_b:
        cur = max(5, cur)
    prog = 0.0 if cur >= 7 else manual_pct / 100
    src_note = "사이드바에서 강제 지정한 단계"
elif dom_given:
    if dest and hist:
        cur, prog = history_to_state(hist["level"], hub_hit)
        src_note = "붙여넣은 택배사 이력 기준 (실제 상태)"
    else:
        cur, prog = dom_info["stage"], dom_info["progress"]
        src_note = "국내 송장 · 데모 데이터 (택배사 이력을 붙여넣으면 실제 상태로 표시)"
else:
    intl_info = fetch_tracking(intl_wb)
    cur, prog = intl_info["stage"], intl_info["progress"]
    src_note = f"국제 송장 · {intl_info['source']}"
    if cur >= 5:
        cur, prog, hold = 4, 1.0, "invoice"
if cur >= 5 and dest is None:  # 국내 구간은 검증된 도착 주소가 있어야 그릴 수 있음
    cur, prog, hold = (5, 0.0, "address") if mode_b else (4, 1.0, "address")

# ============================================================================
# 11. 현재 위치 · 상태
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
# 12. 지도
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
         "title": f"🛃 {gate_name}", "desc": "해외 출항 통관 · 선적 · 대표 위치"},
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
    zoom=min(10, max(0.8, math.log2(430 / _span))), pitch=0, bearing=0)

st.pydeck_chart(
    pdk.Deck(
        layers=layers, initial_view_state=view,
        map_provider="carto", map_style="light",   # Mapbox 토큰 불필요
        tooltip={"html": "<b>{title}</b><br/>{desc}",
                 "style": {"backgroundColor": "#111827", "color": "white", "fontSize": "13px"}},
    ),
    height=500,
)
mode_txt = "모드 B · 국내 단독 추적" if mode_b else "모드 A · 전체 구간 추적"
st.caption(f"🧭 {mode_txt} · 출발지: {origin_key} · 선·점·아이콘에 마우스를 올리거나 터치하면 설명이 보여요. "
           "'대표 위치'는 실제 시설 좌표가 아닌 근사값이고, '장소 검색 보정'은 지도 서비스에서 찾은 실제 시설 좌표예요.")
if not all(oks):
    st.warning("일부 구간은 라우팅 조회에 실패해 직선으로 표시했어요. 잠시 후 새로고침하면 다시 시도해요. "
               "(해상 구간은 requirements.txt의 searoute 설치가 필요해요.)")

# ============================================================================
# 13. 상세 타임라인
# ============================================================================
st.subheader(f"현재 단계 {min(cur + 1, 8)}/8 · {vehicle_icon} {STAGES[cur][1]}")
st.markdown(f"**📍 {status}**")
st.caption(f"국제 송장 {intl_wb or '(미입력)'} · 국내 송장 {dom_wb or '(미입력)'} · {courier_key} · "
           f"도착지 {dest_addr or '(미입력)'} · 기준: {src_note or '-'}")
if mode_b:
    st.caption("※ 국내 송장번호만 입력해서 해외 구간(1~5단계)은 생략하고, 택배사 메인 허브부터 보여드려요.")
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

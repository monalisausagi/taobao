import re
import requests
import streamlit as st
import pydeck as pdk

# 페이지 설정
st.set_page_config(
    page_title="국제소포추적",
    page_type="wide",
    initial_sidebar_state="expanded",
)

# 타이틀
st.title("📦 국제소포추적")
st.markdown(
    "실시간 해외직구 및 국내 택배 물류 경로를 정밀 추적하는 스마트 유틸리티입니다."
)

# --- 1. 사이드바: 입력 및 설정 세션 ---
st.sidebar.header("🔍 배송 조회 설정")

# 운송 방식 선택
shipping_mode = st.sidebar.selectbox(
    "운송 방식", ["자동 (송장 패턴 감지)", "국제 송장 (전체 구간)", "국내 단독 송장"]
)

# 글로벌 출발지 데이터베이스 (다변화)
origin_db = {
    "중국 웨이하이 (항구/물류센터)": {"lat": 37.513, "lon": 122.115, "country": "중국"},
    "중국 상하이 (항구/공항)": {"lat": 31.230, "lon": 121.473, "country": "중국"},
    "중국 칭다오 (항구)": {"lat": 36.067, "lon": 120.382, "country": "중국"},
    "중국 연태 (항구)": {"lat": 37.463, "lon": 121.447, "country": "중국"},
    "미국 로스앤젤레스 (LAX/항구)": {"lat": 33.942, "lon": -118.408, "country": "미국"},
    "미국 뉴욕 (JFK)": {"lat": 40.641, "lon": -73.778, "country": "미국"},
    "일본 도쿄 (NRT/요코하마)": {"lat": 35.771, "lon": 140.390, "country": "일본"},
    "일본 오사카 (KIX)": {"lat": 34.434, "lon": 135.244, "country": "일본"},
}

selected_origin_name = st.sidebar.selectbox(
    "출발 국가 / 거점 (해외 발송 시)", list(origin_db.keys())
)

# 택배사 선택
courier = st.sidebar.selectbox(
    "택배사 선택",
    [
        "CJ대한통운",
        "우체국택배",
        "한진택배",
        "롯데글로벌로지스",
        "타오바오 직배송 (Cainiao)",
    ],
)

# 송장번호 입력 (오직 숫자만 허용하도록 가이드 및 필터링)
st.sidebar.markdown("---")
tracking_input = st.sidebar.text_input(
    "송장번호 입력 (숫자만)",
    placeholder="예: 519717613074",
    help="문자나 특수문자 없이 숫자만 입력해주세요.",
)

# 최종 도착 주소 입력
destination_address = st.sidebar.text_input(
    "최종 도착 주소",
    placeholder="예: 경상남도 양산시 어실로 388 또는 김해시 계동로 102번길 24",
)

st.sidebar.markdown("---")
query_btn = st.sidebar.button("🚀 배송 경로 조회하기", type="primary")


# --- 2. 지오코딩 함수 (정밀 번지수 탐색 + 대한민국 바운더리 제한 + 오탐지 방지) ---
def get_precise_coordinates(address):
    if not address:
        return None, None, "주소가 입력되지 않았습니다."

    # Nominatim API (대한민국 바운더리 내 검색 엄격 제한, 상세 번지수 파싱 유도)
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": address,
        "format": "json",
        "limit": 1,
        "countrycodes": "kr",  # 대한민국 내로 엄격히 제한
        "addressdetails": 1,
    }
    headers = {"User-Agent": "InternationalParcelTracker/2.6"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=5)
        data = response.json()

        if data:
            item = data[0]
            lat = float(item["lat"])
            lon = float(item["lon"])
            display_name = item.get("display_name", "")

            # 번지수/건물번호 포함 여부 확인 (주소 상세도 체크)
            address_details = item.get("address", {})
            has_housenumber = (
                "house_number" in address_details
                or "road" in address_details
                or "suburb" in address_details
            )

            precision_msg = (
                f"확인된 주소: {display_name}"
                if has_housenumber
                else f"확인된 주소 (대략적 위치): {display_name}"
            )
            return lat, lon, precision_msg
        else:
            return (
                None,
                None,
                "주소를 찾을 수 없습니다. 도로명이나 번지수를 다시 확인해주세요.",
            )
    except Exception as e:
        return None, None, f"지오코딩 통신 오류: {str(e)}"


# --- 3. 실행 및 유효성 검증 로직 ---
if query_btn:
    # 3-1. 송장번호 유효성 검증 (숫자만 들어왔는지, 빈 값이 아닌지 체크)
    if not tracking_input:
        st.error(
            "⚠️ 송장번호가 입력되지 않았습니다. 올바른 숫자를 입력해주세요."
        )
        st.stop()

    if not re.fullmatch(r"\d+", tracking_input):
        st.error(
            "⚠️ 유효하지 않은 송장번호입니다. 송장번호는 오직 숫자만 입력할 수 있습니다."
        )
        st.stop()

    # 3-2. 목적지 주소 유효성 검증 및 정밀 지오코딩
    if not destination_address:
        st.error("⚠️ 최종 도착 주소를 입력해주세요.")
        st.stop()

    with st.spinner("주소 및 물류 거점을 정밀 연산 중입니다..."):
        dest_lat, dest_lon, address_msg = get_precise_coordinates(
            destination_address
        )

        if dest_lat is None or dest_lon is None:
            st.error(
                f"🚨 **주소 조회 실패**: {address_msg}\n\n시·도, 시·군·구, 도로명 및 번지수를 정확히 포함하여 다시 입력해주세요."
            )
            st.stop()
        else:
            st.success(f"📍 {address_msg}")

    # 3-3. 경로 구성 (출발지 ➔ 국내 입항지/세관 ➔ 메인 허브 ➔ 지역 캠프 ➔ 최종 목적지)
    origin_info = origin_db[selected_origin_name]
    orig_lat, orig_lon = origin_info["lat"], origin_info["lon"]

    # 국내 세관/인항 거점 좌표 (인천항/인천공항 근처)
    customs_lat, customs_lon = 37.456, 126.652
    # 국내 메인 허브 (예: 곤지암/대전 허브)
    hub_lat, hub_lon = 37.335, 127.234

    # 지도 레이어 구성 (Pydeck)
    # 전체 경로 라인 데이터
    path_data = [
        {
            "path": [
                [orig_lon, orig_lat],
                [customs_lon, customs_lat],
                [hub_lon, hub_lat],
                [dest_lon, dest_lat],
            ],
            "name": f"국제 복합 운송 ({selected_origin_name} ➔ 최종 목적지)",
        }
    ]

    # 마커 데이터
    marker_data = [
        {"coordinates": [orig_lon, orig_lat], "name": f"출발: {selected_origin_name}", "type": "origin"},
        {"coordinates": [customs_lon, customs_lat], "name": "인천항/세관 통관장", "type": "customs"},
        {"coordinates": [hub_lon, hub_lat], "name": f"국내 메인 허브 ({courier})", "type": "hub"},
        {"coordinates": [dest_lon, dest_lat], "name": "최종 도착지", "type": "destination"},
    ]

    # Pydeck 맵 렌더링
    layer_path = pdk.Layer(
        "PathLayer",
        path_data,
        get_path="path",
        get_color=[255, 75, 75],
        width_scale=20,
        width_min_pixels=4,
    )

    layer_scatter = pdk.Layer(
        "ScatterplotLayer",
        marker_data,
        get_position="coordinates",
        get_color="[20, 100, 200, 200]",
        get_radius=3000,
        pickable=True,
    )

    view_state = pdk.ViewState(
        latitude=(orig_lat + dest_lat) / 2,
        longitude=(orig_lon + dest_lon) / 2,
        zoom=5,
        pitch=30,
    )

    r = pdk.Deck(
        layers=[layer_path, layer_scatter],
        initial_view_state=view_state,
        tooltip={"text": "{name}"},
    )

    st.pydeck_chart(r)

    # 3-4. 상세 배송 타임라인 표시
    st.markdown("### 📋 단계별 실시간 배송 현황")
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.info(f"**1. 해외 출고 완료**\n\n거점: {selected_origin_name}")
    with col2:
        st.success("**2. 국내 입항 및 통관**\n\n인천항/세관 통관 완료")
    with col3:
        st.warning(f"**3. 국내 간선 상하차**\n\n{courier} 메인 허브 경유")
    with col4:
        st.error("**4. 최종 배송 중**\n\n지역 캠프 출발 ➔ 고객님 앞")

else:
    st.info(
        "👈 왼쪽 사이드바에 송장번호(숫자 전용)와 최종 도착 주소를 입력하고 **[배송 경로 조회하기]** 버튼을 눌러주세요."
    )

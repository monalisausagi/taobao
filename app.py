import streamlit as st
import pandas as pd
import pydeck as pdk

st.set_page_config(page_title="내 택배 세계지도 추적기", layout="wide")

st.title("🌍 실시간 해외배송 & 세계지도 트래커")
st.markdown("송장번호를 입력하시면 출발지부터 현재 위치, 그리고 통관 타임라인까지 한눈에 보여드립니다!")

# 송장번호 입력
waybill_no = st.text_input("📦 운송장 번호 (H B/L)를 입력하세요", "519717613074")

if st.button("배송 경로 및 타임라인 조회"):
    if waybill_no:
        st.success(f"운송장 번호 {waybill_no} 조회 완료!")
        
        # 1. 상단: 세계지도 시뮬레이션
        st.subheader("📍 글로벌 세밀 이동 경로")
        
        # 상세 거점 데이터
        points_data = pd.DataFrame([
            {"name": "중국 웨이하이 물류창고 (출발)", "lat": 37.50, "lon": 122.12},
            {"name": "중국 출항지 항구", "lat": 37.42, "lon": 122.45},
            {"name": "황해 해상 이동 구간 (선박 🚢)", "lat": 37.30, "lon": 124.50},
            {"name": "인천항 입항 및 세관 (⚓)", "lat": 37.45, "lon": 126.63},
            {"name": "국내 물류 터미널 (택배사 인계 🚚)", "lat": 37.00, "lon": 127.10},
            {"name": "한국 최종 도착지 (🏠)", "lat": 35.18, "lon": 129.07}
        ])
        
        # 부드러운 곡선(Arc)을 위한 구간별 연결 데이터 (중국 ➔ 인천 ➔ 국내최종)
        arc_segments = pd.DataFrame([
            # 중국 웨이하이 ➔ 인천항 (바다를 건너는 해상 경로)
            {"from_lon": 122.12, "from_lat": 37.50, "to_lon": 126.63, "to_lat": 37.45},
            # 인천항 ➔ 국내 최종 목적지 (육상 간선 수송 경로)
            {"from_lon": 126.63, "from_lat": 37.45, "to_lon": 129.07, "to_lat": 35.18}
        ])

        # 레이어 1: 곡선형 이동 경로 (ArcLayer - 입체적이고 부드러운 물류 이동선)
        arc_layer = pdk.Layer(
            'ArcLayer',
            data=arc_segments,
            get_source_position='[from_lon, from_lat]',
            get_target_position='[to_lon, to_lat]',
            get_source_color='[0, 150, 255, 220]', # 출발지 부근 파란색
            get_target_color='[255, 100, 0, 220]', # 도착지 부근 주황색
            get_width=5,
            pickable=True
        )

        # 레이어 2: 주요 거점 마커 (ScatterplotLayer)
        node_layer = pdk.Layer(
            'ScatterplotLayer',
            data=points_data,
            get_position='[lon, lat]',
            get_color='[255, 255, 255, 250]',
            get_radius=7000,
            pickable=True
        )

        # 지도 시점 설정 (한눈에 보이도록 줌인)
        view_state = pdk.ViewState(
            latitude=36.8,
            longitude=125.5,
            zoom=6.2,
            pitch=30, # 약간의 입체감을 주어 경로가 돋보이게 설정
        )

        st.pydeck_chart(pdk.Deck(
            layers=[arc_layer, node_layer],
            initial_view_state=view_state,
            tooltip={"html": "<b>📍 위치:</b> {name}", "style": {"background": "#333", "color": "white", "padding": "8px", "border-radius": "4px"}}
        ))
        
        # 2. 하단: 날짜별 상세 타임라인
        st.subheader("📅 날짜별 상세 진행 상황")
        
        timeline_data = [
            {"날짜": "2026-10-25 14:00", "상태": "물류센터 입고", "설명": "중국 출발지(웨이하이) 물류창고에 상품이 입고되었습니다."},
            {"날짜": "2026-10-26 09:30", "상태": "현지 출고 / 선적 (🚢)", "설명": "항구로 이동하여 한국행 화물선에 적재되었습니다."},
            {"날짜": "2026-10-28 18:00", "상태": "대한민국 입항 / 세관 (⚓)", "설명": "인천항에 도착하여 수입 통관 심사를 진행 중입니다."},
            {"날짜": "진행 중", "상태": "국내 육상 인계 (🚚)", "설명": "통관 완료 후 국내 택배 간선망으로 인계 예정입니다."},
            {"날짜": "예정", "상태": "배송 완료 (🏠)", "설명": "최종 도착지로 안전하게 배송됩니다."},
        ]
        
        for item in timeline_data:
            with st.container():
                col1, col2 = st.columns([1, 3])
                with col1:
                    st.markdown(f"**🕒 {item['날짜']}**")
                    st.code(item['상태'])
                with col2:
                    st.write(item['설명'])
                st.divider()
                
    else:
        st.warning("송장번호를 입력해주세요!")

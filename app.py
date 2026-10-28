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
        st.subheader("📍 글로벌 이동 경로 및 수단")
        
        # 1) 주요 거점 위치 데이터 (이동수단 아이콘 포함)
        points_data = pd.DataFrame([
            {"name": "🇨🇳 중국 웨이하이 물류센터 (출발)", "lat": 37.50, "lon": 122.12, "icon": "🏬"},
            {"name": "🚢 해상 이동 중 (웨이하이 ➔ 인천)", "lat": 37.48, "lon": 124.30, "icon": "🚢"},
            {"name": "🛃 인천항 세관 (통관 진행)", "lat": 37.45, "lon": 126.63, "icon": "⚓"},
            {"name": "🚚 국내 육상 수송 중 (택배사 인계)", "lat": 36.35, "lon": 127.38, "icon": "🚚"},
            {"name": "🏠 한국 최종 도착지", "lat": 35.18, "lon": 129.07, "icon": "📍"}
        ])
        
        # 2) 실제 지상/해상 이동 경로 (지면을 따라 연결되는 구간별 선)
        route_segments = pd.DataFrame([
            # 해상 수송 경로 (중국 웨이하이 ➔ 인천항)
            {"from_lon": 122.12, "from_lat": 37.50, "to_lon": 126.63, "to_lat": 37.45, "type": "해상 수송 (선박)"},
            # 육상 수송 경로 (인천항 ➔ 최종 배송지)
            {"from_lon": 126.63, "from_lat": 37.45, "to_lon": 129.07, "to_lat": 35.18, "type": "육상 수송 (트럭)"}
        ])

        # --- Pydeck 레이어 구성 ---
        
        # [1] 지상을 따라가는 실제 이동 경로 선 (LineLayer)
        line_layer = pdk.Layer(
            'LineLayer',
            data=route_segments,
            get_source_position='[from_lon, from_lat]',
            get_target_position='[to_lon, to_lat]',
            get_color='[30, 144, 255, 220]', # 선명한 파란색 경로선
            get_width=5,
            highlight_color='[255, 255, 0, 255]',
            pickable=True
        )

        # [2] 주요 거점 및 이동수단 위치 표시 (ScatterplotLayer)
        node_layer = pdk.Layer(
            'ScatterplotLayer',
            data=points_data,
            get_position='[lon, lat]',
            get_color='[255, 99, 71, 230]',
            get_radius=8000, # 적절한 크기의 핀 포인트
            pickable=True
        )

        # [3] 이동수단 아이콘 및 위치 명칭 라벨 (TextLayer)
        text_layer = pdk.Layer(
            'TextLayer',
            data=points_data,
            get_position='[lon, lat]',
            get_text='name',
            get_size=15,
            get_color='[255, 255, 255, 255]',
            get_angle=0,
            get_text_anchor='"middle"',
            get_alignment_baseline='"bottom"',
            pixel_offset=[0, -12] # 점 바로 위에 텍스트 배치
        )

        # 지도 카메라 및 시점 설정 (평면 탑뷰에 가까운 angles)
        view_state = pdk.ViewState(
            latitude=36.5,
            longitude=125.5,
            zoom=5.8,
            pitch=0, # 허공 3D 효과를 없애고 평면 직관성 강화
        )

        st.pydeck_chart(pdk.Deck(
            layers=[line_layer, node_layer, text_layer],
            initial_view_state=view_state,
            tooltip={"text": "{name}"}
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

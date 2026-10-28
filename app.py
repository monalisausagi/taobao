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
        
        # 1. 상단: 세계지도 시뮬레이션 (중국 웨이하이/이우 ➔ 대한민국)
        st.subheader("📍 글로벌 이동 경로")
        
        # 지도에 표시할 좌표 예시 (출발지/도착지)
        map_data = pd.DataFrame({
            'lat': [37.5665, 35.1796], 
            'lon': [126.9780, 129.0756],
            'name': ['해외 물류센터 (출발)', '한국 도착지']
        })
        
        # 세계지도 시각화
        st.pydeck_chart(pdk.Deck(
            map_style='mapbox://styles/mapbox/light-v9',
            initial_view_state=pdk.ViewState(
                latitude=36.0,
                longitude=120.0,
                zoom=4,
                pitch=50,
            ),
            layers=[
                pdk.Layer(
                    'ScatterplotLayer',
                    data=map_data,
                    get_position='[lon, lat]',
                    get_color='[200, 30, 0, 160]',
                    get_radius=50000,
                ),
            ],
        ))
        
        # 2. 하단: 날짜별 타임라인 (유니패스 크롤링 데이터 연결 부분)
        st.subheader("📅 날짜별 상세 진행 상황")
        
        timeline_data = [
            {"날짜": "2026-10-25 14:00", "상태": "물류센터 입고", "설명": "중국 출발지 물류창고에 상품이 입고되었습니다."},
            {"날짜": "2026-10-26 09:30", "상태": "현지 출고 / 선적", "설명": "항구로 이동하여 선박에 적재되었습니다."},
            {"날짜": "2026-10-28 18:00", "상태": "대한민국 입항", "설명": "인천항/평택항에 도착하여 하역 작업을 진행 중입니다."},
            {"날짜": "진행 중", "상태": "세관 통관 심사", "설명": "관세청 수입 통관 및 목록 통관 심사 중입니다."},
            {"날짜": "예정", "상태": "국내 택배 인계", "설명": "통관 완료 후 국내 택배사로 전달됩니다."},
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

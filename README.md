# 저평가 기업 스크리너

티커를 넣으면 FMP에서 재무 데이터를 가져와 ROIC, 이익수익률, FCF 수익률, 간이 DCF 안전마진을 계산하고 순위를 매깁니다.
공부용 후보 선별 도구이며 투자 권유가 아닙니다.

## 배포 방법 (Streamlit Community Cloud)

1. GitHub에서 새 저장소를 만듭니다. 이름 예: `stock-screener`
2. 이 폴더의 `app.py`, `requirements.txt`, `README.md` 세 파일을 저장소에 올립니다.
   (GitHub 웹에서 "Add file → Upload files")
3. share.streamlit.io 에 GitHub 계정으로 로그인하고 "Create app"을 누릅니다.
4. 방금 만든 저장소, 브랜치 `main`, 파일 `app.py`를 선택하고 Deploy 합니다.
5. 배포 후 앱 설정(Settings) → Secrets 에 아래 한 줄을 넣고 저장합니다.

```
FMP_API_KEY = "여기에_본인_FMP_키"
```

## 주의

- **API 키를 `app.py`나 저장소 파일에 직접 적지 마세요.** 공개 저장소에 올라가면 누구나 볼 수 있습니다. 반드시 Secrets 칸에 넣거나, Secrets를 쓰지 않을 때는 앱 왼쪽 입력칸에 직접 입력하세요.
- FMP 무료 플랜은 하루 호출 한도가 있고 일부 항목이 유료일 수 있습니다. 한도와 제공 범위는 FMP 사이트에서 확인하세요.
- 은행·보험 등 금융업은 지표가 맞지 않습니다.

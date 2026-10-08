# 채용 터미널

노션에 정리해 둔 채용 공고와 지원 현황을 한 화면에서 보고 처리하는 개인용 웹페이지입니다. GitHub Actions가 1시간마다 노션을 읽어 GitHub Pages로 배포합니다.

## 기능

- 마감 캘린더, 다음 마감, 새 공고, 지원 현황을 한 페이지에 표시
- 새 공고 승인: 「📥 채용 알림 검토함」의 공고를 「📆 취업 지원 현황」으로 옮기고, Gemini가 공고 원문에서 전형 단계, 근무지, 산업군, 회사규모를 채움
- 새 공고 제외, 지원 단계 옮기기 (지원 전부터 최종 결과까지)
- 버튼은 GitHub 토큰을 등록한 기기에서만 활성화되며, 누르면 노션에 반영한 뒤 사이트를 다시 빌드

## 구성

```
build_site.py                       노션 두 DB를 읽어 _site/data.json 생성
site_action.py                      승인, 제외, 단계 옮기기를 노션에 반영
site/index.html                     화면 (data.json을 읽어 그림)
.github/workflows/dashboard_site.yml  1시간마다 빌드 후 Pages 배포
.github/workflows/site_action.yml     사이트 버튼이 호출하는 워크플로
```

## 설정

저장소 Secrets

| 이름 | 용도 |
|---|---|
| `NOTION_TOKEN` | 노션 DB 읽기, 쓰기 |
| `GEMINI_API_KEY` | 승인 시 공고 정보 추출 (없으면 해당 칸을 비워 둠) |
| `DASHBOARD_PASSWORD` | 선택. 넣으면 데이터를 AES-GCM으로 암호화해 비밀번호를 아는 브라우저에서만 보임 (8자 이상) |

Settings > Pages > Source를 **GitHub Actions**로 지정해야 배포됩니다.

# 개인정보 및 네트워크 정책

최종 수정일: 2026-08-20

QuickAccess는 계정, 광고, 사용량 분석 또는 원격 측정 기능을 사용하지 않습니다.
등록한 바로가기 이름과 경로는 QuickAccess 개발자에게 전송되지 않습니다.

## 기기에 저장되는 정보

- `%APPDATA%\QuickAccess\items.json`: 바로가기 이름·경로·종류, 단축키, 화면 설정,
  자동 실행 및 업데이트 확인 선택값
- 같은 폴더의 `items.bak.json`과 `items.corrupt-*.json`: 직전 설정 또는 복구 전 원본
- `%LOCALAPPDATA%\QuickAccess\logs\quickaccess.log`: 실행 및 오류 진단 기록과 회전 백업
- `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\QuickAccessLauncher`:
  Windows 자동 실행을 켰을 때 현재 EXE 경로와 `--startup` 인수

설정 파일과 로그에는 Windows 사용자명, 로컬·네트워크 경로 또는 회사 내부 폴더명이
포함될 수 있습니다. 지원을 요청할 때는 해당 내용을 가린 뒤 공유해 주세요.
`%APPDATA%`의 동기화·로밍 여부는 Windows 계정 또는 회사 정책에 따라 달라질 수
있으며, QuickAccess가 별도로 동기화하지는 않습니다.

## 네트워크 사용

`새 버전 자동 확인`의 기본값은 꺼짐입니다. 사용자가 이 기능을 직접 켠 경우에만
다음 GitHub 공개 API에 HTTPS GET 요청을 보냅니다.

`https://api.github.com/repos/Daksle-number-13/quickaccess-launcher/releases/latest`

요청에는 저장된 바로가기 이름이나 경로가 포함되지 않습니다. 일반적인 인터넷
통신과 마찬가지로 GitHub는 IP 주소, 요청 시각, User-Agent 등의 연결 메타데이터를
처리할 수 있으며, 해당 처리는 [GitHub 개인정보처리방침](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement)의 적용을 받습니다.

사용자가 등록한 웹 링크를 실행하면 Windows 기본 브라우저가 해당 사이트를 엽니다.
그 이후의 통신과 개인정보 처리는 브라우저와 방문한 사이트의 정책을 따릅니다.

## 로컬 Windows 기능

전역 단축키 등록, 파일 탐색기의 현재 선택 항목 확인, 트레이 아이콘, 단일 인스턴스
확인과 파일 아이콘 추출은 Windows의 로컬 API를 사용합니다. 이 과정에서 얻은
경로나 선택 정보는 위 설정 파일 외의 외부 서비스로 전송하지 않습니다.

## 정보 삭제

1. 트레이 아이콘을 우클릭해 `설정`에서 Windows 자동 실행을 끕니다.
2. 트레이 메뉴에서 QuickAccess를 종료하고 EXE를 삭제합니다.
3. 개인 설정과 로그까지 삭제하려면 `%APPDATA%\QuickAccess`와
   `%LOCALAPPDATA%\QuickAccess` 폴더를 삭제합니다.

개인정보 관련 일반 문의는 공개 GitHub Issue를 사용할 수 있습니다. 보안상 민감한
내용은 [보안 정책](SECURITY.md)의 비공개 제보 절차를 따라 주세요.

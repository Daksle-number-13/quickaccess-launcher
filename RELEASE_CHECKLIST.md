# QuickAccess 릴리스 체크리스트

## 자동 검증

- [x] `python -m pytest -q -p no:cacheprovider` — 78개 + 서브테스트 8개 통과
- [x] `python -m compileall -q main.py quickaccess tests`
- [x] `python -m pip check`
- [x] Python 3.13.15 격리 환경에 의존성 재설치
- [x] `.\build.ps1 -Clean -PythonExecutable C:\path\to\python.exe`
- [x] `dist\QuickAccess.exe` 생성
- [x] `dist\QuickAccess.exe --smoke-test` 종료 코드 0
- [x] PyInstaller 경고 검토 — 앱 필수 모듈 누락 없음

검증 빌드: `1.0.0.0`, 13,996,185 bytes,
SHA-256 `838692396CED4061E55782E21F3C7F1DDF77D882958087A60394D858EF343E62`.
현재 Authenticode 상태는 `NotSigned`이며, 서명 후에는 해시와 스모크 테스트를
서명된 파일 기준으로 다시 확인해야 합니다.

## 깨끗한 PC 검증

- [ ] Python이 없는 Windows 10 x64
- [ ] Python이 없는 Windows 11 x64
- [ ] 관리자 권한 없이 실행
- [ ] 최초 실행 JSON 및 기본 항목 생성
- [ ] 일반 창 없이 트레이 상주, 최초 안내 한 번만 표시
- [ ] 같은 EXE를 동시에 두 번 실행해 프로세스 한 개만 유지
- [ ] 트레이의 패널/설정/종료 메뉴와 종료 후 잔류 프로세스 확인
- [ ] 재부팅 후 자동 실행과 설정 해제 확인

## 핫키·보안

- [ ] `Ctrl+Space`가 한글 IME와 업무 앱에서 충돌하지 않음
- [ ] `Ctrl+Shift+Space`가 기본 패널 핫키까지 함께 실행하지 않음
- [ ] 설정에서 유효/중복/이미 점유된 핫키 변경 및 롤백 확인
- [ ] 회사 EDR/백신에 의한 격리 여부 확인
- [ ] 회사 인증서로 Authenticode 서명 및 타임스탬프 적용

## GUI·경로

- [ ] 2~5열, 항목 0/1/20개, 긴 한글 이름과 긴 UNC 경로
- [ ] `ESC` 및 외부 클릭으로 팝업 닫힘
- [ ] 100/125/150/200% 혼합 DPI와 음수 좌표 보조 모니터
- [ ] 작업표시줄이 상/하/좌/우에 있을 때 작업 영역 내부 보정
- [ ] 한글 파일·폴더 추가, 이름 수정, 삭제, 위/아래 이동, 재시작 복원
- [ ] 누락/느린 경로의 회색 `!`, 클릭 후 같은 유형 경로 재지정
- [ ] 연결 끊긴 UNC가 팝업 표시·설정·종료를 막지 않음

## 탐색기 빠른 등록

- [ ] 선택 없음, 파일 선택, 폴더 선택, 다중 선택 정책 확인
- [ ] 여러 탐색기 창 중 활성 창과 일치
- [ ] Windows 11 탐색기 탭 동작 확인
- [ ] Quick Access/내 PC/검색/휴지통 같은 가상 위치 거부
- [ ] OneDrive, UNC, 연결 드라이브, 탐색기 창 닫힘 경쟁 조건
- [ ] COM 접근이 차단되어도 상주 프로세스가 계속 동작

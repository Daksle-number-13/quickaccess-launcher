# QuickAccess 성능 측정과 릴리스 게이트

이 문서는 세 가지 지연을 분리해 측정합니다.

1. **팝업 warm path**: 상주 프로세스가 팝업 내용을 미리 준비한 뒤, 같은 내용을
   다시 표시하는 동기 호출 시간
2. **명령→Map 경로**: `OpenPanelCommand` 발행부터 16ms 명령 pump와 실제 앱
   dispatch를 거쳐 Tk가 팝업의 `<Map>` 이벤트를 전달할 때까지의 시간
3. **one-file 시작**: `QuickAccess.exe` 실행부터 PyInstaller 압축 해제, Python
   import, Tk 생성, 핫키·트레이 준비 완료 로그까지의 시간

각 측정은 원인이 다릅니다. 팝업은 이미 실행 중인 프로세스의 반응성이고,
one-file 시작은 EXE 압축 해제와 백신 검사 영향을 크게 받습니다.

## 팝업 warm path

표준 조건은 밝게 모드, 항목 20개, 3열, 작업 영역 1920×1040, 워밍업 5회와
측정 30회입니다. 명령→Map 관찰값은 별도 워밍업 2회와 측정 10회를 사용합니다.

```powershell
python devtools\benchmark_ui.py `
  --json artifacts\popup-benchmark.json `
  --max-warm-p95-ms 25
```

결과의 주요 항목은 다음과 같습니다.

- `popup_warm_show_call_ms`: `show()` 호출이 Tk 스레드를 점유하는 시간
- `popup_warm_idle_complete_ms`: `show()`부터 `update_idletasks()` 반환까지의 시간
- `open_panel_command_to_map_ms`: 명령 발행부터 의도적으로 한 주기(16ms)를 기다린
  command pump, 앱 dispatch와 Tk `<Map>` 이벤트까지의 대화형 관찰값
- `warm_render_tree_rebuilds`: 동일 내용에서 위젯 트리를 다시 만들었는지 여부

동일 내용에서 렌더 트리가 한 번이라도 바뀌면 측정은 즉시 실패합니다. 시간 기준은
지정 릴리스 PC에서 `popup_warm_show_call_ms`의 nearest-rank p95가 25ms 이하인지를
권장합니다. `popup_warm_idle_complete_ms`는 Tk의 idle 작업까지만 포함하며 OS가 실제
픽셀을 표시했거나 포커스를 옮겼다는 뜻이 아닙니다. 같은 PC의 이전 JSON과 비교할
때만 선택 예산 `--max-idle-p95-ms`를 적용하세요.

`open_panel_command_to_map_ms`는 실제 `CommandBus`, `OpenPanelCommand`, 16ms pump와
`QuickAccessApp`의 dispatch/open 경로를 사용합니다. `<Map>`은 Tk가 창을 mapped
상태로 전환했다는 이벤트일 뿐 첫 픽셀 표시나 키보드 포커스 완료를 보장하지
않습니다. 데스크톱 포커스 정책과 원격 세션 상태에 영향을 받으므로 JSON에 기록하는
관찰 지표이며, 공유 CI 또는 기본 baseline 실패 조건에는 사용하지 않습니다.

```powershell
python devtools\benchmark_ui.py `
  --baseline artifacts\popup-baseline.json `
  --json artifacts\popup-current.json `
  --max-warm-p95-ms 25
```

baseline 비교는 같은 호스트·Python·화면 조건만 허용하며 warm show와 idle 완료
지표만 비교합니다. 기본 허용치는 이전 p95 대비 25% 또는 2ms 중 큰 여유입니다.
단 한 번의 수치는 기준으로 사용하지 않습니다. 스키마 2 이전 JSON은 지표 의미가
달라 baseline으로 사용할 수 없습니다.

## PyInstaller one-file 시작

먼저 최종 EXE를 빌드한 뒤, 로그인된 실제 Windows 데스크톱에서 실행합니다.

```powershell
python devtools\benchmark_onefile.py dist\QuickAccess.exe `
  --runs 6 `
  --json artifacts\onefile-startup.json `
  --max-ready-median-ms 5000
```

측정 도구는 각 실행마다 별도 `LOCALAPPDATA`를 사용하고 `--smoke-test`로 실행합니다.
따라서 사용자의 설정 파일과 시작프로그램 레지스트리를 바꾸지 않습니다. 다만 시험용
전역 단축키와 트레이 아이콘을 잠시 등록하므로 대화형 데스크톱이 필요합니다.

- `log_open_ms`: 프로세스 실행부터 격리 로그 파일이 처음 만들어질 때까지의 시간.
  one-file 압축 해제와 초기 import 비중이 큽니다.
- `resident_ready_ms`: 실행부터 `QuickAccess started` 로그까지의 시간. 구성 로드,
  Tk·앱 생성, 팝업 사전 준비, 핫키 등록과 트레이 준비가 포함됩니다. 현재 구현은
  사전 준비를 마친 뒤 핫키를 활성화하므로 이 시점부터 단축키가 cold render와
  경합하지 않습니다.
- `process_exit_ms`: smoke mode의 의도적인 2.5초 대기까지 포함하므로 성능 게이트로
  사용하지 않습니다. 제한 시간 안에 종료하지 않으면 시작 결과는 보존하고
  `clean_exit=false`로 기록한 뒤 측정 프로세스만 종료합니다.

첫 실행은 결과에 별도 보관하되 게이트에서는 제외합니다. 파일 캐시와 Windows
Defender/회사 EDR 상태를 재현할 수 없기 때문입니다. 나머지 실행의 median을 같은
릴리스 PC의 이전 결과와 비교하고, 일반 개발 PC에서는 5초의 절대 상한을 보조
기준으로 사용할 수 있습니다. 실제 고객 PC의 첫 실행은 재부팅 직후 별도로 기록해야
합니다.

`resident_ready_ms`에는 팝업 사전 준비까지 포함되지만, 실제 픽셀 표시나 포커스
완료 시점은 포함되지 않습니다. 따라서 one-file, warm-path와 명령→Map 결과를 함께
보며, 고객 관점의 실행→첫 패널 표시 시간은 깨끗한 PC 수동 시험으로 보완합니다.

릴리스 smoke 종료까지 강제 검증하려면 `--require-clean-exit`를 추가합니다. 이는
시작 성능과 별개의 건전성 게이트입니다.

## CI에서 flaky하지 않게 운영하기

공유 GitHub Windows runner에는 GUI wall-clock 기준을 두지 않습니다. 호스트 부하,
원격 데스크톱 상태, DPI, Defender 캐시가 결과를 크게 바꿀 수 있기 때문입니다.

- 일반 CI: 통계·게이트 계산의 비GUI 단위 테스트와 기존 팝업 캐시 재사용 테스트만
  실행
- 릴리스 PC 또는 전용 self-hosted runner: 위 두 benchmark를 수동 또는
  `workflow_dispatch`로 실행하고 JSON을 릴리스 증적으로 보관
- 성능 판정: 절대 상한과 같은 PC baseline을 함께 사용
- 회귀 조사: median, p95와 MAD를 모두 보고 단일 최대값만으로 결론 내리지 않음

## 패키징 재현성 감사

현재 빌드는 `requirements*.txt`의 직접 의존성 고정, clean PyInstaller 실행,
UPX 비활성화, 명시적 spec과 버전 리소스를 사용한다는 장점이 있습니다. 다만 다음
항목은 후속 보강이 필요합니다.

| 우선순위 | 현재 위험 | 권장 조치 |
| --- | --- | --- |
| 높음 | `darkdetect`, `packaging` 등 전이 의존성이 해시로 고정되지 않음 | 격리 venv에서 전체 lock 파일과 `--require-hashes` 사용 |
| 높음 | 기존 환경에 설치된 패키지가 분석 결과에 영향을 줄 수 있음 | 매 릴리스마다 빈 venv를 만들고 lock 파일만 설치 |
| 중간 | 빌드가 메타데이터 검사·`pip check`·smoke test·SHA 기록을 한 단계로 강제하지 않음 | 빌드 후 검증과 manifest 생성을 릴리스 스크립트에 연결 |
| 중간 | one-file은 실행 때마다 임시 폴더에 압축을 풀어 백신 영향이 큼 | 빠른 시작이 더 중요하면 무설치 onedir ZIP도 병행 제공 |
| 중간 | 라이선스 원문·SBOM·도구 버전 manifest가 산출물에 자동 포함되지 않음 | 배포 ZIP에 고지 원문과 CycloneDX/SPDX 또는 패키지 manifest 포함 |

코드 서명과 타임스탬프는 최종 EXE 바이트와 SHA-256을 바꾸므로, 성능·smoke·해시는
가능하면 서명된 최종 산출물을 기준으로 다시 기록합니다. 서명 시각이 포함되는
산출물은 바이트 단위 재현과 별개로, 동일 소스·lock·도구 체인으로 재생성 가능한지를
관리해야 합니다.

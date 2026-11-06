# QuickAccess 배포 및 업데이트 정책

## v3 (Rust) 빌드

v3.0.0부터 공개 EXE는 Rust로 만듭니다. Rust(MSVC 도구 모음)와 Windows SDK,
그리고 라이선스 묶음과 매니페스트를 만들 Python 3가 필요합니다.

```powershell
git tag v3.0.0
.\build-rust.ps1 -Clean -SmokeTest
```

`build-rust.ps1`은 다음을 확인합니다.

- `rust/Cargo.toml`, `rust/Cargo.lock`, `rust/resources/quickaccess.rc`의 버전 일치
- 깨끗한 작업 폴더와 정확한 `v버전` 태그 (`-AllowDirty`가 없을 때)
- `cargo test --locked` 전체 통과 (`-SkipTests`는 `-AllowDirty`에서만 허용)
- EXE 파일 버전, VC++ 런타임 의존 없음, 빌드 PC 경로가 남지 않았는지
- `--smoke-test` 실행의 종료 코드 0과 제한 시간

빌드 결과물은 `build\rust-target`에 따로 만듭니다. 그래서 개발용
`rust\target\release\QuickAccess.exe`가 실행 중이어도 빌드가 막히지 않습니다.
산출물 세 파일과 매니페스트 형식은 아래 v2 규칙과 같고, 매니페스트의 `build`
항목에는 Python·PyInstaller 대신 `implementation: "rust"`, Rust 버전, 대상
플랫폼이 기록됩니다. 서명은 같은 `sign.ps1`로 합니다.

Rust 컴파일러는 같은 소스와 도구 버전이면 결과를 대부분 재현하지만, 비트 단위
동일성은 보장하지 않습니다. 공개 배포에서는 항상 SHA-256으로 확인합니다.

## v2 (Python) 통제된 one-file 빌드

아래는 v2.x Python 빌드 규칙입니다. v2.x EXE는 저장소 루트에서 다음처럼 만듭니다.

```powershell
.\build.ps1 -Clean -SmokeTest -PythonExecutable C:\path\to\python.exe
```

빌드는 Windows x64 Python 3.13.15와 PyInstaller 6.19.0을 정확히 요구하며,
실행 전에 다음 항목을 자동으로 확인합니다.

- `pyproject.toml`, `quickaccess.__version__`, `version_info.txt`의 버전 일치
- 런타임 의존성의 완전 고정 및 `requirements.txt`와의 일치
- CI와 로컬 릴리스 Python 버전의 일치
- `quickaccess.spec`의 one-file, 창 모드, 아이콘, 버전 리소스 설정
- 설치된 패키지 간 의존성 충돌 여부
- 전체 테스트와 Python 바이트 컴파일 통과
- Git 작업 폴더가 깨끗하고 현재 커밋에 정확히 `v버전` 태그가 있는지

태그 전 개발 확인 빌드에만 `-AllowDirty`를 명시할 수 있습니다. 이 경우 manifest에
소스 커밋, `source_dirty`와 `release_eligible: false`가 기록되며 서명 및 공개
Release에는 사용할 수 없습니다. `-SkipTests`도 이 확인 빌드에서만 허용됩니다.

`PYTHONHASHSEED=0`과 `SOURCE_DATE_EPOCH`도 고정합니다. 기본 소스 시점은 현재
Git 커밋의 시각입니다. 공개 산출물은 소스 커밋과 정확한 태그를 기록해야 하므로
`.git` 메타데이터가 포함된 checkout에서만 빌드할 수 있습니다.

이 설정은 우연한 차이를 줄이고 같은 빌드 환경에서 결과를 재현하기 위한
것입니다. PyInstaller bootloader, Windows SDK/런타임, 빌드 경로까지 다른 두
컴퓨터에서 비트 단위로 같은 EXE가 된다고 보장하지는 않습니다. 공개 배포에서는
항상 최종 산출물의 SHA-256을 기준으로 동일성을 확인합니다.

## 산출물과 서명 상태

빌드가 끝나면 `dist`에 세 파일이 생성됩니다.

- `QuickAccess.exe`: 설치 없이 실행하는 one-file 앱
- `QuickAccess.release.json`: 버전, 크기, SHA-256, 빌드 런타임,
  소스 커밋·dirty 여부와 Authenticode 상태를 기록한 기계 판독용 매니페스트
- `QuickAccess_Third_Party_Licenses_v버전.zip`: 고정된 런타임·빌드·테스트·문서
  패키지의 원본 라이선스 및 제3자 고지 묶음

서명하지 않은 빌드는 매니페스트에 반드시 다음처럼 기록됩니다.

```json
"authenticode": {
  "signed": false,
  "status": "NotSigned"
}
```

인증서가 준비되면 `sign.ps1`을 사용합니다. 이 스크립트는 서명 전 EXE 해시가
빌드 매니페스트와 같은지 검사하고, 코드 서명 EKU·개인 키·유효기간·RFC 3161
타임스탬프를 검증합니다. 서명 후 변경된 크기와 SHA-256, `Valid` 상태를 같은
매니페스트에 원자적으로 다시 기록합니다.

미서명 파일을 배포할 수는 있지만 Release 설명에 `NotSigned`와 SHA-256을
그대로 공개해야 합니다. 인증서가 없는 파일을 서명된 것처럼 표시하거나 자체
서명 인증서를 공인 서명처럼 설명하지 않습니다.

## GitHub Release 규칙

빌드 실패·시간 초과·자식 프로세스 정리와 서명 전 거부 경로는 실제 사용자 앱을
실행하지 않는 전용 시험 파일로 재현할 수 있습니다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File devtools/verify_release_scripts.ps1
```

이 명령은 13개 동작을 검사하고 결과 JSON을 `artifacts/audit-build`에 남깁니다.
최종 QuickAccess EXE의 실행 검사는 앞의 `build.ps1 -SmokeTest`가 별도로 수행합니다.

공개 Release에는 아래 세 파일을 같은 빌드에서 나온 한 묶음으로 첨부합니다.

- `QuickAccess.exe`
- `QuickAccess.release.json`
- `QuickAccess_Third_Party_Licenses_v버전.zip`

Release 태그는 `vMAJOR.MINOR.PATCH` 형식을 사용하고, 앱 내부 버전은 앞의 `v`를
제외한 값과 같아야 합니다. 업로드 후 매니페스트의 SHA-256과 다운로드한 EXE의
해시를 다시 비교합니다.

앱의 업데이트 검사는 GitHub `releases/latest` API만 사용합니다. 응답에 포함된
페이지 및 EXE 주소는 다음 조건을 모두 만족할 때만 사용자 동작에 전달합니다.

- HTTPS 및 정확한 `github.com` 호스트
- 공식 `Daksle-number-13/quickaccess-launcher` 저장소 경로
- 응답 태그와 URL 태그의 정확한 일치
- 자산 이름과 마지막 경로가 정확히 `QuickAccess.exe`
- 쿼리, 프래그먼트, 사용자 정보가 없는 주소

즉, API 또는 중간 프록시가 다른 사이트 주소를 반환해도 앱은 그 주소를 열거나
다운로드 주소로 노출하지 않습니다. 현재 앱은 자동 설치하지 않고 공식 Release
페이지로 안내합니다.

## 업데이트 결과 상태

업데이트 서비스는 기존 `available` 값을 유지하면서 수동 확인 화면에서 사용할
수 있는 네 상태를 제공합니다.

- `latest`: 현재 버전이 최신이거나 공개 최신 버전보다 높음
- `update_available`: 더 높은 정식 Release가 있음
- `offline`: 타임아웃, DNS, 프록시, 네트워크 연결 실패
- `error`: HTTP 오류, 잘못된 버전·저장소·JSON·GitHub 응답

자동 확인은 이전과 같이 `available`이 참일 때만 알림을 띄우므로 기존 사용자에게
오류 알림을 추가하지 않습니다. 향후 설정 화면의 수동 확인 버튼은 `status`를
읽어 최신/업데이트/오프라인/오류를 구분해 표시할 수 있습니다.

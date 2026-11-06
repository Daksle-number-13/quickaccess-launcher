# 제3자 소프트웨어 고지

QuickAccess 자체 소스는 [MIT License](LICENSE)로 배포됩니다.

v3.0.0부터 QuickAccess.exe는 Rust로 만든 네이티브 프로그램입니다. Python,
CustomTkinter, Pillow, pystray, PyInstaller는 더 이상 EXE에 들어 있지 않습니다.
v2.x까지의 고지는 해당 버전 태그의 이 파일에 남아 있습니다.

## EXE에 포함되는 Rust 라이브러리

아래 목록은 `cargo metadata`가 보고하는 일반 의존성 전체입니다. 빌드할 때만 쓰이는
매크로 라이브러리(`syn`, `quote`, `proc-macro2`, `unicode-ident`,
`windows-implement`, `windows-interface`)도 보수적으로 포함했습니다.

| 구성요소 | 버전 | 라이선스 |
| --- | --- | --- |
| caseless | 0.2.2 | MIT |
| cfg-if | 1.0.5 | MIT 또는 Apache-2.0 |
| equivalent | 1.0.3 | Apache-2.0 또는 MIT |
| foldhash | 0.2.0 | Zlib |
| getrandom | 0.4.3 | MIT 또는 Apache-2.0 |
| hashbrown | 0.17.1 | MIT 또는 Apache-2.0 |
| indexmap | 2.14.2 | Apache-2.0 또는 MIT |
| itoa | 1.0.18 | MIT 또는 Apache-2.0 |
| memchr | 2.8.3 | Unlicense 또는 MIT |
| proc-macro2 | 1.0.107 | MIT 또는 Apache-2.0 |
| quote | 1.0.47 | MIT 또는 Apache-2.0 |
| serde_core | 1.0.229 | MIT 또는 Apache-2.0 |
| serde_json | 1.0.154 | MIT 또는 Apache-2.0 |
| syn | 2.0.119 | MIT 또는 Apache-2.0 |
| tinyvec | 1.13.3 | Zlib, Apache-2.0 또는 MIT |
| unicode-ident | 1.0.27 | (MIT 또는 Apache-2.0) 및 Unicode-3.0 |
| unicode-normalization | 0.1.26 | (MIT 또는 Apache-2.0) 및 Unicode-3.0 |
| uuid | 1.29.0 | Apache-2.0 또는 MIT |
| windows 및 windows-* (11개) | 0.61.x 외 | MIT 또는 Apache-2.0 |
| zmij | 1.0.23 | MIT |

## 런타임

- **Rust 표준 라이브러리**: MIT 또는 Apache-2.0. 위 라이브러리와 같은 원문이
  적용됩니다.
- **Microsoft C 런타임(정적 링크)**: Visual Studio 빌드 도구의 재배포 가능한
  정적 라이브러리를 사용합니다. 그래서 사용자 PC에 Visual C++ 재배포 패키지가
  없어도 실행됩니다.
- **Windows 구성요소**: Direct2D, DirectWrite, WIC, WinHTTP 같은 Windows 기본
  구성요소를 호출만 하며, EXE에 포함하지 않습니다.

## 라이선스 원문 묶음

빌드는 `QuickAccess_Third_Party_Licenses_v버전.zip`을 함께 만듭니다. 이 묶음에는
위 각 라이브러리의 패키지에 들어 있는 라이선스 원문, 이 고지,
`CRATE_VERSIONS.txt`(버전과 라이선스 표현식)가 들어 있습니다. 매니페스트에도
이 묶음의 파일명·크기·SHA-256이 기록됩니다.

이 요약은 각 라이선스 원문을 대체하지 않습니다. 의존성을 바꾼 뒤에는 이 표와
묶음 내용을 다시 확인합니다.

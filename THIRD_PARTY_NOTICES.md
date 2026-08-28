# 제3자 소프트웨어 고지

QuickAccess 자체 소스는 [MIT License](LICENSE)로 배포됩니다. QuickAccess.exe에는
여러 제3자 구성요소의 코드 또는 리소스가 포함됩니다. 아래 목록은 v1.2.5 EXE의
PyInstaller 빌드 분석과 당시 설치된 패키지의 라이선스 파일에서 확인한 주요
구성요소를 기준으로 작성했습니다.

| 구성요소 | 확인 버전 | 라이선스 | 프로젝트 |
| --- | --- | --- | --- |
| Python | 3.13.15 | Python Software Foundation License Version 2 | [python.org](https://www.python.org/) |
| Tcl/Tk | Python 3.13.15 포함 버전 | Tcl/Tk License | [tcl.tk](https://www.tcl-lang.org/) |
| CustomTkinter | 5.2.2 | MIT License | [TomSchimansky/CustomTkinter](https://github.com/TomSchimansky/CustomTkinter/tree/v5.2.2) |
| Roboto Regular/Medium fonts | CustomTkinter 포함 파일 | Apache License 2.0 | [googlefonts/roboto-2](https://github.com/googlefonts/roboto-2) |
| darkdetect | 0.8.0 | BSD 3-Clause License | [albertosottile/darkdetect](https://github.com/albertosottile/darkdetect/tree/v0.8.0) |
| packaging | 26.3 | Apache License 2.0 **또는** BSD 2-Clause License | [pypa/packaging](https://github.com/pypa/packaging) |
| Pillow | 12.3.0 | MIT-CMU License | [python-pillow/Pillow](https://github.com/python-pillow/Pillow) |
| pystray | 0.19.5 | GNU Lesser General Public License v3.0 | [moses-palmer/pystray](https://github.com/moses-palmer/pystray/tree/v0.19.5) |
| six | 1.17.0 | MIT License | [benjaminp/six](https://github.com/benjaminp/six) |
| pywin32 | 311 | Python Software Foundation License | [mhammond/pywin32](https://github.com/mhammond/pywin32) |
| PyInstaller bootloader | 6.19.0 | GPL-2.0-or-later with the PyInstaller Bootloader Exception | [pyinstaller/pyinstaller](https://github.com/pyinstaller/pyinstaller/tree/v6.19.0) |

Windows용 Python 배포본에는 Microsoft Distributable Code와 bzip2, libffi,
OpenSSL, zlib, Expat 등 추가 런타임 구성요소가 포함됩니다. 이들의 정확한 저작권
고지와 재배포 조건은 해당 Python 배포본에 포함된 `LICENSE.txt`를 기준으로 하며,
바이너리 배포 시 그 원문도 함께 제공해야 합니다.

CustomTkinter 5.2.2의 패키지 메타데이터에는 과거 `CC0` 표기가 남아 있지만, 해당
wheel에 포함된 `LICENSE` 파일과 v5.2.2 소스 태그의 라이선스 원문은 MIT License입니다.
위 표는 실제 라이선스 파일을 기준으로 적었습니다.

pystray는 LGPLv3 적용 라이브러리입니다. QuickAccess 저장소에는 프로그램 소스와
PyInstaller 빌드 명세가 공개되어 있습니다. 재배포자는 LGPLv3를 포함한 각 라이선스의
고지·원문 제공 및 수정·재결합 관련 의무를 직접 검토해야 합니다.

이 요약은 각 라이선스 원문을 대체하지 않습니다. 공개 바이너리를 새로 만들 때는
빌드 환경의 실제 패키지 버전을 다시 확인하고, 각 배포 패키지의 원본 라이선스 파일을
EXE와 함께 제공해야 합니다.

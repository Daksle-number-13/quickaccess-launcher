"""Build the Korean QuickAccess easy user manual as a polished PDF."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from PIL import Image as PILImage


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "QuickAccess_Easy_Manual_KO.pdf"
POPUP_SCREENSHOT = ROOT / "docs" / "screenshots" / "quickaccess-popup-light.png"
SETTINGS_SCREENSHOT = ROOT / "docs" / "screenshots" / "quickaccess-settings-light.png"
BRAND_MARK = ROOT / "assets" / "quickaccess-mark.png"

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_X = 17 * mm
MARGIN_TOP = 17 * mm
MARGIN_BOTTOM = 16 * mm
CONTENT_WIDTH = PAGE_WIDTH - MARGIN_X * 2

BLUE = colors.HexColor("#1769E8")
BLUE_DARK = colors.HexColor("#0D47A1")
BLUE_SOFT = colors.HexColor("#EAF2FF")
INK = colors.HexColor("#172033")
MUTED = colors.HexColor("#667085")
LINE = colors.HexColor("#D9E2F0")
SURFACE = colors.HexColor("#F7F9FC")
GREEN = colors.HexColor("#16855B")
AMBER = colors.HexColor("#B85C00")
RED = colors.HexColor("#C62828")


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("Malgun", r"C:\Windows\Fonts\malgun.ttf"))
    pdfmetrics.registerFont(TTFont("MalgunBold", r"C:\Windows\Fonts\malgunbd.ttf"))
    pdfmetrics.registerFontFamily(
        "Malgun",
        normal="Malgun",
        bold="MalgunBold",
        italic="Malgun",
        boldItalic="MalgunBold",
    )


register_fonts()
BASE = getSampleStyleSheet()

TITLE = ParagraphStyle(
    "TitleKO",
    parent=BASE["Title"],
    fontName="MalgunBold",
    fontSize=27,
    leading=35,
    textColor=INK,
    alignment=TA_LEFT,
    spaceAfter=7 * mm,
)
SUBTITLE = ParagraphStyle(
    "SubtitleKO",
    parent=BASE["Normal"],
    fontName="Malgun",
    fontSize=12,
    leading=19,
    textColor=MUTED,
    spaceAfter=7 * mm,
)
H1 = ParagraphStyle(
    "H1KO",
    parent=BASE["Heading1"],
    fontName="MalgunBold",
    fontSize=20,
    leading=27,
    textColor=INK,
    spaceBefore=2 * mm,
    spaceAfter=5 * mm,
)
H2 = ParagraphStyle(
    "H2KO",
    parent=BASE["Heading2"],
    fontName="MalgunBold",
    fontSize=13,
    leading=19,
    textColor=INK,
    spaceBefore=3 * mm,
    spaceAfter=2 * mm,
)
BODY = ParagraphStyle(
    "BodyKO",
    parent=BASE["BodyText"],
    fontName="Malgun",
    fontSize=10.2,
    leading=16.2,
    textColor=INK,
    spaceAfter=2.2 * mm,
)
SMALL = ParagraphStyle(
    "SmallKO",
    parent=BODY,
    fontSize=8.6,
    leading=13.2,
    textColor=MUTED,
)
CAPTION = ParagraphStyle(
    "CaptionKO",
    parent=SMALL,
    fontSize=8.2,
    leading=12,
    alignment=TA_CENTER,
    spaceBefore=1.5 * mm,
    spaceAfter=3 * mm,
)
STEP_TITLE = ParagraphStyle(
    "StepTitleKO",
    parent=BODY,
    fontName="MalgunBold",
    fontSize=11,
    leading=16,
    spaceAfter=0,
)
STEP_BODY = ParagraphStyle(
    "StepBodyKO",
    parent=BODY,
    fontSize=9.4,
    leading=14.5,
    textColor=MUTED,
    spaceAfter=0,
)


class PageAccent(Flowable):
    def __init__(self, width: float = 34 * mm) -> None:
        super().__init__()
        self.width = width
        self.height = 2.5 * mm

    def draw(self) -> None:
        self.canv.setFillColor(BLUE)
        self.canv.roundRect(0, 0, self.width, self.height, self.height / 2, fill=1, stroke=0)


def screenshot(path: Path, max_width: float, caption: str) -> KeepTogether:
    with PILImage.open(path) as image:
        width, height = image.size
    ratio = min(1.0, max_width / width)
    displayed = Image(str(path), width=width * ratio, height=height * ratio)
    framed = Table([[displayed]], colWidths=[width * ratio], hAlign="CENTER")
    framed.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, LINE),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return KeepTogether([framed, Paragraph(caption, CAPTION)])


def step(number: int, title: str, description: str) -> Table:
    badge = Paragraph(
        f'<font color="white"><b>{number}</b></font>',
        ParagraphStyle(
            f"Badge{number}",
            parent=BODY,
            fontName="MalgunBold",
            fontSize=11,
            leading=18,
            alignment=TA_CENTER,
        ),
    )
    text = [Paragraph(title, STEP_TITLE), Spacer(1, 0.8 * mm), Paragraph(description, STEP_BODY)]
    table = Table([[badge, text]], colWidths=[10 * mm, CONTENT_WIDTH - 16 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), BLUE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("BACKGROUND", (1, 0), (1, 0), SURFACE),
                ("LEFTPADDING", (0, 0), (0, 0), 2 * mm),
                ("RIGHTPADDING", (0, 0), (0, 0), 2 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3.2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2 * mm),
                ("LEFTPADDING", (1, 0), (1, 0), 4 * mm),
                ("RIGHTPADDING", (1, 0), (1, 0), 4 * mm),
            ]
        )
    )
    return table


def note(title: str, text: str, *, tone: str = "blue") -> Table:
    palette = {
        "blue": (BLUE_SOFT, BLUE_DARK),
        "green": (colors.HexColor("#EAF8F1"), GREEN),
        "amber": (colors.HexColor("#FFF4E5"), AMBER),
        "red": (colors.HexColor("#FDECEC"), RED),
    }
    background, accent = palette[tone]
    content = Paragraph(f'<font color="{accent.hexval()}"><b>{title}</b></font><br/>{text}', BODY)
    table = Table([[content]], colWidths=[CONTENT_WIDTH])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
                ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5 * mm),
            ]
        )
    )
    return table


def keycap(text: str) -> Table:
    label = Paragraph(text, ParagraphStyle("Keycap", parent=BODY, fontName="MalgunBold", alignment=TA_CENTER))
    table = Table([[label]], colWidths=[52 * mm], rowHeights=[10 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.8, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1 * mm),
            ]
        )
    )
    return table


def page_header(title: str, kicker: str) -> list[Flowable]:
    return [
        Paragraph(kicker, ParagraphStyle("Kicker", parent=SMALL, fontName="MalgunBold", textColor=BLUE)),
        Spacer(1, 1.5 * mm),
        Paragraph(title, H1),
        PageAccent(),
        Spacer(1, 5 * mm),
    ]


def draw_page(canvas, document) -> None:  # type: ignore[no-untyped-def]
    canvas.saveState()
    page = canvas.getPageNumber()
    if page > 1:
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN_X, 12 * mm, PAGE_WIDTH - MARGIN_X, 12 * mm)
        canvas.setFont("Malgun", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN_X, 7.5 * mm, "QuickAccess Easy Manual  |  v1.2.4")
        canvas.drawRightString(PAGE_WIDTH - MARGIN_X, 7.5 * mm, str(page))
    canvas.restoreState()


def build_story() -> list[Flowable]:
    story: list[Flowable] = []

    # Cover
    brand = Image(str(BRAND_MARK), width=24 * mm, height=24 * mm)
    story.extend(
        [
            Spacer(1, 12 * mm),
            brand,
            Spacer(1, 7 * mm),
            Paragraph("QuickAccess", TITLE),
            Paragraph("쉬운 사용 설명서", ParagraphStyle("CoverSub", parent=TITLE, fontSize=22, leading=29, textColor=BLUE)),
            Paragraph("파일, 폴더, 웹 링크를 단축키 한 번으로 여는 Windows 런처", SUBTITLE),
            Spacer(1, 4 * mm),
            screenshot(POPUP_SCREENSHOT, CONTENT_WIDTH * 0.88, "실제 QuickAccess 빠른 실행 패널"),
            Spacer(1, 4 * mm),
            note("5분이면 시작할 수 있어요", "설치 없이 실행하고, 원하는 항목을 등록한 뒤 Ctrl + Space만 누르면 됩니다.", tone="green"),
            Spacer(1, 9 * mm),
            Paragraph("버전 1.2.4  |  Windows 10/11 x64  |  2026-08-24", SMALL),
            PageBreak(),
        ]
    )

    # Download and first run
    story.extend(page_header("1. 다운로드하고 처음 실행하기", "처음 한 번만 하면 됩니다"))
    story.extend(
        [
            step(1, "GitHub에서 실행 파일 받기", "https://github.com/Daksle-number-13/quickaccess-launcher/releases 에서 최신 버전의 QuickAccess.exe를 다운로드합니다."),
            Spacer(1, 3 * mm),
            step(2, "QuickAccess.exe 실행", "설치 과정은 없습니다. 다운로드한 파일을 더블 클릭하면 시스템 트레이에 상주합니다."),
            Spacer(1, 3 * mm),
            step(3, "패널 열기", "키보드에서 Ctrl + Space를 함께 누릅니다. 마우스 커서가 있는 화면에 패널이 나타납니다."),
            Spacer(1, 6 * mm),
            note("Windows에서 경고가 표시되나요?", "현재 공개 파일에는 Authenticode 코드 서명이 적용되지 않아 Windows가 '인식할 수 없는 앱' 경고를 표시할 수 있습니다. GitHub 공식 Release에서 받은 파일인지 확인하고 Release 안내의 SHA-256 값과 파일 해시를 비교한 뒤 '추가 정보' - '실행'을 선택하세요.", tone="amber"),
            Spacer(1, 5 * mm),
            Paragraph("기본으로 등록되는 항목", H2),
            Paragraph("- 다운로드 폴더<br/>- 문서 폴더", BODY),
            note("관리자 권한은 필요하지 않습니다", "QuickAccess는 키보드 전체를 감시하지 않고 Windows 공식 전역 단축키 기능만 사용합니다."),
            PageBreak(),
        ]
    )

    # Panel
    story.extend(page_header("2. 빠른 실행 패널 사용하기", "가장 자주 쓰는 화면"))
    story.extend(
        [
            screenshot(POPUP_SCREENSHOT, CONTENT_WIDTH, "Ctrl + Space를 눌렀을 때 나타나는 실제 패널"),
            step(1, "항목 열기", "원하는 카드를 클릭하면 파일이나 폴더는 Windows 탐색기/연결 프로그램으로, 웹 링크는 기본 브라우저로 열립니다."),
            Spacer(1, 2.5 * mm),
            step(2, "키보드로 선택", "방향키로 카드 사이를 이동하고 Enter 또는 Space를 누르면 선택한 항목이 열립니다."),
            Spacer(1, 2.5 * mm),
            step(3, "설정 열기", "패널 오른쪽 위의 톱니바퀴 아이콘을 누르면 항목을 추가하거나 순서를 바꿀 수 있습니다."),
            Spacer(1, 4 * mm),
            note("빨간 카드가 보이면", "파일이나 폴더의 위치를 찾을 수 없다는 뜻입니다. 해당 카드를 누르고 새 위치를 지정하세요. 웹 링크는 인터넷 연결 여부와 관계없이 등록 상태를 유지합니다.", tone="red"),
            PageBreak(),
        ]
    )

    # Add items
    story.extend(page_header("3. 파일, 폴더, 웹 링크 추가하기", "설정 - 바로가기"))
    story.extend(
        [
            screenshot(SETTINGS_SCREENSHOT, CONTENT_WIDTH, "실제 환경 설정 화면 - 오른쪽 위에 3개의 추가 버튼이 있습니다"),
            Paragraph("추가하려는 종류에 맞는 버튼을 누르세요", H2),
            Table(
                [
                    [Paragraph("폴더 추가", STEP_TITLE), Paragraph("자주 여는 작업 폴더를 선택합니다.", BODY)],
                    [Paragraph("파일 추가", STEP_TITLE), Paragraph("문서, 엑셀, PDF, 프로그램 등을 선택합니다.", BODY)],
                    [Paragraph("웹 링크", STEP_TITLE), Paragraph("인터넷 주소와 패널에 표시할 이름을 입력합니다.", BODY)],
                ],
                colWidths=[38 * mm, CONTENT_WIDTH - 38 * mm],
                style=TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                        ("BACKGROUND", (0, 0), (0, -1), BLUE_SOFT),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                        ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ]
                ),
            ),
            Spacer(1, 4 * mm),
            note("순서를 바꾸려면", "각 항목 오른쪽의 위/아래 화살표를 누르세요. '수정'은 표시 이름과 웹 링크 주소를 바꾸고, 휴지통은 항목을 삭제합니다. 삭제 직후에는 실행취소가 가능합니다.", tone="green"),
            PageBreak(),
        ]
    )

    # Web links
    story.extend(page_header("4. 인터넷 링크 등록하기", "v1.2.0 새 기능"))
    story.extend(
        [
            step(1, "웹 링크 버튼 누르기", "환경 설정의 바로가기 화면 오른쪽 위에서 '웹 링크'를 누릅니다."),
            Spacer(1, 3 * mm),
            step(2, "인터넷 주소 입력", "예: naver.com 또는 https://www.naver.com. 앞에 http:// 또는 https://가 없으면 안전한 https://가 자동으로 붙습니다."),
            Spacer(1, 3 * mm),
            step(3, "표시 이름 입력", "패널에서 알아보기 쉬운 이름을 입력합니다. 예: 네이버, 사내 포털, 근태 시스템."),
            Spacer(1, 3 * mm),
            step(4, "패널에서 실행", "Ctrl + Space를 누르고 링크 카드를 클릭하면 Windows의 기본 브라우저에서 열립니다."),
            Spacer(1, 6 * mm),
            Paragraph("입력 예시", H2),
            Table(
                [
                    [Paragraph("입력", STEP_TITLE), Paragraph("저장 결과", STEP_TITLE)],
                    [Paragraph("naver.com", BODY), Paragraph("https://naver.com", BODY)],
                    [Paragraph("https://example.com/docs", BODY), Paragraph("그대로 저장", BODY)],
                    [Paragraph("file:///C:/... 또는 javascript:...", BODY), Paragraph("안전을 위해 등록 거부", BODY)],
                ],
                colWidths=[CONTENT_WIDTH * 0.52, CONTENT_WIDTH * 0.48],
                style=TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                        ("BACKGROUND", (0, 0), (-1, 0), BLUE_SOFT),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                        ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ]
                ),
            ),
            Spacer(1, 5 * mm),
            note("주소를 잘못 입력했나요?", "설정 목록에서 해당 웹 링크의 '수정'을 누르면 주소와 표시 이름을 다시 입력할 수 있습니다.", tone="blue"),
            PageBreak(),
        ]
    )

    # Shortcuts and settings
    story.extend(page_header("5. 단축키와 화면 설정", "내 방식대로 바꾸기"))
    shortcut_table = Table(
        [
            [keycap("Ctrl + Space"), Paragraph("빠른 실행 패널 열기", STEP_TITLE)],
            [keycap("Ctrl + Shift + Space"), Paragraph("현재 탐색기의 선택 파일/폴더 빠르게 등록", STEP_TITLE)],
        ],
        colWidths=[58 * mm, CONTENT_WIDTH - 58 * mm],
    )
    shortcut_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm)]))
    story.extend(
        [
            shortcut_table,
            Paragraph("환경 설정에서 바꿀 수 있는 항목", H2),
            Paragraph(
                "- 화면 스타일: 시스템 / 밝게 / 어둡게<br/>"
                "- 패널 열 수: 한 줄에 2개에서 5개<br/>"
                "- Windows 시작 시 자동 실행<br/>"
                "- 새 버전 자동 확인 (기본값 꺼짐)<br/>"
                "- 패널 단축키와 탐색기 빠른 등록 단축키",
                BODY,
            ),
            Spacer(1, 4 * mm),
            note("단축키가 작동하지 않으면", "다른 프로그램이나 한글 입력기가 같은 키를 사용 중일 수 있습니다. 환경 설정에서 Ctrl + Alt + Space처럼 다른 조합으로 변경하세요. 변경에 실패하면 기존 단축키가 안전하게 유지됩니다.", tone="amber"),
            Spacer(1, 5 * mm),
            note("탐색기 빠른 등록", "파일 탐색기를 먼저 활성화하세요. 선택한 항목이 있으면 그 항목을, 선택한 항목이 없으면 현재 폴더를 등록합니다. 여러 항목을 한 번에 선택한 경우에는 등록하지 않습니다.", tone="green"),
            PageBreak(),
        ]
    )

    # Troubleshooting
    story.extend(page_header("6. 종료와 문제 해결", "알아두면 편리합니다"))
    rows = [
        ("앱을 완전히 종료하려면", "화면 오른쪽 아래 시스템 트레이에서 QuickAccess 아이콘을 우클릭하고 '종료'를 선택합니다."),
        ("설정 창의 X를 눌렀는데 계속 실행돼요", "정상 동작입니다. QuickAccess는 트레이에 상주해야 단축키를 받을 수 있습니다."),
        ("패널이 두 번 뜨지 않아요", "QuickAccess는 중복 실행을 막습니다. 이미 실행 중이면 새 프로세스는 조용히 종료됩니다."),
        ("파일 카드가 빨갛게 보여요", "파일이 이동 또는 삭제되었거나 네트워크 드라이브가 연결되지 않은 상태입니다. 카드를 눌러 새 경로를 지정하세요."),
        ("웹 링크가 열리지 않아요", "인터넷 연결과 Windows 기본 브라우저 설정을 확인한 뒤 설정에서 주소를 다시 확인하세요."),
        ("설정을 되돌리고 싶어요", "%APPDATA%\\QuickAccess\\items.bak.json에 직전 설정 백업이 한 개 보관됩니다."),
        ("로그가 필요해요", "%LOCALAPPDATA%\\QuickAccess\\logs\\quickaccess.log에서 오류 기록을 확인할 수 있습니다."),
    ]
    troubleshooting = Table(
        [[Paragraph(question, STEP_TITLE), Paragraph(answer, BODY)] for question, answer in rows],
        colWidths=[52 * mm, CONTENT_WIDTH - 52 * mm],
    )
    troubleshooting.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("BACKGROUND", (0, 0), (0, -1), SURFACE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.extend(
        [
            troubleshooting,
            Spacer(1, 6 * mm),
            note("최신 버전과 도움말", "GitHub: https://github.com/Daksle-number-13/quickaccess-launcher", tone="blue"),
        ]
    )
    return story


def build() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frame = Frame(
        MARGIN_X,
        MARGIN_BOTTOM,
        CONTENT_WIDTH,
        PAGE_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    document = BaseDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=MARGIN_TOP,
        bottomMargin=MARGIN_BOTTOM,
        title="QuickAccess 쉬운 사용 설명서",
        author="Daksle",
        subject="QuickAccess v1.2.4 Korean user manual",
        creator="QuickAccess project",
    )
    document.addPageTemplates(PageTemplate(id="manual", frames=[frame], onPage=draw_page))
    document.build(build_story())


if __name__ == "__main__":
    build()

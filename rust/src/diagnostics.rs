//! Privacy-conscious diagnostics that can be copied from the settings UI.

use crate::model::{ItemType, LauncherConfig};

pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const APP_AUTHOR: &str = "Daksle";

/// A stable report without launcher target paths or usernames.
pub fn render(config: &LauncherConfig, startup_state: Option<&str>, operating_system: &str) -> String {
    let count = |kind| config.items.iter().filter(|item| item.item_type == kind).count();
    let on_off = |flag: bool| if flag { "켬" } else { "끔" };
    let build = if cfg!(debug_assertions) { "개발 빌드" } else { "배포 EXE" };
    [
        "QuickAccess 진단 정보".to_string(),
        format!("버전: {APP_VERSION}"),
        format!("만든 사람: {APP_AUTHOR}"),
        format!("운영체제: {operating_system} (x64 / 64-bit)"),
        format!("실행 환경: {build}"),
        format!(
            "바로가기: {}개 (폴더 {}, 파일 {}, 링크 {})",
            config.items.len(),
            count(ItemType::Folder),
            count(ItemType::File),
            count(ItemType::Url),
        ),
        format!("화면: {}, {}열", config.appearance_mode.as_str(), config.columns),
        format!("패널 단축키: {}", config.hotkey),
        format!("빠른 등록 단축키: {}", config.quick_add_hotkey),
        format!(
            "자동 실행: 설정 {}, 실제 상태 {}",
            on_off(config.run_on_startup),
            startup_state.unwrap_or("확인 안 됨"),
        ),
        format!("업데이트 확인: {}", on_off(config.check_updates)),
        "개인 파일 경로와 등록된 URL은 포함하지 않았습니다.".to_string(),
    ]
    .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn omits_paths_and_urls() {
        let mut config = LauncherConfig::empty();
        config.add_item(r"C:\Users\secret\a.txt", "비밀이름", Some(ItemType::File), None).unwrap();
        config.add_item("https://secret.example", "링크", Some(ItemType::Url), None).unwrap();
        config.add_item(r"C:\Users\secret", "폴더", Some(ItemType::Folder), None).unwrap();
        let report = render(&config, Some("correct"), "Windows 10.0.26100");
        assert!(!report.contains("secret"));
        assert!(!report.contains("비밀이름"));
        assert!(report.contains("바로가기: 3개 (폴더 1, 파일 1, 링크 1)"));
        assert!(report.contains("자동 실행: 설정 켬, 실제 상태 correct"));
        assert!(report.contains("업데이트 확인: 끔"));
    }

    #[test]
    fn marks_an_unknown_startup_state() {
        let report = render(&LauncherConfig::empty(), None, "Windows");
        assert!(report.contains("실제 상태 확인 안 됨"));
    }
}

//! Resolve a quick-add target from the foreground Explorer window via COM.

use windows::core::{Interface, BSTR};
use windows::Win32::Foundation::VARIANT_TRUE;
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, IDispatch, CLSCTX_ALL,
    COINIT_APARTMENTTHREADED,
};
use windows::Win32::System::Variant::VARIANT;
use windows::Win32::UI::Shell::{
    Folder2, FolderItem, IShellFolderViewDual, IShellWindows, IWebBrowserApp, ShellWindows,
};

use crate::model::ItemType;
use crate::pyish::{display_name, is_abs, normpath, path_identity, strip};

pub const MAX_SELECTED_ITEMS: i32 = 50;

const NO_EXPLORER: &str = "현재 열린 탐색기 창이 없습니다";
const TOO_MANY: &str = "한 번에 최대 50개까지 등록할 수 있습니다";
const NOT_FILESYSTEM: &str = "현재 탐색기 위치를 파일 경로로 변환할 수 없습니다";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExplorerTarget {
    pub path: String,
    pub suggested_name: String,
    pub item_type: ItemType,
}

/// `None` for virtual locations such as Quick Access or This PC.
pub fn filesystem_path(value: &str) -> Option<String> {
    let text = strip(value);
    if text.is_empty() || text.starts_with("::") || text.to_lowercase().starts_with("shell:") {
        return None;
    }
    is_abs(text).then(|| normpath(text))
}

fn target_for(path: String, item_type: ItemType) -> ExplorerTarget {
    ExplorerTarget { suggested_name: display_name(&path), path, item_type }
}

struct ComApartment;

impl ComApartment {
    fn enter() -> windows::core::Result<Self> {
        // SAFETY: paired with CoUninitialize in `Drop` on this same thread.
        unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok()? };
        Ok(Self)
    }
}

impl Drop for ComApartment {
    fn drop(&mut self) {
        // SAFETY: balances the successful CoInitializeEx in `enter`.
        unsafe { CoUninitialize() };
    }
}

fn find_window(hwnd: isize) -> windows::core::Result<Option<IWebBrowserApp>> {
    // SAFETY: standard COM activation and method calls on live interfaces.
    unsafe {
        let shell: IShellWindows = CoCreateInstance(&ShellWindows, None, CLSCTX_ALL)?;
        for index in 0..shell.Count()? {
            let Ok(dispatch): windows::core::Result<IDispatch> = shell.Item(&VARIANT::from(index)) else {
                continue;
            };
            let Ok(browser) = dispatch.cast::<IWebBrowserApp>() else {
                continue;
            };
            if browser.HWND().is_ok_and(|handle| handle.0 as isize == hwnd) {
                return Ok(Some(browser));
            }
        }
    }
    Ok(None)
}

fn selected_item_target(item: &FolderItem) -> Option<ExplorerTarget> {
    // SAFETY: method calls on a live COM interface.
    unsafe {
        let path: BSTR = item.Path().ok()?;
        let path = filesystem_path(&path.to_string())?;
        // A shell extension that cannot report IsFolder is treated as a file
        // rather than synchronously probing a possibly slow path.
        let is_folder = item.IsFolder().map(|flag| flag == VARIANT_TRUE).unwrap_or(false);
        Some(target_for(path, if is_folder { ItemType::Folder } else { ItemType::File }))
    }
}

fn targets_from(view: &IShellFolderViewDual) -> windows::core::Result<Result<Vec<ExplorerTarget>, &'static str>> {
    // SAFETY: method calls on live COM interfaces.
    unsafe {
        let selected = view.SelectedItems()?;
        let count = selected.Count()?;
        if count > 0 {
            if count > MAX_SELECTED_ITEMS {
                return Ok(Err(TOO_MANY));
            }
            let mut targets: Vec<ExplorerTarget> = Vec::new();
            let mut seen = std::collections::HashSet::new();
            for index in 0..count {
                let item = selected.Item(&VARIANT::from(index))?;
                // Multi-selection is atomic: one virtual item rejects the whole
                // selection instead of silently adding a surprising subset.
                let Some(target) = selected_item_target(&item) else {
                    return Ok(Err(NOT_FILESYSTEM));
                };
                if seen.insert(path_identity(&target.path)) {
                    targets.push(target);
                }
            }
            return Ok(Ok(targets));
        }
        let folder: Folder2 = view.Folder()?.cast()?;
        let path: BSTR = folder.Self_()?.Path()?;
        Ok(match filesystem_path(&path.to_string()) {
            Some(path) => Ok(vec![target_for(path, ItemType::Folder)]),
            None => Err(NOT_FILESYSTEM),
        })
    }
}

/// Selected items, or the current folder, of the window with handle `hwnd`.
/// Never panics; COM and window races surface as a user-facing message.
pub fn resolve_target(hwnd: isize) -> Result<Vec<ExplorerTarget>, String> {
    if hwnd == 0 {
        return Err(NO_EXPLORER.to_string());
    }
    let run = || -> windows::core::Result<Result<Vec<ExplorerTarget>, &'static str>> {
        let _apartment = ComApartment::enter()?;
        let Some(browser) = find_window(hwnd)? else {
            return Ok(Err(NO_EXPLORER));
        };
        // SAFETY: method call on a live COM interface.
        let document: IDispatch = unsafe { browser.Document()? };
        let view: IShellFolderViewDual = document.cast()?;
        targets_from(&view)
    };
    match run() {
        Ok(Ok(targets)) if !targets.is_empty() => Ok(targets),
        Ok(Ok(_)) => Err(NOT_FILESYSTEM.to_string()),
        Ok(Err(message)) => Err(message.to_string()),
        Err(error) => {
            crate::log_warn!("Explorer COM lookup failed: {error}");
            Err(NO_EXPLORER.to_string())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_virtual_locations() {
        assert_eq!(filesystem_path("::{20D04FE0-3AEA-1069-A2D8-08002B30309D}"), None);
        assert_eq!(filesystem_path("shell:Downloads"), None);
        assert_eq!(filesystem_path("  "), None);
        assert_eq!(filesystem_path("relative\\path"), None);
    }

    #[test]
    fn normalizes_real_paths() {
        assert_eq!(filesystem_path(r" C:/Users/me/ ").as_deref(), Some(r"C:\Users\me"));
        assert_eq!(filesystem_path(r"\\srv\share\a").as_deref(), Some(r"\\srv\share\a"));
    }

    #[test]
    fn suggests_the_final_component() {
        let target = target_for(r"C:\문서\보고서.xlsx".to_string(), ItemType::File);
        assert_eq!(target.suggested_name, "보고서.xlsx");
    }

    #[test]
    fn no_window_is_a_message_not_a_crash() {
        assert_eq!(resolve_target(0), Err(NO_EXPLORER.to_string()));
        assert_eq!(resolve_target(1), Err(NO_EXPLORER.to_string()));
    }
}

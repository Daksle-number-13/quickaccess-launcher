//! Native Windows file and folder pickers.

use windows::core::{Interface, PCWSTR, PWSTR};
use windows::Win32::Foundation::HWND;
use windows::Win32::System::Com::{CoCreateInstance, CoTaskMemFree, CLSCTX_INPROC_SERVER};
use windows::Win32::UI::Shell::Common::COMDLG_FILTERSPEC;
use windows::Win32::UI::Shell::{
    FileOpenDialog, FileSaveDialog, IFileDialog, IFileOpenDialog, IFileSaveDialog, FOS_FORCEFILESYSTEM,
    FOS_OVERWRITEPROMPT, FOS_PICKFOLDERS, SIGDN_FILESYSPATH,
};

use crate::pyish::wide;

const JSON_FILTER_NAME: &str = "JSON 설정 파일";
const ALL_FILTER_NAME: &str = "모든 파일";

fn run(dialog: &IFileDialog, owner: Option<HWND>, title: &str) -> Option<String> {
    let title = wide(title);
    // SAFETY: the dialog is a live COM object; the title outlives the modal
    // call, and the result string is copied before being freed.
    unsafe {
        dialog.SetTitle(PCWSTR(title.as_ptr())).ok()?;
        dialog.Show(owner).ok()?;
        let item = dialog.GetResult().ok()?;
        let path: PWSTR = item.GetDisplayName(SIGDN_FILESYSPATH).ok()?;
        let text = path.to_string().ok();
        CoTaskMemFree(Some(path.0 as *const _));
        text.filter(|value| !value.is_empty())
    }
}

fn set_json_filters(dialog: &IFileDialog) -> Option<()> {
    let json_name = wide(JSON_FILTER_NAME);
    let json_spec = wide("*.json");
    let all_name = wide(ALL_FILTER_NAME);
    let all_spec = wide("*.*");
    let filters = [
        COMDLG_FILTERSPEC {
            pszName: PCWSTR(json_name.as_ptr()),
            pszSpec: PCWSTR(json_spec.as_ptr()),
        },
        COMDLG_FILTERSPEC {
            pszName: PCWSTR(all_name.as_ptr()),
            pszSpec: PCWSTR(all_spec.as_ptr()),
        },
    ];
    // SAFETY: the dialog copies the filter strings during this call.
    unsafe { dialog.SetFileTypes(&filters).ok() }
}

fn open_dialog(extra: windows::Win32::UI::Shell::FILEOPENDIALOGOPTIONS) -> Option<IFileDialog> {
    // SAFETY: standard in-process COM activation on the STA UI thread.
    unsafe {
        let dialog: IFileOpenDialog =
            CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER).ok()?;
        let options = dialog.GetOptions().ok()?;
        dialog.SetOptions(options | FOS_FORCEFILESYSTEM | extra).ok()?;
        dialog.cast().ok()
    }
}

pub fn pick_folder(owner: Option<HWND>, title: &str) -> Option<String> {
    run(&open_dialog(FOS_PICKFOLDERS)?, owner, title)
}

pub fn pick_file(owner: Option<HWND>, title: &str) -> Option<String> {
    let none = windows::Win32::UI::Shell::FILEOPENDIALOGOPTIONS(0);
    run(&open_dialog(none)?, owner, title)
}

pub fn pick_json_to_open(owner: Option<HWND>, title: &str) -> Option<String> {
    let none = windows::Win32::UI::Shell::FILEOPENDIALOGOPTIONS(0);
    let dialog = open_dialog(none)?;
    set_json_filters(&dialog)?;
    run(&dialog, owner, title)
}

pub fn pick_json_to_save(owner: Option<HWND>, title: &str, initial_name: &str) -> Option<String> {
    let name = wide(initial_name);
    let extension = wide("json");
    // SAFETY: standard COM activation; strings are copied during the calls.
    unsafe {
        let dialog: IFileSaveDialog =
            CoCreateInstance(&FileSaveDialog, None, CLSCTX_INPROC_SERVER).ok()?;
        let options = dialog.GetOptions().ok()?;
        dialog
            .SetOptions(options | FOS_FORCEFILESYSTEM | FOS_OVERWRITEPROMPT)
            .ok()?;
        dialog.SetFileName(PCWSTR(name.as_ptr())).ok()?;
        dialog.SetDefaultExtension(PCWSTR(extension.as_ptr())).ok()?;
        let dialog: IFileDialog = dialog.cast().ok()?;
        set_json_filters(&dialog)?;
        run(&dialog, owner, title)
    }
}

//! HICON pixel extraction using only stable Win32 shell and GDI APIs.

use std::ffi::c_void;

use windows::core::PCWSTR;
use windows::Win32::Graphics::Gdi::{
    CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, SelectObject, BITMAPINFO,
    BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS, HGDIOBJ,
};
use windows::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL, FILE_FLAGS_AND_ATTRIBUTES,
};
use windows::Win32::UI::Shell::{
    SHGetFileInfoW, SHFILEINFOW, SHGFI_FLAGS, SHGFI_ICON, SHGFI_LARGEICON, SHGFI_USEFILEATTRIBUTES,
};
use windows::Win32::UI::WindowsAndMessaging::{DestroyIcon, DrawIconEx, DI_NORMAL};

use crate::icons::{has_visible_alpha, premultiply, recover_legacy_alpha, IconApi, IconImage};
use crate::pyish::{extension, strip, wide};

pub struct NativeIconApi;

fn extract(
    path: &str,
    size: u32,
    attributes: FILE_FLAGS_AND_ATTRIBUTES,
    flags: SHGFI_FLAGS,
) -> Option<IconImage> {
    let wide_path = wide(path);
    let mut info = SHFILEINFOW::default();
    // SAFETY: the path is NUL-terminated and `info` is the declared size. All
    // GDI objects created below are released before returning, and the pixel
    // pointer is only read while its DIB section is alive.
    unsafe {
        let result = SHGetFileInfoW(
            PCWSTR(wide_path.as_ptr()),
            attributes,
            Some(&mut info),
            std::mem::size_of::<SHFILEINFOW>() as u32,
            SHGFI_ICON | SHGFI_LARGEICON | flags,
        );
        if result == 0 || info.hIcon.is_invalid() {
            return None;
        }

        let memory_dc = CreateCompatibleDC(None);
        let mut image = None;
        if !memory_dc.is_invalid() {
            let header = BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: size as i32,
                biHeight: -(size as i32),
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                biSizeImage: size * size * 4,
                ..Default::default()
            };
            let bitmap_info = BITMAPINFO { bmiHeader: header, ..Default::default() };
            let mut bits: *mut c_void = std::ptr::null_mut();
            if let Ok(bitmap) =
                CreateDIBSection(Some(memory_dc), &bitmap_info, DIB_RGB_COLORS, &mut bits, None, 0)
            {
                if !bits.is_null() {
                    let previous = SelectObject(memory_dc, HGDIOBJ(bitmap.0));
                    let length = (size * size * 4) as usize;
                    let read = |fill: u8| -> Option<Vec<u8>> {
                        std::ptr::write_bytes(bits.cast::<u8>(), fill, length);
                        DrawIconEx(
                            memory_dc,
                            0,
                            0,
                            info.hIcon,
                            size as i32,
                            size as i32,
                            0,
                            None,
                            DI_NORMAL,
                        )
                        .ok()?;
                        Some(std::slice::from_raw_parts(bits.cast::<u8>(), length).to_vec())
                    };
                    if let Some(pixels) = read(0) {
                        let direct = IconImage { width: size, height: size, bgra: pixels };
                        if has_visible_alpha(&direct) {
                            // Alpha blending onto zeros already premultiplies.
                            image = Some(direct);
                        } else if let Some(white) = read(0xFF) {
                            // Legacy icons expose transparency through an AND
                            // mask; a second render over white recovers it.
                            let mut bgra = recover_legacy_alpha(&direct.bgra, &white);
                            premultiply(&mut bgra);
                            let recovered = IconImage { width: size, height: size, bgra };
                            if has_visible_alpha(&recovered) {
                                image = Some(recovered);
                            }
                        }
                    }
                    SelectObject(memory_dc, previous);
                }
                let _ = DeleteObject(HGDIOBJ(bitmap.0));
            }
            let _ = DeleteDC(memory_dc);
        }
        let _ = DestroyIcon(info.hIcon);
        image
    }
}

impl IconApi for NativeIconApi {
    fn extract(&self, path: &str, size: u32) -> Option<IconImage> {
        extract(path, size, FILE_FLAGS_AND_ATTRIBUTES(0), SHGFI_FLAGS(0))
    }

    /// A registered type icon, read without touching the target path.
    fn extract_generic(&self, path: &str, size: u32, is_directory: bool) -> Option<IconImage> {
        let lowered = strip(path).to_lowercase();
        let suffix = if lowered.starts_with("http://") || lowered.starts_with("https://") {
            ".url"
        } else {
            extension(path)
        };
        let lookup = if is_directory {
            "QuickAccessFolder".to_string()
        } else if suffix.is_empty() {
            "QuickAccess.file".to_string()
        } else {
            format!("QuickAccess{suffix}")
        };
        let attributes = if is_directory { FILE_ATTRIBUTE_DIRECTORY } else { FILE_ATTRIBUTE_NORMAL };
        extract(&lookup, size, attributes, SHGFI_USEFILEATTRIBUTES)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_real_shell_icons() {
        crate::platform::com_init_thread();
        let api = NativeIconApi;
        let folder = api.extract_generic("x", 32, true).expect("folder icon");
        assert!(has_visible_alpha(&folder));
        assert_eq!((folder.width, folder.height), (32, 32));
        assert!(api.extract_generic(r"C:\a.txt", 32, false).is_some());
        let windir = std::env::var("WINDIR").unwrap();
        assert!(api.extract(&format!(r"{windir}\explorer.exe"), 32).is_some());
    }
}

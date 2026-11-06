//! Render a window's frame offscreen to PNG, for visual review.
//!
//! Frames are drawn by the normal paint code into a bitmap rather than copied
//! from the screen, so a capture never contains other apps or personal windows.
//! Native edit controls paint outside Direct2D, so their text (or cue banner)
//! is drawn onto the frame afterwards at the controls' own positions.

use std::path::Path;

use windows::core::{GUID, PCWSTR};
use windows::Win32::Foundation::{GENERIC_WRITE, HWND, LPARAM, POINT, RECT, WPARAM};
use windows::Win32::Graphics::Direct2D::Common::{
    D2D1_ALPHA_MODE_PREMULTIPLIED, D2D1_COLOR_F, D2D1_PIXEL_FORMAT, D2D_RECT_F,
};
use windows::Win32::Graphics::Direct2D::{
    D2D1_DRAW_TEXT_OPTIONS_CLIP, D2D1_FEATURE_LEVEL_DEFAULT, D2D1_RENDER_TARGET_PROPERTIES,
    D2D1_RENDER_TARGET_TYPE_SOFTWARE, D2D1_RENDER_TARGET_USAGE_NONE,
};
use windows::Win32::Graphics::DirectWrite::{
    DWRITE_FONT_STRETCH_NORMAL, DWRITE_FONT_STYLE_NORMAL, DWRITE_FONT_WEIGHT_NORMAL,
    DWRITE_MEASURING_MODE_NATURAL, DWRITE_WORD_WRAPPING_NO_WRAP,
};
use windows::Win32::Graphics::Dxgi::Common::DXGI_FORMAT_B8G8R8A8_UNORM;
use windows::Win32::Graphics::Gdi::{
    GetObjectW, InvalidateRect, MapWindowPoints, UpdateWindow, HFONT, HGDIOBJ, LOGFONTW,
};
use windows::Win32::Graphics::Imaging::{
    GUID_ContainerFormatPng, GUID_WICPixelFormat32bppBGRA, IWICBitmap, IWICBitmapFrameEncode,
    WICBitmapEncoderNoCache,
};
use windows::Win32::System::Com::StructuredStorage::IPropertyBag2;
use windows::Win32::UI::Controls::{EM_GETCUEBANNER, EM_GETRECT};
use windows::Win32::UI::WindowsAndMessaging::{
    EnumChildWindows, GetClassNameW, GetWindowTextLengthW, GetWindowTextW, IsWindowVisible,
    SendMessageW, WM_GETFONT,
};
use windows::core::BOOL;

use crate::pyish::wide;
use crate::ui::gfx::{capture_frame, gfx};

pub fn save_window(hwnd: HWND, path: &Path) -> Result<(), String> {
    let text = |error: windows::core::Error| error.to_string();
    // SAFETY: forcing a synchronous repaint of a window owned by this thread.
    let bitmap = capture_frame(|| unsafe {
        let _ = InvalidateRect(Some(hwnd), None, false);
        let _ = UpdateWindow(hwnd);
    })
    .ok_or("the window produced no frame")?;
    draw_edit_text(hwnd, &bitmap).map_err(text)?;

    let graphics = gfx().map_err(text)?;
    let target = wide(&path.to_string_lossy());
    // SAFETY: standard WIC encoding on live interfaces; `target` outlives use.
    unsafe {
        let stream = graphics.wic.CreateStream().map_err(text)?;
        stream
            .InitializeFromFilename(PCWSTR(target.as_ptr()), GENERIC_WRITE.0)
            .map_err(text)?;
        let encoder = graphics
            .wic
            .CreateEncoder(&GUID_ContainerFormatPng, std::ptr::null())
            .map_err(text)?;
        encoder.Initialize(&stream, WICBitmapEncoderNoCache).map_err(text)?;
        let mut frame: Option<IWICBitmapFrameEncode> = None;
        let mut options: Option<IPropertyBag2> = None;
        encoder.CreateNewFrame(&mut frame, &mut options).map_err(text)?;
        let frame = frame.ok_or("no WIC frame")?;
        frame.Initialize(options.as_ref()).map_err(text)?;
        let mut format: GUID = GUID_WICPixelFormat32bppBGRA;
        frame.SetPixelFormat(&mut format).map_err(text)?;
        frame.WriteSource(&bitmap, std::ptr::null()).map_err(text)?;
        frame.Commit().map_err(text)?;
        encoder.Commit().map_err(text)?;
    }
    Ok(())
}

struct EditSnapshot {
    /// Formatting rectangle in the parent's client pixels.
    rect: RECT,
    text: String,
    cue: bool,
    font_pixels: f32,
}

fn visible_edits(parent: HWND) -> Vec<EditSnapshot> {
    unsafe extern "system" fn collect(child: HWND, data: LPARAM) -> BOOL {
        // SAFETY: `data` is the Vec passed below, alive for the enumeration.
        let found = unsafe { &mut *(data.0 as *mut Vec<HWND>) };
        found.push(child);
        BOOL(1)
    }
    let mut children: Vec<HWND> = Vec::new();
    // SAFETY: the callback only pushes into `children`, which outlives the call.
    unsafe {
        let _ = EnumChildWindows(Some(parent), Some(collect), LPARAM(&mut children as *mut _ as isize));
    }
    children
        .into_iter()
        .filter_map(|child| {
            let mut class = [0u16; 16];
            // SAFETY: the buffer length is passed with the buffer.
            let length = unsafe { GetClassNameW(child, &mut class) } as usize;
            if !String::from_utf16_lossy(&class[..length]).eq_ignore_ascii_case("edit") {
                return None;
            }
            // SAFETY: plain queries on a child window of this thread.
            unsafe {
                if !IsWindowVisible(child).as_bool() {
                    return None;
                }
                let mut rect = RECT::default();
                SendMessageW(child, EM_GETRECT, None, Some(LPARAM(&mut rect as *mut _ as isize)));
                let mut corners = [POINT { x: rect.left, y: rect.top }, POINT { x: rect.right, y: rect.bottom }];
                MapWindowPoints(Some(child), Some(parent), &mut corners);
                let rect = RECT { left: corners[0].x, top: corners[0].y, right: corners[1].x, bottom: corners[1].y };

                let mut buffer = vec![0u16; GetWindowTextLengthW(child).max(0) as usize + 1];
                let copied = GetWindowTextW(child, &mut buffer).max(0) as usize;
                let mut text = String::from_utf16_lossy(&buffer[..copied]);
                let mut cue = false;
                if text.is_empty() {
                    let mut banner = [0u16; 256];
                    let ok = SendMessageW(
                        child,
                        EM_GETCUEBANNER,
                        Some(WPARAM(banner.as_mut_ptr() as usize)),
                        Some(LPARAM(banner.len() as isize)),
                    );
                    if ok.0 != 0 {
                        let end = banner.iter().position(|unit| *unit == 0).unwrap_or(banner.len());
                        text = String::from_utf16_lossy(&banner[..end]);
                        cue = true;
                    }
                }

                let font = HFONT(SendMessageW(child, WM_GETFONT, None, None).0 as *mut _);
                let mut logical = LOGFONTW::default();
                let size = std::mem::size_of::<LOGFONTW>() as i32;
                let font_pixels = if !font.is_invalid()
                    && GetObjectW(HGDIOBJ(font.0), size, Some(&mut logical as *mut _ as *mut _)) == size
                {
                    logical.lfHeight.unsigned_abs() as f32
                } else {
                    (rect.bottom - rect.top) as f32 * 0.75
                };
                (!text.is_empty()).then_some(EditSnapshot { rect, text, cue, font_pixels })
            }
        })
        .collect()
}

fn draw_edit_text(parent: HWND, bitmap: &IWICBitmap) -> windows::core::Result<()> {
    let edits = visible_edits(parent);
    if edits.is_empty() {
        return Ok(());
    }
    let graphics = gfx()?;
    let color = crate::ui::kit::edit_text_colorref();
    let channel = |shift: u32| ((color >> shift) & 0xFF) as f32 / 255.0;
    let family = wide("Malgun Gothic");
    let locale = wide("ko-KR");
    // 96 DPI makes one DIP one pixel, matching the edit rectangles.
    let properties = D2D1_RENDER_TARGET_PROPERTIES {
        r#type: D2D1_RENDER_TARGET_TYPE_SOFTWARE,
        pixelFormat: D2D1_PIXEL_FORMAT {
            format: DXGI_FORMAT_B8G8R8A8_UNORM,
            alphaMode: D2D1_ALPHA_MODE_PREMULTIPLIED,
        },
        dpiX: 96.0,
        dpiY: 96.0,
        usage: D2D1_RENDER_TARGET_USAGE_NONE,
        minLevel: D2D1_FEATURE_LEVEL_DEFAULT,
    };
    // SAFETY: Direct2D/DirectWrite calls on live interfaces; Begin/EndDraw
    // are paired and every string outlives its call.
    unsafe {
        let target = graphics.d2d.CreateWicBitmapRenderTarget(bitmap, &properties)?;
        target.BeginDraw();
        for edit in &edits {
            let alpha = if edit.cue { 0.45 } else { 1.0 };
            let brush = target.CreateSolidColorBrush(
                &D2D1_COLOR_F { r: channel(0), g: channel(8), b: channel(16), a: alpha },
                None,
            )?;
            let format = graphics.dwrite.CreateTextFormat(
                PCWSTR(family.as_ptr()),
                None,
                DWRITE_FONT_WEIGHT_NORMAL,
                DWRITE_FONT_STYLE_NORMAL,
                DWRITE_FONT_STRETCH_NORMAL,
                edit.font_pixels,
                PCWSTR(locale.as_ptr()),
            )?;
            format.SetWordWrapping(DWRITE_WORD_WRAPPING_NO_WRAP)?;
            let units: Vec<u16> = edit.text.encode_utf16().collect();
            let rect = D2D_RECT_F {
                left: edit.rect.left as f32,
                top: edit.rect.top as f32,
                right: edit.rect.right as f32,
                bottom: edit.rect.bottom as f32,
            };
            target.DrawText(
                &units,
                &format,
                &rect,
                &brush,
                D2D1_DRAW_TEXT_OPTIONS_CLIP,
                DWRITE_MEASURING_MODE_NATURAL,
            );
        }
        target.EndDraw(None, None)?;
    }
    Ok(())
}

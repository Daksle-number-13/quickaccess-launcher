//! Render a window's frame offscreen to PNG, for visual review.
//!
//! Frames are drawn by the normal paint code into a bitmap rather than copied
//! from the screen, so a capture never contains other apps or personal windows.
//! Native edit controls are not part of the Direct2D frame.

use std::path::Path;

use windows::core::{GUID, PCWSTR};
use windows::Win32::Foundation::{GENERIC_WRITE, HWND};
use windows::Win32::Graphics::Gdi::{InvalidateRect, UpdateWindow};
use windows::Win32::Graphics::Imaging::{
    GUID_ContainerFormatPng, GUID_WICPixelFormat32bppBGRA, IWICBitmapFrameEncode, WICBitmapEncoderNoCache,
};
use windows::Win32::System::Com::StructuredStorage::IPropertyBag2;

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

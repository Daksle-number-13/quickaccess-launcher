//! Direct2D and DirectWrite drawing shared by every QuickAccess window.

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::Arc;

use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{HWND, RECT};
use windows::Win32::Graphics::Direct2D::Common::{
    D2D1_ALPHA_MODE_PREMULTIPLIED, D2D1_COLOR_F, D2D1_PIXEL_FORMAT, D2D_RECT_F,
    D2D_SIZE_U,
};
use windows::Win32::Graphics::Direct2D::{
    D2D1CreateFactory, ID2D1Bitmap, ID2D1Factory, ID2D1HwndRenderTarget, ID2D1RenderTarget,
    ID2D1SolidColorBrush, D2D1_ANTIALIAS_MODE_ALIASED, D2D1_BITMAP_INTERPOLATION_MODE_LINEAR,
    D2D1_BITMAP_PROPERTIES, D2D1_DRAW_TEXT_OPTIONS_CLIP, D2D1_FACTORY_TYPE_SINGLE_THREADED,
    D2D1_HWND_RENDER_TARGET_PROPERTIES, D2D1_PRESENT_OPTIONS_IMMEDIATELY, D2D1_ROUNDED_RECT,
    D2D1_RENDER_TARGET_PROPERTIES, D2D1_RENDER_TARGET_TYPE_SOFTWARE,
};
use windows::Win32::Graphics::DirectWrite::{
    DWriteCreateFactory, IDWriteFactory, IDWriteTextFormat, DWRITE_FACTORY_TYPE_SHARED,
    DWRITE_FONT_STRETCH_NORMAL, DWRITE_FONT_STYLE_NORMAL, DWRITE_FONT_WEIGHT_BOLD,
    DWRITE_FONT_WEIGHT_NORMAL, DWRITE_MEASURING_MODE_NATURAL, DWRITE_PARAGRAPH_ALIGNMENT_CENTER,
    DWRITE_PARAGRAPH_ALIGNMENT_FAR, DWRITE_PARAGRAPH_ALIGNMENT_NEAR,
    DWRITE_TEXT_ALIGNMENT_CENTER, DWRITE_TEXT_ALIGNMENT_LEADING, DWRITE_TEXT_ALIGNMENT_TRAILING,
    DWRITE_TEXT_METRICS, DWRITE_TRIMMING, DWRITE_TRIMMING_GRANULARITY_CHARACTER,
    DWRITE_WORD_WRAPPING_NO_WRAP, DWRITE_WORD_WRAPPING_WRAP,
};
use windows::Win32::Graphics::Dxgi::Common::DXGI_FORMAT_B8G8R8A8_UNORM;
use windows::Win32::Graphics::Imaging::{
    CLSID_WICImagingFactory, GUID_WICPixelFormat32bppPBGRA, IWICBitmap, IWICImagingFactory,
    WICBitmapCacheOnLoad, WICBitmapDitherTypeNone, WICBitmapInterpolationModeFant, WICBitmapPaletteTypeCustom,
    WICDecodeMetadataCacheOnLoad,
};
use windows::Win32::System::Com::{CoCreateInstance, CLSCTX_INPROC_SERVER};
use windows::Win32::Graphics::Gdi::InvalidateRect;
use windows::Win32::UI::WindowsAndMessaging::GetClientRect;

use crate::icons::IconImage;

#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct R {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
}

impl R {
    pub const fn new(x: f32, y: f32, w: f32, h: f32) -> Self {
        Self { x, y, w, h }
    }

    pub fn right(self) -> f32 {
        self.x + self.w
    }

    pub fn bottom(self) -> f32 {
        self.y + self.h
    }

    pub fn contains(self, x: f32, y: f32) -> bool {
        x >= self.x && x < self.right() && y >= self.y && y < self.bottom()
    }

    pub fn inset(self, dx: f32, dy: f32) -> Self {
        Self::new(self.x + dx, self.y + dy, (self.w - dx * 2.0).max(0.0), (self.h - dy * 2.0).max(0.0))
    }

    pub fn overlaps(self, other: R) -> bool {
        self.x < other.right() && other.x < self.right() && self.y < other.bottom() && other.y < self.bottom()
    }

    pub fn encloses(self, other: R) -> bool {
        other.x >= self.x && other.y >= self.y && other.right() <= self.right() && other.bottom() <= self.bottom()
    }

    fn d2d(self) -> D2D_RECT_F {
        D2D_RECT_F { left: self.x, top: self.y, right: self.right(), bottom: self.bottom() }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Align {
    Start,
    Center,
    End,
}

#[derive(Debug, Clone, Copy)]
pub struct TextStyle {
    pub size: f32,
    pub bold: bool,
    pub color: D2D1_COLOR_F,
    pub align: Align,
    pub valign: Align,
    pub wrap: bool,
    pub icon: bool,
}

impl TextStyle {
    pub fn new(size: f32, bold: bool, color: D2D1_COLOR_F) -> Self {
        Self { size, bold, color, align: Align::Start, valign: Align::Center, wrap: false, icon: false }
    }

    pub fn centered(mut self) -> Self {
        self.align = Align::Center;
        self
    }

    pub fn valign(mut self, valign: Align) -> Self {
        self.valign = valign;
        self
    }

    pub fn wrapped(mut self) -> Self {
        self.wrap = true;
        self.valign = Align::Start;
        self
    }

    pub fn icon(mut self) -> Self {
        self.icon = true;
        self.align = Align::Center;
        self
    }
}

#[derive(PartialEq, Eq, Hash, Clone, Copy)]
struct FormatKey {
    size_tenths: u32,
    bold: bool,
    icon: bool,
    align: Align,
    valign: Align,
    wrap: bool,
}

pub struct Gfx {
    pub d2d: ID2D1Factory,
    pub dwrite: IDWriteFactory,
    pub wic: IWICImagingFactory,
    formats: RefCell<HashMap<FormatKey, IDWriteTextFormat>>,
    icon_font: PCWSTR,
    brand: Arc<IconImage>,
}

thread_local! {
    /// `Some` while frames are diverted; the inner value is the latest frame.
    static OFFSCREEN: RefCell<Option<Option<IWICBitmap>>> = const { RefCell::new(None) };
    static GFX: RefCell<Option<Arc<Gfx>>> = const { RefCell::new(None) };
}

const BRAND_PNG: &[u8] = include_bytes!("../../../assets/quickaccess-mark.png");
const BRAND_SIZE: u32 = 96;

fn decode_brand(wic: &IWICImagingFactory) -> windows::core::Result<IconImage> {
    // SAFETY: WIC calls on live interfaces; the PNG is a static byte slice and
    // the output buffer matches the requested stride and height.
    unsafe {
        let stream = wic.CreateStream()?;
        stream.InitializeFromMemory(BRAND_PNG)?;
        let decoder =
            wic.CreateDecoderFromStream(&stream, std::ptr::null(), WICDecodeMetadataCacheOnLoad)?;
        let frame = decoder.GetFrame(0)?;
        let scaler = wic.CreateBitmapScaler()?;
        scaler.Initialize(&frame, BRAND_SIZE, BRAND_SIZE, WICBitmapInterpolationModeFant)?;
        let converter = wic.CreateFormatConverter()?;
        converter.Initialize(
            &scaler,
            &GUID_WICPixelFormat32bppPBGRA,
            WICBitmapDitherTypeNone,
            None,
            0.0,
            WICBitmapPaletteTypeCustom,
        )?;
        let mut bgra = vec![0_u8; (BRAND_SIZE * BRAND_SIZE * 4) as usize];
        converter.CopyPixels(std::ptr::null(), BRAND_SIZE * 4, &mut bgra)?;
        Ok(IconImage { width: BRAND_SIZE, height: BRAND_SIZE, bgra })
    }
}

fn icon_font_family() -> PCWSTR {
    let fluent = std::env::var("WINDIR")
        .map(|dir| std::path::Path::new(&dir).join("Fonts").join("SegoeIcons.ttf").is_file())
        .unwrap_or(false);
    if fluent {
        w!("Segoe Fluent Icons")
    } else {
        w!("Segoe MDL2 Assets")
    }
}

/// The per-thread graphics factories; UI windows all live on one thread.
pub fn gfx() -> windows::core::Result<Arc<Gfx>> {
    GFX.with(|slot| {
        if let Some(existing) = slot.borrow().as_ref() {
            return Ok(Arc::clone(existing));
        }
        // SAFETY: standard factory creation on the COM-initialized UI thread.
        let created = unsafe {
            let d2d: ID2D1Factory = D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, None)?;
            let dwrite: IDWriteFactory = DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED)?;
            let wic: IWICImagingFactory =
                CoCreateInstance(&CLSID_WICImagingFactory, None, CLSCTX_INPROC_SERVER)?;
            let brand = Arc::new(decode_brand(&wic)?);
            Arc::new(Gfx {
                d2d,
                dwrite,
                wic,
                formats: RefCell::new(HashMap::new()),
                icon_font: icon_font_family(),
                brand,
            })
        };
        *slot.borrow_mut() = Some(Arc::clone(&created));
        Ok(created)
    })
}

impl Gfx {
    fn format(&self, style: &TextStyle) -> windows::core::Result<IDWriteTextFormat> {
        let key = FormatKey {
            size_tenths: (style.size * 10.0).round() as u32,
            bold: style.bold,
            icon: style.icon,
            align: style.align,
            valign: style.valign,
            wrap: style.wrap,
        };
        if let Some(format) = self.formats.borrow().get(&key) {
            return Ok(format.clone());
        }
        let family = if style.icon { self.icon_font } else { w!("Malgun Gothic") };
        let weight = if style.bold { DWRITE_FONT_WEIGHT_BOLD } else { DWRITE_FONT_WEIGHT_NORMAL };
        // SAFETY: DirectWrite calls with static strings on live interfaces.
        let format = unsafe {
            let format = self.dwrite.CreateTextFormat(
                family,
                None,
                weight,
                DWRITE_FONT_STYLE_NORMAL,
                DWRITE_FONT_STRETCH_NORMAL,
                style.size,
                w!("ko-kr"),
            )?;
            format.SetTextAlignment(match style.align {
                Align::Start => DWRITE_TEXT_ALIGNMENT_LEADING,
                Align::Center => DWRITE_TEXT_ALIGNMENT_CENTER,
                Align::End => DWRITE_TEXT_ALIGNMENT_TRAILING,
            })?;
            format.SetParagraphAlignment(match style.valign {
                Align::Start => DWRITE_PARAGRAPH_ALIGNMENT_NEAR,
                Align::Center => DWRITE_PARAGRAPH_ALIGNMENT_CENTER,
                Align::End => DWRITE_PARAGRAPH_ALIGNMENT_FAR,
            })?;
            format.SetWordWrapping(if style.wrap {
                DWRITE_WORD_WRAPPING_WRAP
            } else {
                DWRITE_WORD_WRAPPING_NO_WRAP
            })?;
            if !style.wrap && !style.icon {
                let sign = self.dwrite.CreateEllipsisTrimmingSign(&format)?;
                let trimming = DWRITE_TRIMMING {
                    granularity: DWRITE_TRIMMING_GRANULARITY_CHARACTER,
                    ..Default::default()
                };
                format.SetTrimming(&trimming, &sign)?;
            }
            format
        };
        self.formats.borrow_mut().insert(key, format.clone());
        Ok(format)
    }

    /// Rendered `(width, height)` in DIPs within a maximum layout width.
    pub fn measure(&self, text: &str, size: f32, bold: bool, max_width: f32, wrap: bool) -> (f32, f32) {
        let style = TextStyle { wrap, ..TextStyle::new(size, bold, D2D1_COLOR_F::default()) };
        let units: Vec<u16> = text.encode_utf16().collect();
        let measured = (|| -> windows::core::Result<DWRITE_TEXT_METRICS> {
            let format = self.format(&style)?;
            // SAFETY: `units` outlives the call; `metrics` is a valid out-param.
            unsafe {
                let layout = self.dwrite.CreateTextLayout(&units, &format, max_width, 100_000.0)?;
                let mut metrics = DWRITE_TEXT_METRICS::default();
                layout.GetMetrics(&mut metrics)?;
                Ok(metrics)
            }
        })();
        measured
            .map(|metrics| (metrics.widthIncludingTrailingWhitespace, metrics.height))
            .unwrap_or((0.0, size * 1.4))
    }

    /// Middle-ellipsize to a rendered width rather than a character count.
    pub fn ellipsize_middle(&self, value: &str, max_width: f32, size: f32, bold: bool) -> String {
        if self.measure(value, size, bold, 100_000.0, false).0 <= max_width {
            return value.to_string();
        }
        let chars: Vec<char> = value.chars().collect();
        let (mut low, mut high) = (0_i64, chars.len() as i64);
        let mut best = "…".to_string();
        while low <= high {
            let retained = (low + high) / 2;
            let head = ((retained + 1) / 2) as usize;
            let tail = (retained / 2) as usize;
            let candidate: String = chars[..head]
                .iter()
                .chain(std::iter::once(&'…'))
                .chain(chars[chars.len() - tail..].iter())
                .collect();
            if self.measure(&candidate, size, bold, 100_000.0, false).0 <= max_width {
                best = candidate;
                low = retained + 1;
            } else {
                high = retained - 1;
            }
        }
        best
    }

    pub fn brand(&self) -> Arc<IconImage> {
        Arc::clone(&self.brand)
    }
}

/// One window's Direct2D target, recreated transparently on device loss.
pub struct Surface {
    hwnd: HWND,
    target: Option<ID2D1HwndRenderTarget>,
    brush: Option<ID2D1SolidColorBrush>,
    bitmaps: HashMap<String, ID2D1Bitmap>,
    dpi: u32,
}

fn client_size(hwnd: HWND) -> D2D_SIZE_U {
    let mut rect = RECT::default();
    // SAFETY: `rect` is a valid out-param; a failure leaves a zero size.
    unsafe {
        let _ = GetClientRect(hwnd, &mut rect);
    }
    D2D_SIZE_U {
        width: (rect.right - rect.left).max(1) as u32,
        height: (rect.bottom - rect.top).max(1) as u32,
    }
}

fn default_rt_properties(dpi: u32) -> D2D1_RENDER_TARGET_PROPERTIES {
    D2D1_RENDER_TARGET_PROPERTIES {
        // Measured: software rendering opens the 20-item popup as fast as the
        // GPU path (about 3 ms) while using 12 MB of private memory instead of
        // 43 MB, because no Direct3D driver stays loaded in a resident process.
        r#type: D2D1_RENDER_TARGET_TYPE_SOFTWARE,
        pixelFormat: D2D1_PIXEL_FORMAT {
            format: DXGI_FORMAT_B8G8R8A8_UNORM,
            alphaMode: D2D1_ALPHA_MODE_PREMULTIPLIED,
        },
        dpiX: dpi as f32,
        dpiY: dpi as f32,
        ..Default::default()
    }
}

impl Surface {
    pub fn new(hwnd: HWND, dpi: u32) -> Self {
        Self { hwnd, target: None, brush: None, bitmaps: HashMap::new(), dpi }
    }

    pub fn set_dpi(&mut self, dpi: u32) {
        if dpi != self.dpi {
            self.dpi = dpi;
            if let Some(target) = &self.target {
                // SAFETY: method call on a live render target.
                unsafe { target.SetDpi(dpi as f32, dpi as f32) };
            }
        }
    }

    /// Match the render target to the current client area.
    pub fn sync_size(&mut self) {
        if let Some(target) = &self.target {
            // SAFETY: method call on a live render target.
            if unsafe { target.Resize(&client_size(self.hwnd)) }.is_err() {
                self.release_device();
            }
        }
    }

    fn release_device(&mut self) {
        self.target = None;
        self.brush = None;
        self.bitmaps.clear();
    }

    fn ensure_target(&mut self) -> windows::core::Result<()> {
        if self.target.is_some() {
            return Ok(());
        }
        let graphics = gfx()?;
        let window = D2D1_HWND_RENDER_TARGET_PROPERTIES {
            hwnd: self.hwnd,
            pixelSize: client_size(self.hwnd),
            // Never block the hotkey path waiting for a vertical blank.
            presentOptions: D2D1_PRESENT_OPTIONS_IMMEDIATELY,
        };
        // SAFETY: both property structs are valid for the call.
        let target =
            unsafe { graphics.d2d.CreateHwndRenderTarget(&default_rt_properties(self.dpi), &window)? };
        // SAFETY: method call on the render target created above.
        let brush = unsafe { target.CreateSolidColorBrush(&D2D1_COLOR_F::default(), None)? };
        self.target = Some(target);
        self.brush = Some(brush);
        Ok(())
    }

    /// Run one frame. On device loss the window is invalidated to retry.
    pub fn draw(&mut self, paint: impl FnOnce(&mut Painter<'_>)) {
        if OFFSCREEN.with(|slot| slot.borrow().is_some()) {
            if let Err(error) = self.draw_offscreen(paint) {
                crate::log_error!("Offscreen frame failed: {error}");
            }
            return;
        }
        if let Err(error) = self.ensure_target() {
            crate::log_error!("Unable to create a render target: {error}");
            return;
        }
        let Ok(graphics) = gfx() else { return };
        let target = self.target.clone().expect("ensured above");
        let brush = self.brush.clone().expect("ensured above");
        let render_target: ID2D1RenderTarget = (*target).clone();
        // SAFETY: Begin/EndDraw are paired on a live target on the UI thread.
        let result = unsafe {
            render_target.BeginDraw();
            {
                let mut painter = Painter {
                    target: &render_target,
                    brush: &brush,
                    bitmaps: &mut self.bitmaps,
                    graphics: &graphics,
                };
                paint(&mut painter);
            }
            render_target.EndDraw(None, None)
        };
        if let Err(error) = result {
            crate::log_warn!("Direct2D frame failed: {error}");
            self.release_device();
            // SAFETY: invalidating a window owned by this thread.
            unsafe {
                let _ = InvalidateRect(Some(self.hwnd), None, false);
            }
        }
    }
}

impl Surface {
    /// Render the same frame into a WIC bitmap, independent of the desktop.
    fn draw_offscreen(&mut self, paint: impl FnOnce(&mut Painter<'_>)) -> windows::core::Result<()> {
        let graphics = gfx()?;
        let size = client_size(self.hwnd);
        // SAFETY: WIC and Direct2D calls on live interfaces; Begin/EndDraw pair.
        unsafe {
            let bitmap = graphics.wic.CreateBitmap(
                size.width,
                size.height,
                &GUID_WICPixelFormat32bppPBGRA,
                WICBitmapCacheOnLoad,
            )?;
            let target = graphics.d2d.CreateWicBitmapRenderTarget(&bitmap, &default_rt_properties(self.dpi))?;
            let brush = target.CreateSolidColorBrush(&D2D1_COLOR_F::default(), None)?;
            let mut bitmaps = HashMap::new();
            target.BeginDraw();
            {
                let mut painter = Painter {
                    target: &target,
                    brush: &brush,
                    bitmaps: &mut bitmaps,
                    graphics: &graphics,
                };
                paint(&mut painter);
            }
            target.EndDraw(None, None)?;
            OFFSCREEN.with(|slot| *slot.borrow_mut() = Some(Some(bitmap)));
        }
        Ok(())
    }
}

/// Run `paint` with frames diverted to a bitmap, returning the last frame.
pub fn capture_frame(paint: impl FnOnce()) -> Option<IWICBitmap> {
    OFFSCREEN.with(|slot| *slot.borrow_mut() = Some(None));
    paint();
    OFFSCREEN.with(|slot| slot.borrow_mut().take()).flatten()
}

pub struct Painter<'a> {
    target: &'a ID2D1RenderTarget,
    brush: &'a ID2D1SolidColorBrush,
    bitmaps: &'a mut HashMap<String, ID2D1Bitmap>,
    pub graphics: &'a Gfx,
}

impl Painter<'_> {
    fn use_color(&self, color: D2D1_COLOR_F) {
        // SAFETY: method call on a live brush.
        unsafe { self.brush.SetColor(&color) };
    }

    pub fn clear(&self, color: D2D1_COLOR_F) {
        // SAFETY: called between BeginDraw and EndDraw.
        unsafe { self.target.Clear(Some(&color)) };
    }

    pub fn fill(&self, rect: R, radius: f32, color: D2D1_COLOR_F) {
        self.use_color(color);
        let brush: &windows::Win32::Graphics::Direct2D::ID2D1Brush = self.brush;
        // SAFETY: called between BeginDraw and EndDraw.
        unsafe {
            if radius <= 0.0 {
                self.target.FillRectangle(&rect.d2d(), brush);
            } else {
                let rounded = D2D1_ROUNDED_RECT { rect: rect.d2d(), radiusX: radius, radiusY: radius };
                self.target.FillRoundedRectangle(&rounded, brush);
            }
        }
    }

    /// A stroke fully inside `rect`, so 1-DIP borders stay crisp.
    pub fn stroke(&self, rect: R, radius: f32, width: f32, color: D2D1_COLOR_F) {
        self.use_color(color);
        let half = width / 2.0;
        let inner = rect.inset(half, half);
        let rounded = D2D1_ROUNDED_RECT {
            rect: inner.d2d(),
            radiusX: (radius - half).max(0.0),
            radiusY: (radius - half).max(0.0),
        };
        let brush: &windows::Win32::Graphics::Direct2D::ID2D1Brush = self.brush;
        // SAFETY: called between BeginDraw and EndDraw.
        unsafe { self.target.DrawRoundedRectangle(&rounded, brush, width, None) };
    }

    pub fn text(&self, rect: R, text: &str, style: TextStyle) {
        let Ok(format) = self.graphics.format(&style) else {
            return;
        };
        self.use_color(style.color);
        let units: Vec<u16> = text.encode_utf16().collect();
        let brush: &windows::Win32::Graphics::Direct2D::ID2D1Brush = self.brush;
        // SAFETY: `units` outlives the call; between BeginDraw and EndDraw.
        unsafe {
            self.target.DrawText(
                &units,
                &format,
                &rect.d2d(),
                brush,
                D2D1_DRAW_TEXT_OPTIONS_CLIP,
                DWRITE_MEASURING_MODE_NATURAL,
            );
        }
    }

    pub fn clip(&self, rect: R, inner: impl FnOnce(&Self)) {
        // SAFETY: the clip is pushed and popped symmetrically within a frame.
        unsafe {
            self.target.PushAxisAlignedClip(&rect.d2d(), D2D1_ANTIALIAS_MODE_ALIASED);
            inner(self);
            self.target.PopAxisAlignedClip();
        }
    }

    /// Draw a premultiplied BGRA image, cached per device under `key`.
    pub fn image(&mut self, key: &str, image: &IconImage, rect: R) {
        if !self.bitmaps.contains_key(key) {
            let properties = D2D1_BITMAP_PROPERTIES {
                pixelFormat: D2D1_PIXEL_FORMAT {
                    format: DXGI_FORMAT_B8G8R8A8_UNORM,
                    alphaMode: D2D1_ALPHA_MODE_PREMULTIPLIED,
                },
                dpiX: 96.0,
                dpiY: 96.0,
            };
            // SAFETY: the pixel pointer and pitch describe `image.bgra`, which
            // is copied by the call.
            let created = unsafe {
                self.target.CreateBitmap(
                    D2D_SIZE_U { width: image.width, height: image.height },
                    Some(image.bgra.as_ptr().cast()),
                    image.width * 4,
                    &properties,
                )
            };
            let Ok(bitmap) = created else {
                return;
            };
            self.bitmaps.insert(key.to_string(), bitmap);
        }
        let bitmap = &self.bitmaps[key];
        // SAFETY: called between BeginDraw and EndDraw.
        unsafe {
            self.target.DrawBitmap(
                bitmap,
                Some(&rect.d2d()),
                1.0,
                D2D1_BITMAP_INTERPOLATION_MODE_LINEAR,
                None,
            );
        }
    }

}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rect_helpers_behave() {
        let rect = R::new(10.0, 20.0, 100.0, 50.0);
        assert!(rect.contains(10.0, 20.0));
        assert!(!rect.contains(110.0, 20.0));
        assert_eq!(rect.inset(5.0, 5.0), R::new(15.0, 25.0, 90.0, 40.0));
        assert!(rect.overlaps(R::new(100.0, 60.0, 50.0, 50.0)));
        assert!(!rect.overlaps(R::new(110.0, 20.0, 5.0, 5.0)));
        assert!(rect.encloses(R::new(20.0, 30.0, 10.0, 10.0)));
        assert!(!rect.encloses(R::new(20.0, 30.0, 200.0, 10.0)));
    }

    #[test]
    fn measures_and_ellipsizes_korean_text() {
        crate::platform::com_init_thread();
        let graphics = gfx().unwrap();
        let (short, _) = graphics.measure("가", 12.0, false, 1000.0, false);
        let (long, _) = graphics.measure("가나다라마바사", 12.0, false, 1000.0, false);
        assert!(short > 0.0 && long > short * 3.0);

        let path = r"C:\Users\한글사용자\아주\긴\폴더\이름\최종보고서.xlsx";
        assert_eq!(graphics.ellipsize_middle("짧음", 500.0, 12.0, false), "짧음");
        let fitted = graphics.ellipsize_middle(path, 120.0, 12.0, false);
        assert!(fitted.contains('…'));
        assert!(fitted.ends_with("xlsx"));
        assert!(graphics.measure(&fitted, 12.0, false, 1000.0, false).0 <= 120.0);
    }

    #[test]
    fn wraps_text_to_a_measured_height() {
        crate::platform::com_init_thread();
        let graphics = gfx().unwrap();
        let text = "메시지가 아주 길면 여러 줄로 줄바꿈되어 높이가 늘어나야 합니다.";
        let (_, one_line) = graphics.measure(text, 11.0, false, 2000.0, false);
        let (_, wrapped) = graphics.measure(text, 11.0, false, 100.0, true);
        assert!(wrapped > one_line * 1.5);
    }

    #[test]
    fn decodes_the_embedded_brand_mark() {
        crate::platform::com_init_thread();
        let brand = gfx().unwrap().brand();
        assert_eq!((brand.width, brand.height), (BRAND_SIZE, BRAND_SIZE));
        assert!(crate::icons::has_visible_alpha(&brand));
    }
}

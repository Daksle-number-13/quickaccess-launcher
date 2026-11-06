//! Shared visual tokens, identical to the Python release's light and dark pairs.

use windows::Win32::Graphics::Direct2D::Common::D2D1_COLOR_F;

use crate::model::AppearanceMode;
use crate::platform::registry::system_prefers_dark;

pub type Color = D2D1_COLOR_F;

pub const fn rgb(value: u32) -> Color {
    Color {
        r: ((value >> 16) & 0xFF) as f32 / 255.0,
        g: ((value >> 8) & 0xFF) as f32 / 255.0,
        b: (value & 0xFF) as f32 / 255.0,
        a: 1.0,
    }
}

/// `COLORREF` byte order for GDI-drawn native edit controls.
pub fn colorref(color: Color) -> u32 {
    let channel = |value: f32| (value * 255.0).round() as u32;
    channel(color.r) | channel(color.g) << 8 | channel(color.b) << 16
}

#[derive(Clone, Copy)]
pub struct Palette {
    pub dark: bool,
    pub bg: Color,
    pub surface: Color,
    pub surface_alt: Color,
    pub surface_hover: Color,
    pub border: Color,
    pub text: Color,
    pub muted: Color,
    pub accent: Color,
    pub accent_hover: Color,
    pub accent_soft: Color,
    pub danger: Color,
    pub danger_hover: Color,
    pub danger_soft: Color,
    pub danger_soft_hover: Color,
    pub success: Color,
    pub warning: Color,
    pub warning_soft: Color,
    pub warning_soft_hover: Color,
    pub white: Color,
}

pub const LIGHT: Palette = Palette {
    dark: false,
    bg: rgb(0xF8FAFD),
    surface: rgb(0xFFFFFF),
    surface_alt: rgb(0xF4F7FB),
    surface_hover: rgb(0xEAF0F7),
    border: rgb(0xD7DEE8),
    text: rgb(0x17202A),
    muted: rgb(0x667085),
    accent: rgb(0x2563EB),
    accent_hover: rgb(0x1D4ED8),
    accent_soft: rgb(0xE9F0FF),
    danger: rgb(0xC4322B),
    danger_hover: rgb(0xA92520),
    danger_soft: rgb(0xFFF0EF),
    danger_soft_hover: rgb(0xFFE3E1),
    success: rgb(0x178347),
    warning: rgb(0xA15C00),
    warning_soft: rgb(0xFFF6E5),
    warning_soft_hover: rgb(0xFFEDCC),
    white: rgb(0xFFFFFF),
};

pub const DARK: Palette = Palette {
    dark: true,
    bg: rgb(0x0F1115),
    surface: rgb(0x191C22),
    surface_alt: rgb(0x20242B),
    surface_hover: rgb(0x292E37),
    border: rgb(0x343A45),
    text: rgb(0xF4F6F8),
    muted: rgb(0x9CA6B4),
    accent: rgb(0x5B91FF),
    accent_hover: rgb(0x76A5FF),
    accent_soft: rgb(0x192B4D),
    danger: rgb(0xFF7B72),
    danger_hover: rgb(0xFF958D),
    danger_soft: rgb(0x3B2022),
    danger_soft_hover: rgb(0x4A272A),
    success: rgb(0x4CCB7F),
    warning: rgb(0xF6B94A),
    warning_soft: rgb(0x392B16),
    warning_soft_hover: rgb(0x49371A),
    white: rgb(0xFFFFFF),
};

pub fn palette_for(mode: AppearanceMode) -> &'static Palette {
    let dark = match mode {
        AppearanceMode::Light => false,
        AppearanceMode::Dark => true,
        AppearanceMode::System => system_prefers_dark(),
    };
    if dark {
        &DARK
    } else {
        &LIGHT
    }
}

pub const GLYPH_FOLDER: &str = "\u{E8B7}";
pub const GLYPH_FILE: &str = "\u{E8A5}";
pub const GLYPH_LINK: &str = "\u{E71B}";
pub const GLYPH_SETTINGS: &str = "\u{E713}";
pub const GLYPH_DELETE: &str = "\u{E74D}";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn converts_channels() {
        let color = rgb(0x2563EB);
        assert!((color.r - 0x25 as f32 / 255.0).abs() < 1e-6);
        assert!((color.b - 0xEB as f32 / 255.0).abs() < 1e-6);
        assert_eq!(colorref(color), 0xEB6325);
    }

    #[test]
    fn explicit_modes_ignore_the_system() {
        assert!(!palette_for(AppearanceMode::Light).dark);
        assert!(palette_for(AppearanceMode::Dark).dark);
    }
}

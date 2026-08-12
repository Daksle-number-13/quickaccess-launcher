"""Keep Pillow's dynamic image-format plugins out of the launcher bundle.

QuickAccess creates one RGBA tray bitmap in memory and never decodes user
images.  PyInstaller's default Pillow hook collects every codec (AVIF, WebP,
JPEG, and others), adding several megabytes and one-file extraction latency.
"""

hiddenimports = ["PIL.IcoImagePlugin"]

#pragma once
// Movie Remote: keeps the RGB panel's bounce buffers in step with the frame.
//
// In bounce-buffer mode the ESP-IDF driver refills a 16-line buffer from the PSRAM
// frame buffer in the DMA EOF interrupt, moving `bounce_pos_px` on each time. If an EOF
// interrupt is lost (it was held off while flash was written, or two arrived while it
// couldn't run), the position falls one buffer behind and stays there: with
// CONFIG_LCD_RGB_RESTART_IN_VSYNC the driver only corrects a position that is *ahead*.
// The whole picture then shows shifted down by 16 or 32 lines, wrapped round at the
// bottom, until the box restarts. The buffer parity (`bb_eof_count`) also stays flipped,
// so refills land in the buffer being sent: streaks.
//
// At VSYNC_END, before the driver's own restart, each frame should have left the
// position two buffers into the next frame. If it hasn't, this sets it back to the
// start so the driver's restart refills both buffers from line 0, and it re-aligns the
// parity. A lost interrupt then costs one frame instead of a lasting shift.
//
// It needs the driver's private struct, so it's checked against ESP-IDF 5.5 and sanity
// checked at run time; on anything else it stays off and the driver works as before.

#include <esp_lcd_panel_rgb.h>
#include <cstdint>

namespace esphome::mipi_rgb {

/// Hook the resync to this panel. Returns false (and leaves the panel alone) if the
/// driver's private layout doesn't look as expected.
bool bounce_resync_install(esp_lcd_panel_handle_t panel, size_t width, size_t height, size_t bounce_px);

/// Frames on which the position was found out of step and reset, since start.
uint32_t bounce_resync_count();

}  // namespace esphome::mipi_rgb

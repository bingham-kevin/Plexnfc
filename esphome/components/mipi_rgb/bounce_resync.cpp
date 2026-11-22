#if defined(USE_ESP32_VARIANT_ESP32S3)
#include "bounce_resync.h"
#include "esphome/core/log.h"
#include <esp_attr.h>
#include <esp_idf_version.h>
#include <esp_intr_alloc.h>
#include <esp_lcd_panel_interface.h>
#include <esp_memory_utils.h>
#include <esp_pm.h>
#include <hal/lcd_hal.h>
#include <soc/soc_caps.h>

namespace esphome::mipi_rgb {

static const char *const RESYNC_TAG = "display.mipi_rgb";

#if ESP_IDF_VERSION_MAJOR == 5 && ESP_IDF_VERSION_MINOR == 5
#define BOUNCE_RESYNC_SUPPORTED 1

// The start of `struct esp_rgb_panel_t` in esp_lcd/rgb/esp_lcd_panel_rgb.c (ESP-IDF 5.5),
// as built for the ESP32-S3 (RGB_LCD_NEEDS_SEPARATE_RESTART_LINK, no link-switch event),
// up to the fields used here.
struct RgbPanelHead {
  esp_lcd_panel_t base;
  int panel_id;
  lcd_hal_context_t hal;
  size_t data_width;
  size_t fb_bits_per_pixel;
  size_t num_fbs;
  size_t output_bits_per_pixel;
  size_t dma_burst_size;
  intr_handle_t intr;
  esp_pm_lock_handle_t pm_lock;
  size_t num_dma_nodes;
  void *dma_chan;
  void *dma_fb_links[3];
  void *dma_bb_link;
  void *dma_restart_link;
  uint8_t *fbs[3];
  uint8_t *bounce_buffer[2];
  size_t fb_size;
  size_t bb_size;
  uint8_t cur_fb_index;
  uint8_t bb_fb_index;
  size_t int_mem_align;
  size_t ext_mem_align;
  int hsync_gpio_num;
  int vsync_gpio_num;
  int de_gpio_num;
  int pclk_gpio_num;
  int disp_gpio_num;
  int data_gpio_nums[SOC_LCDCAM_RGB_DATA_WIDTH];
  uint64_t gpio_reserve_mask;
  uint32_t src_clk_hz;
  esp_lcd_rgb_timing_t timings;
  int bounce_pos_px;
  size_t bb_eof_count;
  size_t expect_eof_count;
};

static int expected_pos_px = 0;  // two bounce buffers into the frame
static volatile uint32_t resyncs = 0;

static IRAM_ATTR bool on_vsync(esp_lcd_panel_handle_t panel, const esp_lcd_rgb_panel_event_data_t *, void *) {
  auto *p = reinterpret_cast<RgbPanelHead *>(panel);
  // Runs in the LCD interrupt; the GDMA EOF interrupt that moves these is on the same
  // core at the same level, so it can't run in between.
  if (p->bounce_pos_px != expected_pos_px) {
    p->bounce_pos_px = 0;  // the driver's restart now refills both buffers from line 0
    resyncs = resyncs + 1;
  }
  // The restart sends buffer 0 first, so the next EOF is buffer 0's
  p->bb_eof_count = 0;
  return false;
}
#endif

bool bounce_resync_install(esp_lcd_panel_handle_t panel, size_t width, size_t height, size_t bounce_px) {
#ifdef BOUNCE_RESYNC_SUPPORTED
  auto *p = reinterpret_cast<RgbPanelHead *>(panel);
  size_t bpp = 16;
  bool looks_right = p != nullptr && p->fb_bits_per_pixel == bpp && p->num_fbs == 1 &&
                     p->fb_size == width * height * bpp / 8 && p->bb_size == bounce_px * bpp / 8 &&
                     p->expect_eof_count == p->fb_size / p->bb_size && p->fbs[0] != nullptr &&
                     esp_ptr_external_ram(p->fbs[0]) && p->bounce_buffer[0] != nullptr &&
                     esp_ptr_internal(p->bounce_buffer[0]) && p->timings.h_res == width &&
                     p->timings.v_res == height && p->bounce_pos_px >= 0 &&
                     p->bounce_pos_px < static_cast<int>(width * height);
  if (!looks_right) {
    ESP_LOGW(RESYNC_TAG, "Bounce buffer resync off: the driver's layout isn't as expected");
    return false;
  }
  expected_pos_px = static_cast<int>(bounce_px * 2);
  esp_lcd_rgb_panel_event_callbacks_t callbacks{};
  callbacks.on_vsync = on_vsync;
  esp_err_t err = esp_lcd_rgb_panel_register_event_callbacks(panel, &callbacks, nullptr);
  if (err != ESP_OK) {
    ESP_LOGW(RESYNC_TAG, "Bounce buffer resync off: %s", esp_err_to_name(err));
    return false;
  }
  ESP_LOGCONFIG(RESYNC_TAG, "Bounce buffer resync on");
  return true;
#else
  ESP_LOGW(RESYNC_TAG, "Bounce buffer resync off: not checked against this ESP-IDF");
  return false;
#endif
}

uint32_t bounce_resync_count() {
#ifdef BOUNCE_RESYNC_SUPPORTED
  return resyncs;
#else
  return 0;
#endif
}

}  // namespace esphome::mipi_rgb
#endif

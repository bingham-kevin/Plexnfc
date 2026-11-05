#pragma once

#include <algorithm>
#include <deque>
#include <mutex>
#include <string>

#include <JPEGDEC.h>
#include <esp_heap_caps.h>
#include <esp_http_client.h>
#include <lvgl.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/log.h"

namespace esphome::marquee_link {

/// Talks to Marquee Server on a background task, so the screen and touch never wait
/// on the network. Requests are queued from the main loop; each answer comes back on
/// the main loop through on_response (tag, status, body). status is -1 if the server
/// couldn't be reached.
///
/// request_image() downloads a JPEG and decodes it on the background task too. When
/// it's ready, on_response gets its tag with status 200, and image() is the picture to
/// show with lv_image_set_src().
class MarqueeLink : public Component {
 public:
  void set_base_url(const std::string &url) { this->base_url_ = url; }

  /// "Bearer <secret>", sent with every request; empty sends none.
  void set_auth(const std::string &auth) {
    std::lock_guard<std::mutex> guard(this->lock_);
    this->auth_ = auth;
  }

  /// Queue a request. With replace, a queued request with the same tag that hasn't
  /// started yet is updated instead of adding another (polls); without it, every
  /// request is sent (button presses, tag taps).
  void request(const std::string &tag, const char *method, const std::string &path, const std::string &body = "",
               bool replace = true) {
    std::lock_guard<std::mutex> guard(this->lock_);
    if (replace) {
      for (auto &job : this->jobs_) {
        if (job.tag == tag) {
          job = Job{tag, method, path, body};
          return;
        }
      }
    }
    if (this->jobs_.size() >= MAX_JOBS) {
      ESP_LOGW("marquee_link", "Too many requests waiting; dropped %s", tag.c_str());
      return;
    }
    this->jobs_.push_back(Job{tag, method, path, body});
    if (this->wake_ != nullptr)
      xSemaphoreGive(this->wake_);
  }

  /// Download a JPEG of exactly width x height and decode it, all in the background.
  void request_image(const std::string &tag, const std::string &path, uint16_t width, uint16_t height) {
    std::lock_guard<std::mutex> guard(this->lock_);
    Job job{tag, "GET", path, "", width, height};
    for (auto &queued : this->jobs_) {
      if (queued.tag == tag) {
        queued = job;
        return;
      }
    }
    if (this->jobs_.size() >= MAX_JOBS)
      return;
    this->jobs_.push_back(job);
    if (this->wake_ != nullptr)
      xSemaphoreGive(this->wake_);
  }

  /// The last picture request_image() delivered, for lv_image_set_src().
  const lv_image_dsc_t *image() const { return &this->images_[this->shown_]; }

  /// A request with this tag is waiting or being sent.
  bool pending(const std::string &tag) {
    std::lock_guard<std::mutex> guard(this->lock_);
    if (this->in_flight_ == tag)
      return true;
    for (auto &job : this->jobs_) {
      if (job.tag == tag)
        return true;
    }
    return false;
  }

  /// A JSON string literal for value, quotes included.
  static std::string quote(const std::string &value) {
    std::string out = "\"";
    for (char c : value) {
      if (c == '"' || c == '\\') {
        out += '\\';
        out += c;
      } else if ((unsigned char) c < 0x20) {
        char esc[8];
        snprintf(esc, sizeof esc, "\\u%04x", c);
        out += esc;
      } else {
        out += c;
      }
    }
    return out + "\"";
  }

  Trigger<std::string, int, std::string> *get_response_trigger() { return &this->response_trigger_; }

  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void setup() override {
    {
      // Anything queued before setup goes first
      std::lock_guard<std::mutex> guard(this->lock_);
      this->wake_ = xSemaphoreCreateCounting(MAX_JOBS, this->jobs_.size());
    }
    // Same priority as the main loop, so a long decode shares the CPU with it
    xTaskCreate(&MarqueeLink::task_entry_, "marquee_link", 8192, this, 1, nullptr);
  }

  // Hand finished answers to on_response, on the main loop.
  void loop() override {
    for (;;) {
      Result result;
      {
        std::lock_guard<std::mutex> guard(this->lock_);
        if (this->results_.empty())
          return;
        result = std::move(this->results_.front());
        this->results_.pop_front();
      }
      if (result.pixels != nullptr) {
        // Show it in the other slot, so the picture on screen stays valid until it's replaced
        int slot = 1 - this->shown_;
        void *old = const_cast<uint8_t *>(this->images_[slot].data);
        free(old);  // LVGL is built without an image cache here, so nothing else holds it
        lv_image_dsc_t &dsc = this->images_[slot];
        memset(&dsc, 0, sizeof dsc);
        dsc.header.magic = LV_IMAGE_HEADER_MAGIC;
        dsc.header.cf = LV_COLOR_FORMAT_RGB565;
        dsc.header.w = result.width;
        dsc.header.h = result.height;
        dsc.header.stride = result.width * 2;
        dsc.data_size = result.width * result.height * 2;
        dsc.data = reinterpret_cast<const uint8_t *>(result.pixels);
        this->shown_ = slot;
      }
      this->response_trigger_.trigger(result.tag, result.status, result.body);
    }
  }

 protected:
  static constexpr size_t MAX_JOBS = 8;
  static constexpr size_t MAX_BODY = 4096;

  static constexpr size_t MAX_IMAGE = 256 * 1024;

  struct Job {
    std::string tag;
    const char *method;
    std::string path;
    std::string body;
    uint16_t width{0};  // non-zero: a JPEG to decode
    uint16_t height{0};
  };
  struct Result {
    std::string tag;
    int status{-1};
    std::string body;
    uint16_t *pixels{nullptr};
    uint16_t width{0};
    uint16_t height{0};
  };

  // Big buffers go in PSRAM where there is some
  static void *big_alloc(size_t size) {
    void *p = heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return p != nullptr ? p : malloc(size);
  }

  struct Decode {
    uint16_t *pixels;
    uint16_t width, height;
  };

  static int draw_(JPEGDRAW *draw) {
    auto *out = static_cast<Decode *>(draw->pUser);
    for (int y = 0; y < draw->iHeight; y++) {
      int row = draw->y + y;
      if (row >= out->height)
        break;
      int count = std::min<int>(draw->iWidth, out->width - draw->x);
      if (count > 0)
        memcpy(out->pixels + row * out->width + draw->x, draw->pPixels + y * draw->iWidth, count * 2);
    }
    // Pause between blocks. The screen is refreshed from PSRAM too, and decoding flat
    // out takes so much of PSRAM's bandwidth that the screen can't keep up and glitches.
    vTaskDelay(1);
    return 1;
  }

  // Decode a JPEG into a new RGB565 picture; nullptr if it can't
  static uint16_t *decode_jpeg_(uint8_t *data, size_t size, uint16_t width, uint16_t height) {
    auto *pixels = static_cast<uint16_t *>(big_alloc(width * height * 2));
    // The decoder's working memory in internal RAM if it fits, to keep it off PSRAM
    auto *jpeg = static_cast<JPEGDEC *>(heap_caps_malloc(sizeof(JPEGDEC), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (jpeg == nullptr)
      jpeg = static_cast<JPEGDEC *>(big_alloc(sizeof(JPEGDEC)));
    if (pixels == nullptr || jpeg == nullptr) {
      free(pixels);
      free(jpeg);
      return nullptr;
    }
    new (jpeg) JPEGDEC();
    // Cleared in pieces, for the same reason as the pause in draw_()
    size_t total = width * height * 2;
    for (size_t done = 0; done < total; done += 8192) {
      memset(reinterpret_cast<uint8_t *>(pixels) + done, 0, std::min<size_t>(8192, total - done));
      vTaskDelay(1);
    }
    Decode out{pixels, width, height};
    bool ok = jpeg->openRAM(data, size, draw_) != 0;
    if (ok) {
      jpeg->setUserPointer(&out);
      jpeg->setPixelType(RGB565_LITTLE_ENDIAN);
      ok = jpeg->decode(0, 0, 0) != 0;
      jpeg->close();
    }
    jpeg->~JPEGDEC();
    free(jpeg);
    if (!ok) {
      free(pixels);
      return nullptr;
    }
    return pixels;
  }

  static void task_entry_(void *arg) { static_cast<MarqueeLink *>(arg)->run_(); }

  void run_() {
    for (;;) {
      xSemaphoreTake(this->wake_, portMAX_DELAY);
      Job job;
      std::string auth;
      {
        std::lock_guard<std::mutex> guard(this->lock_);
        if (this->jobs_.empty())
          continue;  // its wake-up was for a request that was replaced
        job = std::move(this->jobs_.front());
        this->jobs_.pop_front();
        this->in_flight_ = job.tag;
        auth = this->auth_;
      }
      Result result{job.tag, -1, ""};
      this->send_(job, auth, result);
      {
        std::lock_guard<std::mutex> guard(this->lock_);
        this->results_.push_back(std::move(result));
        this->in_flight_.clear();
      }
    }
  }

  void send_(const Job &job, const std::string &auth, Result &result) {
    std::string url = this->base_url_ + job.path;
    esp_http_client_config_t config{};
    config.url = url.c_str();
    config.timeout_ms = 6000;
    config.method = strcmp(job.method, "POST") == 0 ? HTTP_METHOD_POST : HTTP_METHOD_GET;
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == nullptr)
      return;
    if (!auth.empty())
      esp_http_client_set_header(client, "Authorization", auth.c_str());
    if (config.method == HTTP_METHOD_POST)
      esp_http_client_set_header(client, "Content-Type", "application/json");
    uint32_t started = millis();
    esp_err_t err = esp_http_client_open(client, job.body.size());
    if (err == ESP_OK) {
      if (!job.body.empty())
        esp_http_client_write(client, job.body.data(), job.body.size());
      esp_http_client_fetch_headers(client);
      result.status = esp_http_client_get_status_code(client);
      if (job.width != 0) {
        this->read_image_(client, job, result);
      } else {
        char buf[512];
        int n;
        while ((n = esp_http_client_read(client, buf, sizeof buf)) > 0) {
          if (result.body.size() + n <= MAX_BODY)
            result.body.append(buf, n);
        }
      }
      // Slow answers are worth knowing about; quick polls aren't
      unsigned took = millis() - started;
      if (took >= 500 || config.method == HTTP_METHOD_POST) {
        ESP_LOGD("marquee_link", "%s %s: %d in %u ms", job.method, job.path.c_str(), result.status, took);
      } else {
        ESP_LOGV("marquee_link", "%s %s: %d in %u ms", job.method, job.path.c_str(), result.status, took);
      }
    } else {
      ESP_LOGW("marquee_link", "%s %s failed: %s", job.method, job.path.c_str(), esp_err_to_name(err));
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
  }

  void read_image_(esp_http_client_handle_t client, const Job &job, Result &result) {
    if (result.status != 200)
      return;
    auto *data = static_cast<uint8_t *>(big_alloc(MAX_IMAGE));
    if (data == nullptr) {
      result.status = -1;
      return;
    }
    size_t size = 0;
    int n;
    while (size < MAX_IMAGE && (n = esp_http_client_read(client, reinterpret_cast<char *>(data) + size,
                                                          std::min<size_t>(4096, MAX_IMAGE - size))) > 0)
      size += n;
    uint32_t started = millis();
    result.pixels = decode_jpeg_(data, size, job.width, job.height);
    free(data);
    if (result.pixels == nullptr) {
      ESP_LOGW("marquee_link", "Couldn't decode %s (%u bytes)", job.path.c_str(), (unsigned) size);
      result.status = -1;
      return;
    }
    result.width = job.width;
    result.height = job.height;
    ESP_LOGD("marquee_link", "Decoded %s in %u ms", job.path.c_str(), (unsigned) (millis() - started));
  }

  lv_image_dsc_t images_[2]{};
  int shown_{0};
  std::string base_url_;
  std::string auth_;
  std::mutex lock_;
  std::deque<Job> jobs_;
  std::deque<Result> results_;
  std::string in_flight_;
  SemaphoreHandle_t wake_{nullptr};
  Trigger<std::string, int, std::string> response_trigger_;
};

}  // namespace esphome::marquee_link

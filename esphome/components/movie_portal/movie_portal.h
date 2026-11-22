#pragma once

#include "esphome/core/component.h"
#include "esphome/components/captive_portal/captive_portal.h"
#include "esphome/components/web_server_base/web_server_base.h"

namespace esphome::movie_portal {

/// Serves the Movie Remote's own Wi-Fi setup page in place of the captive portal's,
/// while the captive portal is up. It's added to the web server before the captive
/// portal is, so it gets each request first.
class MoviePortal : public AsyncWebHandler, public Component {
 public:
  explicit MoviePortal(web_server_base::WebServerBase *base) : base_(base) {}

  void set_page(const uint8_t *page, size_t size) {
    this->page_ = page;
    this->size_ = size;
  }

  // Before the captive portal, which starts with the Wi-Fi access point
  float get_setup_priority() const override { return setup_priority::WIFI + 1.0f; }

  void setup() override { this->base_->add_handler_without_auth(this); }

  bool canHandle(AsyncWebServerRequest *request) const override {
    auto *portal = captive_portal::global_captive_portal;
    if (portal == nullptr || !portal->is_active() || request->method() != HTTP_GET)
      return false;
    char url_buf[AsyncWebServerRequest::URL_BUF_SIZE];
    StringRef url = request->url_to(url_buf);
    // The captive portal's own endpoints stay its own
    return !(url == "/config.json" || url == "/wifisave");
  }

  void handleRequest(AsyncWebServerRequest *request) override {
    auto *response = request->beginResponse(200, "text/html", this->page_, this->size_);
    response->addHeader("Content-Encoding", "gzip");
    response->addHeader("Cache-Control", "no-store");
    request->send(response);
  }

 protected:
  web_server_base::WebServerBase *base_;
  const uint8_t *page_{nullptr};
  size_t size_{0};
};

}  // namespace esphome::movie_portal

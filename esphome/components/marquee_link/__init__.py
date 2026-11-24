"""Requests to Marquee Server, sent from a background task.

ESPHome's http_request blocks the main loop until the server answers, and nothing on
the screen moves or responds to touch meanwhile. This sends them from its own task
and hands each answer back to the main loop through on_response.
"""

from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_URL

DEPENDENCIES = ["wifi", "lvgl"]

CONF_ON_RESPONSE = "on_response"

marquee_link_ns = cg.esphome_ns.namespace("marquee_link")
MarqueeLink = marquee_link_ns.class_("MarqueeLink", cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(MarqueeLink),
        cv.Required(CONF_URL): cv.url,
        cv.Optional(CONF_ON_RESPONSE): automation.validate_automation(single=True),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_base_url(config[CONF_URL].rstrip("/")))
    # Posters are decoded here too, with the same JPEG library build as ESPHome's own images
    # (its runtime_image asks for this one; two builds of it won't link)
    cg.add_library("JPEGDEC", "1.8.4", "https://github.com/esphome-libs/JPEGDEC#1.8.4-esphome.4")
    if conf := config.get(CONF_ON_RESPONSE):
        await automation.build_automation(
            var.get_response_trigger(),
            [(cg.std_string, "tag"), (cg.int_, "status"), (cg.std_string, "body")],
            conf,
        )

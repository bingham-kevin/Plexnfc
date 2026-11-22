"""The Movie Remote's Wi-Fi setup page.

ESPHome's captive portal answers every page request with its own built-in page. This
component answers those requests first with portal.html, and leaves the captive
portal's /config.json (the networks it found) and /wifisave (join one) as they are.
"""

import gzip
from pathlib import Path

import esphome.codegen as cg
from esphome.components import web_server_base
from esphome.components.web_server_base import CONF_WEB_SERVER_BASE_ID
import esphome.config_validation as cv
from esphome.const import CONF_ID

AUTO_LOAD = ["web_server_base"]
DEPENDENCIES = ["wifi", "captive_portal"]

movie_portal_ns = cg.esphome_ns.namespace("movie_portal")
MoviePortal = movie_portal_ns.class_("MoviePortal", cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(MoviePortal),
        cv.GenerateID(CONF_WEB_SERVER_BASE_ID): cv.use_id(web_server_base.WebServerBase),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    base = await cg.get_variable(config[CONF_WEB_SERVER_BASE_ID])
    var = cg.new_Pvariable(config[CONF_ID], base)
    await cg.register_component(var, config)
    page = gzip.compress((Path(__file__).parent / "portal.html").read_bytes(), 9)
    cg.add_global(
        cg.RawStatement(
            "static const uint8_t MOVIE_PORTAL_PAGE[] = {" + ",".join(str(b) for b in page) + "};"
        )
    )
    cg.add(var.set_page(cg.RawExpression("MOVIE_PORTAL_PAGE"), len(page)))

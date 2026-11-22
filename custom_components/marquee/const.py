"""Constants for the Marquee integration."""

DOMAIN = "marquee"
CONF_SECRET = "secret"

# Fired on Home Assistant's event bus as Marquee players start, pause, resume and stop
# movies. Device triggers listen for it; automations can too, with an Event trigger.
EVENT = "marquee_event"
TRIGGER_TYPES = ("started", "paused", "resumed", "stopped")

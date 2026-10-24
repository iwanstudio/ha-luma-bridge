"""Constants for the Luma Bridge integration."""

DOMAIN = "luma_bridge"

DOMAINS = [
    "light",
    "switch",
    "fan",
    "input_boolean",
    "cover",
    "scene",
    "script",
    "button",
]

# What one press on the TV tile does. Luma does not choose the service.
ACTION = {
    "light": "toggle",
    "switch": "toggle",
    "fan": "toggle",
    "input_boolean": "toggle",
    "cover": "toggle",
    "scene": "turn_on",
    "script": "turn_on",
    "button": "press",
}

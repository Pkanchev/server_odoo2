# -*- coding: utf-8 -*-
# Delivery modes:
# - disabled: new logic not applied
# - date_only: compute + propagate delivery date and logistics fields
# - full: date_only + expedition + tasks + allocations
DELIVERY_MODE_SELECTION = [
    ("inherit", "Inherit (use parent)"),
    ("disabled", "Disabled"),
    ("date_only", "Only delivery date"),
    ("full", "Full logistics"),
]

DELIVERY_MODE_APPLIED_SELECTION = [
    ("disabled", "Disabled"),
    ("date_only", "Only delivery date"),
]

WEEKDAY_SELECTION = [
    ("0", "Monday"),
    ("1", "Tuesday"),
    ("2", "Wednesday"),
    ("3", "Thursday"),
    ("4", "Friday"),
    ("5", "Saturday"),
    ("6", "Sunday"),
]

DELIVERY_PRIORITY_SELECTION = [
    ("low", "Low"),
    ("normal", "Normal"),
    ("high", "High"),
    ("urgent", "Urgent"),
]

EXPEDITION_STATE_SELECTION = [
    ("planned", "Planned"),
    ("preparing", "Preparing"),
    ("ready", "Ready to load"),
    ("loaded", "Loaded"),
    ("dispatched", "Dispatched"),
    ("delivered", "Delivered"),
    ("done", "Done"),
    ("hold", "Hold / Problem"),
]

# From these states onwards, data must be stabilized (no free edits)
EXPEDITION_LOCKED_STATES = {"loaded", "dispatched", "delivered", "done"}

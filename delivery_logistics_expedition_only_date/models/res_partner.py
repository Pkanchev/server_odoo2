# -*- coding: utf-8 -*-
from odoo import api, fields, models

from .delivery_constants import DELIVERY_MODE_SELECTION, DELIVERY_PRIORITY_SELECTION, WEEKDAY_SELECTION


class ResPartner(models.Model):
    _inherit = "res.partner"

    # --- Delivery mode (staged rollout) ---
    delivery_mode = fields.Selection(
        DELIVERY_MODE_SELECTION,
        string="Delivery Mode",
        default="inherit",
        help=(
            "Controls whether the delivery logic is applied:\n"
            "- Disabled: do not apply the new logic\n"
            "- Only delivery date: compute + propagate delivery date & logistics fields\n"
            "- Full logistics: also create expeditions + tasks + allocations\n"
            "For delivery addresses, 'Inherit' means use the parent company setting."
        ),
    )

    # --- Delivery date rules template ---
    delivery_lead_days = fields.Integer(
        string="Delivery Lead Days",
        default=False,
        help="If set, delivery date = sale order date + lead days (has priority over weekday rule).",
    )
    delivery_weekday = fields.Selection(
        WEEKDAY_SELECTION,
        string="Delivery Weekday",
        default=False,
        help="If lead days is not set, plan delivery on the next occurrence of this weekday.",
    )
    delivery_weeks_ahead = fields.Integer(
        string="Weeks Ahead",
        default=False,
        help="If weekday is set, add this many weeks ahead (e.g. plan for the next-next Wednesday).",
    )

    # --- Operational logistics template fields (copied to SO) ---
    delivery_driver_id = fields.Many2one("res.users", string="Default Driver", default=False)
    delivery_region = fields.Char(string="Region", default=False)

    delivery_window_from = fields.Float(string="Time Window From", default=False)
    delivery_window_to = fields.Float(string="Time Window To", default=False)
    delivery_unload_time = fields.Integer(string="Unload Time (min)", default=False)

    delivery_contact_name = fields.Char(string="Delivery Contact", default=False)
    delivery_contact_phone = fields.Char(string="Delivery Phone", default=False)
    delivery_instructions = fields.Text(string="Delivery Instructions", default=False)
    delivery_priority = fields.Selection(DELIVERY_PRIORITY_SELECTION, string="Priority", default=False)

    def _get_effective_delivery_mode(self):
        """
        Returns the effective delivery mode for this partner:
        - If delivery_mode != inherit => return it
        - Else use parent.company delivery_mode if present
        - Else fallback to disabled
        """
        self.ensure_one()
        if self.delivery_mode and self.delivery_mode != "inherit":
            return self.delivery_mode
        if self.parent_id:
            return self.parent_id._get_effective_delivery_mode()
        return "disabled"

    def _get_delivery_template_values(self, fallback_partner=None):
        """
        Build a template dict used to populate sale.order.

        Priority rule (as per requirements):
        - delivery address (object) has priority
        - if a value is empty on the object, fallback to customer company values
        """
        self.ensure_one()
        fallback_partner = fallback_partner if fallback_partner else self.env["res.partner"]

        template_fields = [
            # rules
            "delivery_lead_days",
            "delivery_weekday",
            "delivery_weeks_ahead",
            # operational info
            "delivery_driver_id",
            "delivery_region",
            "delivery_window_from",
            "delivery_window_to",
            "delivery_unload_time",
            "delivery_contact_name",
            "delivery_contact_phone",
            "delivery_instructions",
            "delivery_priority",
        ]

        vals = {}
        for fname in template_fields:
            # Important: we treat False/None as "not set", so fallback can apply.
            current_val = self[fname]
            if (current_val is False or current_val is None) and fallback_partner:
                current_val = fallback_partner[fname]
            vals[fname] = current_val
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        """
        Ensure top-level companies default to a concrete mode unless explicitly provided.
        We keep delivery addresses defaulting to 'inherit' to enable staged rollout.
        """
        for vals in vals_list:
            if "delivery_mode" not in vals:
                # If this partner is not a child contact/address => default to disabled for safety.
                if not vals.get("parent_id"):
                    vals["delivery_mode"] = "disabled"
        return super().create(vals_list)

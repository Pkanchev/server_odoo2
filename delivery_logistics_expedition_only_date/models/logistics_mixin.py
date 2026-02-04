# -*- coding: utf-8 -*-
from odoo import fields, models, _

from .delivery_constants import DELIVERY_PRIORITY_SELECTION


class DeliveryLogisticsMixin(models.AbstractModel):
    """
    Reusable set of logistics info fields that must travel across documents:
    SO -> Picking -> Invoice -> (optional) Task.

    The fields are informational; they do not alter stock quantities nor accounting.
    """

    _name = "delivery.logistics.mixin"
    _description = "Delivery Logistics Mixin"

    delivery_date = fields.Date(
        string="Logistics Delivery Date",
        help="Planned delivery date used by the Logistics/Expeditions workflow.",
        copy=False,
    )

    delivery_driver_id = fields.Many2one("res.users", string="Driver", copy=False)
    delivery_region = fields.Char(string="Region", copy=False)

    delivery_window_from = fields.Float(
        string="Time Window From",
        help="Time window start (hours).",
        default=False,
        copy=False,
    )
    delivery_window_to = fields.Float(
        string="Time Window To",
        help="Time window end (hours).",
        default=False,
        copy=False,
    )
    delivery_unload_time = fields.Integer(
        string="Unload Time (min)",
        help="Estimated unloading time in minutes.",
        default=False,
        copy=False,
    )

    delivery_contact_name = fields.Char(string="Delivery Contact", copy=False)
    delivery_contact_phone = fields.Char(string="Delivery Phone", copy=False)

    delivery_instructions = fields.Text(string="Delivery Instructions", copy=False)
    delivery_priority = fields.Selection(
        selection=DELIVERY_PRIORITY_SELECTION,
        string="Delivery Priority",
        help="Priority used by the Logistics/Expeditions workflow (separate from Odoo picking priority).",
    )

    def _delivery_logistics_field_names(self):
        """
        Central place to define which fields are considered 'delivery logistics fields'
        for copying/sync across documents.
        """
        return [
            "delivery_date",
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

    def _prepare_delivery_logistics_vals(self):
        """
        Prepare a dict of current record's logistics values, suitable for write/create
        on another model that also has these fields.

        IMPORTANT: ensure SQL-safe values (Many2one -> ID).
        """
        self.ensure_one()
        vals = {}
        for fname in self._delivery_logistics_field_names():
            if fname not in self._fields:
                continue

            field = self._fields[fname]
            value = self[fname]

            # SQL-safe conversion: Many2one recordset -> integer id / False
            if field.type == "many2one":
                vals[fname] = value.id if value else False
            else:
                vals[fname] = value

        return vals

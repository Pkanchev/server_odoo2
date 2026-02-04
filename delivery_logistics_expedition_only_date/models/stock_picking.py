# -*- coding: utf-8 -*-
from odoo import api, fields, models


class StockPicking(models.Model):
    _name = "stock.picking"
    _inherit = ["stock.picking", "delivery.logistics.mixin"]

    expedition_line_id = fields.Many2one(
        "delivery.expedition.line",
        string="Expedition Line",
        copy=False,
        readonly=True,
        help="If full logistics mode is used, links this delivery order to a specific expedition line.",
    )

    def action_open_expedition_line(self):
        self.ensure_one()
        line = self.expedition_line_id
        if not line:
            return {"type": "ir.actions.act_window_close"}

        return {
            "type": "ir.actions.act_window",
            "name": "Expedition Line",
            "res_model": "delivery.expedition.line",
            "view_mode": "form",
            "res_id": line.id,
            "target": "current",
        }

    @api.model_create_multi
    def create(self, vals_list):
        """
        When pickings are created from a sale order, we copy logistics info immediately.
        This helps ensure warehouse sees the same data as in SO.
        """
        pickings = super().create(vals_list)

        for picking in pickings:
            if self.env.context.get("delivery_logistics_skip_sync"):
                continue
            if picking.picking_type_code != "outgoing":
                continue
            if not picking.sale_id:
                continue

            # Apply only when sale order has new logic active.
            if getattr(picking.sale_id, "delivery_mode_applied", "disabled") not in (
                "date_only",
                "full",
            ):
                continue

            vals = picking.sale_id._prepare_delivery_logistics_vals()
            picking.with_context(delivery_logistics_skip_sync=True).write(vals)

        return pickings

    def write(self, vals):
        """
        Prevent recursion with context. Otherwise keep standard behavior.
        """
        return super().write(vals)

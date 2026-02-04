# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class AccountMove(models.Model):
    _name = "account.move"
    _inherit = ["account.move", "delivery.logistics.mixin"]

    # Logistics -> Expedition traceability (invoice -> expedition line via related delivery order)
    logistics_expedition_line_id = fields.Many2one(
        "delivery.expedition.line",
        string="Expedition Line",
        compute="_compute_logistics_expedition_lines",
        readonly=True,
        store=False,
    )

    logistics_expedition_line_count = fields.Integer(
        string="Expedition Lines",
        compute="_compute_logistics_expedition_lines",
        readonly=True,
        store=False,
    )

    @api.depends(
        "invoice_line_ids.sale_line_ids.order_id.picking_ids.expedition_line_id"
    )
    def _compute_logistics_expedition_lines(self):
        """Compute expedition line(s) linked to this invoice.

        Source of truth:
        invoice -> sale order(s) -> outgoing picking(s) -> expedition_line_id

        If multiple expedition lines exist (invoice from multiple SOs / deliveries),
        we keep the count and show a button to open the list.
        """
        ExpeditionLine = self.env["delivery.expedition.line"].sudo()

        for move in self:
            lines = self.env["delivery.expedition.line"]

            # Gather sale orders via invoice lines
            sale_orders = move.invoice_line_ids.sale_line_ids.order_id
            if sale_orders:
                pickings = sale_orders.picking_ids.filtered(
                    lambda p: p.picking_type_code == "outgoing" and p.state != "cancel"
                )

                # Prefer direct link on picking
                linked = pickings.mapped("expedition_line_id")
                lines = linked.filtered(lambda l: l)

                # Fallback: if expedition_line_id is not stored on picking, search by picking_id
                if not lines and pickings:
                    lines = ExpeditionLine.search([("picking_id", "in", pickings.ids)])

            # De-duplicate
            lines = lines.exists()
            move.logistics_expedition_line_count = len(lines)
            move.logistics_expedition_line_id = (
                lines[:1].id if len(lines) == 1 else False
            )

    def action_open_logistics_expedition_lines(self):
        self.ensure_one()

        sale_orders = self.invoice_line_ids.sale_line_ids.order_id
        pickings = sale_orders.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state != "cancel"
        )

        domain = []
        if pickings:
            domain = [("picking_id", "in", pickings.ids)]

        # If we have exactly one, open it directly
        if self.logistics_expedition_line_id:
            return {
                "type": "ir.actions.act_window",
                "name": _("Expedition Line"),
                "res_model": "delivery.expedition.line",
                "view_mode": "form",
                "res_id": self.logistics_expedition_line_id.id,
                "target": "current",
            }

        return {
            "type": "ir.actions.act_window",
            "name": _("Expedition Lines"),
            "res_model": "delivery.expedition.line",
            "view_mode": "tree,form",
            "domain": domain,
            "target": "current",
        }

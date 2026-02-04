# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ProjectTask(models.Model):
    _inherit = "project.task"

    logistics_picking_id = fields.Many2one(
        "stock.picking",
        string="Delivery Order",
        index=True,
        help="Delivery order (picking) this task is associated with.",
    )
    logistics_expedition_line_id = fields.Many2one(
        "delivery.expedition.line",
        string="Expedition Line",
        index=True,
        help="Expedition line this task is associated with.",
    )
    logistics_driver_id = fields.Many2one(
        "res.users",
        string="Driver (Logistics)",
        index=True,
        help="Canonical driver for uniqueness and synchronization.",
    )
    logistics_vehicle_id = fields.Many2one(
        "fleet.vehicle",
        string="Vehicle",
        help="Vehicle used by this driver for this delivery (from allocation/line/user defaults).",
    )
    logistics_is_primary = fields.Boolean(
        string="Primary Driver Task",
        default=False,
        help="Marks the task representing the expedition main driver for the delivery.",
    )

    logistics_invoice_refs = fields.Char(
        string="Invoice(s)",
        compute="_compute_logistics_invoice_refs",
        store=False,
        help="Customer invoice numbers linked to the sale order of this delivery.",
    )

    @api.depends(
        "logistics_picking_id",
        "logistics_picking_id.sale_id",
        "logistics_picking_id.sale_id.invoice_ids",
        "logistics_picking_id.sale_id.invoice_ids.name",
        "logistics_picking_id.sale_id.invoice_ids.state",
    )
    def _compute_logistics_invoice_refs(self):
        """Show invoice number(s) for the delivery.

        Spec requirement (t.12): driver task should contain references to both
        the Delivery Order (picking) and the Invoice.

        This is a non-stored compute so it remains up to date even if the invoice
        is created/posted after the task was created.
        """
        for task in self:
            sale = (
                task.logistics_picking_id.sale_id
                if task.logistics_picking_id
                else False
            )
            if not sale or not sale.invoice_ids:
                task.logistics_invoice_refs = False
                continue

            invoices = sale.invoice_ids
            posted = invoices.filtered(lambda m: m.state == "posted")
            others = invoices - posted

            def _label(inv):
                # Draft invoices usually have name='/' so display_name is clearer.
                return inv.name if inv.name and inv.name != "/" else inv.display_name

            task.logistics_invoice_refs = ", ".join(
                _label(inv) for inv in (posted + others)
            )

    def _get_assigned_driver_from_task(self):
        """
        Odoo versions can use either user_id (m2o) or user_ids (m2m) for assignees.
        We support both, but our created tasks will always have exactly 1 assignee.
        """
        self.ensure_one()
        if "user_id" in self._fields and self.user_id:
            return self.user_id
        if "user_ids" in self._fields and self.user_ids:
            # If multiple, take first; in our process we keep it 1.
            return self.user_ids[:1]
        return self.env["res.users"]

    def _set_assignee_driver(self, user):
        """
        Set the task assignee in a version-safe way.
        """
        self.ensure_one()
        if "user_id" in self._fields:
            self.user_id = user.id
        elif "user_ids" in self._fields:
            self.user_ids = [(6, 0, [user.id])]

    @api.constrains("logistics_picking_id", "logistics_driver_id")
    def _check_logistics_driver_required_if_linked(self):
        """
        If a task is linked to a picking as a logistics task, it must have a driver.
        """
        for task in self:
            if task.logistics_picking_id and not task.logistics_driver_id:
                raise ValidationError(
                    _("Logistics task linked to a delivery order must have a driver.")
                )

    # Uniqueness (one task per picking+driver) is enforced by delivery_logistics_expedition:
    # it always searches for an existing task and updates it with new data (company, delivery
    # time, etc.) instead of creating a duplicate. No constraint here so confirmation flow
    # never raises "A driver task for this delivery already exists."

    def write(self, vals):
        """
        On reassignment from Field Service / Tasks:
        - If this is a logistics task and the assignee changes, sync back to expedition line + SO/picking.
        """
        if self.env.context.get("delivery_logistics_skip_sync"):
            return super().write(vals)

        # Only run driver sync when assignee was actually changed (user_id/user_ids).
        # Skip when e.g. _portal_ensure_token only writes access_token to avoid recursion.
        assignee_keys = {"user_id", "user_ids"}
        if not assignee_keys.intersection(vals.keys()):
            return super().write(vals)

        # Determine old driver before write (for change detection)
        old_drivers = {t.id: t.logistics_driver_id for t in self}

        res = super().write(vals)

        # Post-write synchronization
        for task in self:
            if not task.logistics_expedition_line_id or not task.logistics_picking_id:
                continue

            new_driver = task._get_assigned_driver_from_task()
            old_driver = old_drivers.get(task.id)

            # If assignment changed but logistics_driver_id wasn't explicitly set, align it.
            if new_driver and (
                not task.logistics_driver_id or task.logistics_driver_id != new_driver
            ):
                task.sudo().with_context(delivery_logistics_skip_sync=True).write(
                    {
                        "logistics_driver_id": new_driver.id,
                    }
                )

            # Trigger back-sync only when driver actually changed.
            if old_driver and new_driver and old_driver != new_driver:
                # Critical logic: reassignment indicates transfer of execution responsibility.
                task.logistics_expedition_line_id._on_task_reassigned(
                    task=task,
                    old_driver=old_driver,
                    new_driver=new_driver,
                )

        return res

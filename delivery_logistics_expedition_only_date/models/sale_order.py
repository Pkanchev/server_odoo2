# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .delivery_constants import DELIVERY_MODE_APPLIED_SELECTION, WEEKDAY_SELECTION


class SaleOrder(models.Model):
    _name = "sale.order"
    _inherit = ["sale.order", "delivery.logistics.mixin"]

    # --- Transparency & control ---
    delivery_mode_applied = fields.Selection(
        DELIVERY_MODE_APPLIED_SELECTION,
        string="Applied Delivery Mode",
        compute="_compute_delivery_mode_applied",
        store=True,
        readonly=True,
        help="Shows which delivery mode is effectively applied to this sale order (address overrides customer).",
    )

    lock_delivery_logistics = fields.Boolean(
        string="Lock Logistics Data",
        help=(
            "When enabled, selecting a different delivery address/customer will NOT overwrite "
            "delivery date and logistics fields. This prevents accidental overwrites after dispatcher edits."
        ),
        default=False,
    )

    # --- Rules copied into SO (dispatcher can adjust) ---
    delivery_lead_days = fields.Integer(string="Delivery Lead Days", default=False)
    delivery_weekday = fields.Selection(
        WEEKDAY_SELECTION, string="Delivery Weekday", default=False
    )
    delivery_weeks_ahead = fields.Integer(string="Weeks Ahead", default=False)

    def _is_delivery_logistics_dispatcher(self):
        """
        Central permission check. Dispatcher can bypass locks after expedition is loaded/dispatched.
        """
        return self.env.user.has_group(
            "delivery_logistics_expedition.group_delivery_logistics_dispatcher"
        )

    @api.depends(
        "partner_id",
        "partner_shipping_id",
        "partner_id.delivery_mode",
        "partner_shipping_id.delivery_mode",
        "partner_id.parent_id.delivery_mode",
        "partner_shipping_id.parent_id.delivery_mode",
    )
    def _compute_delivery_mode_applied(self):
        """
        Compute applied mode with priority:
        partner_shipping_id (object) overrides partner_id (company).
        'inherit' is resolved via parent_id chain.
        """
        for order in self:
            shipping = order.partner_shipping_id
            if shipping:
                mode = shipping._get_effective_delivery_mode()
            else:
                mode = (
                    order.partner_id._get_effective_delivery_mode()
                    if order.partner_id
                    else "disabled"
                )
            # We store only the 3 operational modes, not 'inherit'.
            order.delivery_mode_applied = (
                mode if mode in ("disabled", "date_only", "full") else "disabled"
            )

    # ---------------------------
    # Onchange: populate from customer/object
    # ---------------------------
    @api.onchange("partner_id", "partner_shipping_id")
    def _onchange_delivery_logistics_from_partner(self):
        """
        On selecting customer and/or delivery address:
        - Populate logistics rule fields and info fields into SO
        - Compute delivery_date according to rules
        - Do NOT overwrite if lock is enabled
        """
        for order in self:
            if order.lock_delivery_logistics:
                # Dispatcher explicitly locked the data; do not overwrite.
                continue

            if order.delivery_mode_applied == "disabled":
                # Mode disabled => no new logic applied. Keep fields as-is.
                continue

            customer = order.partner_id
            delivery_addr = order.partner_shipping_id

            if not customer and not delivery_addr:
                continue

            # Build merged template (object overrides customer).
            # If there is no delivery address, customer acts as both.
            source_partner = delivery_addr or customer
            fallback_partner = customer if delivery_addr else None

            template = source_partner._get_delivery_template_values(
                fallback_partner=fallback_partner
            )

            # Copy rule fields
            order.delivery_lead_days = template.get("delivery_lead_days") or False
            order.delivery_weekday = template.get("delivery_weekday") or False
            order.delivery_weeks_ahead = template.get("delivery_weeks_ahead") or False

            # Copy operational logistics fields (driver, region, window, contact...)
            # Note: these field names match the mixin, so we can assign directly.
            for fname in [
                "delivery_driver_id",
                "delivery_region",
                "delivery_window_from",
                "delivery_window_to",
                "delivery_unload_time",
                "delivery_contact_name",
                "delivery_contact_phone",
                "delivery_instructions",
                "delivery_priority",
            ]:
                order[fname] = template.get(fname) or False

            # Compute delivery date as per requirements
            order._recompute_delivery_date_from_rules()

    @api.onchange(
        "delivery_lead_days", "delivery_weekday", "delivery_weeks_ahead", "date_order"
    )
    def _onchange_delivery_rules_recompute_date(self):
        """
        If dispatcher changes rules on the SO, recompute delivery_date accordingly.
        """
        for order in self:
            if order.lock_delivery_logistics:
                continue
            if order.delivery_mode_applied in ("date_only", "full"):
                order._recompute_delivery_date_from_rules()

    @api.onchange("order_line")
    def _onchange_order_line_prevent_leadtime_autofill(self):
        """
        Critical requirement:
        If there are NO delivery rules on customer/object, delivery date must remain empty
        and we must avoid Odoo's standard lead-time-driven suggestion.

        We enforce this by clearing commitment_date when:
        - applied mode is date_only/full
        - no rules are present
        - and delivery_date is not explicitly set
        """
        for order in self:
            if order.delivery_mode_applied not in ("date_only", "full"):
                continue
            if order.delivery_date:
                # User/dispatcher explicitly set a date; do not clear.
                continue
            if order.delivery_lead_days or order.delivery_weekday:
                # Rules exist; do not clear.
                continue
            # Clear standard field if it exists on this database.
            if "commitment_date" in order._fields:
                order.commitment_date = False

    @api.onchange("delivery_date")
    def _onchange_sync_commitment_date(self):
        """
        Keep standard Odoo 'commitment_date' consistent (if present), so other flows
        (like scheduled dates) can leverage the same planned date.

        We set commitment_date to midnight of delivery_date in user's timezone.
        """
        for order in self:
            if "commitment_date" not in order._fields:
                continue
            if not order.delivery_date:
                order.commitment_date = False
                continue

            # Set commitment datetime at 00:00:00 (local)
            dt = fields.Datetime.to_datetime(order.delivery_date)
            order.commitment_date = dt

    # ---------------------------
    # Core date computation logic
    # ---------------------------
    def _recompute_delivery_date_from_rules(self):
        """
        Compute delivery_date according to rules:
        - If lead days is set => date_order + lead days (priority)
        - Else if weekday is set => next occurrence of weekday (+ weeks ahead)
        - Else => delivery_date MUST be empty (False)
        """
        for order in self:
            if order.delivery_mode_applied not in ("date_only", "full"):
                # If new logic is not active, do not touch the date.
                continue

            base_dt = order.date_order or fields.Datetime.now()
            base_date = fields.Date.to_date(base_dt)

            if order.delivery_lead_days:
                order.delivery_date = base_date + timedelta(
                    days=int(order.delivery_lead_days)
                )
                continue

            if order.delivery_weekday:
                target_weekday = int(order.delivery_weekday)
                days_ahead = (target_weekday - base_date.weekday()) % 7
                candidate = base_date + timedelta(days=days_ahead)

                if order.delivery_weeks_ahead:
                    candidate = candidate + timedelta(
                        weeks=int(order.delivery_weeks_ahead)
                    )

                order.delivery_date = candidate
                continue

            # No rules at all => keep delivery_date empty (critical requirement).
            order.delivery_date = False

    # ---------------------------
    # Safe propagation / synchronization
    # ---------------------------
    def _delivery_logistics_relevant_fields(self):
        """
        Fields which, when changed on SO, must propagate to picking/invoice (if safe).
        """
        return set(
            self._delivery_logistics_field_names()
            + [
                "delivery_lead_days",
                "delivery_weekday",
                "delivery_weeks_ahead",
            ]
        )

    def _sync_to_outgoing_pickings(self):
        """
        Copy logistics fields from SO to outgoing pickings, but only if picking is not done.
        """
        for order in self:
            vals = order._prepare_delivery_logistics_vals()
            # Also copy the rule fields for transparency on picking if you want later; we do not add them to picking.
            for picking in order.picking_ids.filtered(
                lambda p: p.picking_type_code == "outgoing"
            ):
                if picking.state in ("done", "cancel"):
                    continue
                picking.with_context(delivery_logistics_skip_sync=True).write(vals)

    def _sync_to_draft_invoices(self):
        """
        Copy logistics fields from SO to invoices, but only when invoice is draft (not posted).
        """
        for order in self:
            vals = order._prepare_delivery_logistics_vals()
            for inv in order.invoice_ids:
                if inv.state != "draft":
                    continue
                inv.with_context(delivery_logistics_skip_sync=True).write(vals)

    def _ensure_not_locked_by_expedition(self, vals):
        """
        If SO is linked to an expedition which is already loaded/dispatched, block unsafe edits
        unless user is dispatcher.

        This avoids mismatches between SO / Expedition / Tasks after stabilization.
        """
        self.ensure_one()
        if self._is_delivery_logistics_dispatcher():
            return

        # Find any expedition line linked to this order's outgoing pickings.
        expedition_lines = (
            self.env["delivery.expedition.line"]
            .sudo()
            .search(
                [
                    (
                        "picking_id",
                        "in",
                        self.picking_ids.filtered(
                            lambda p: p.picking_type_code == "outgoing"
                        ).ids,
                    ),
                ]
            )
        )
        if not expedition_lines:
            return

        # If any linked expedition is locked, block edits of critical fields.
        locked = expedition_lines.mapped("expedition_id").filtered(
            lambda e: e.is_locked
        )
        if not locked:
            return

        critical = self._delivery_logistics_relevant_fields()
        if critical.intersection(vals.keys()):
            raise UserError(
                _(
                    "You cannot modify delivery logistics on this Sales Order because the related expedition "
                    "is already Loaded/Dispatched. Please contact a dispatcher."
                )
            )

    def write(self, vals):
        """
        Extend write:
        - Enforce stabilization rule (block edits if expedition is locked)
        - After write, propagate changes to pickings/invoices when safe
        - Avoid recursion with context flag
        """
        if self.env.context.get("delivery_logistics_skip_sync"):
            return super().write(vals)

        for order in self:
            order._ensure_not_locked_by_expedition(vals)

        res = super().write(vals)

        # Propagate only when new logic is active
        relevant = self._delivery_logistics_relevant_fields()
        if relevant.intersection(vals.keys()):
            for order in self.filtered(
                lambda o: o.delivery_mode_applied in ("date_only", "full")
            ):
                order._sync_to_outgoing_pickings()
                order._sync_to_draft_invoices()

        return res

    def _prepare_invoice(self):
        """Inject logistics fields into newly created invoices.

        Functional spec (t.6): when an invoice is created from a Sales Order,
        the same informational delivery fields must be copied to account.move.
        """
        vals = super()._prepare_invoice()
        if self.delivery_mode_applied in ("date_only", "full"):
            vals.update(self._prepare_delivery_logistics_vals())
        return vals

    def action_confirm(self):
        """
        On confirm:
        - standard Odoo creates pickings
        - we then copy logistics to created pickings and optionally create expedition + tasks
        """
        res = super().action_confirm()

        for order in self:
            if order.delivery_mode_applied not in ("date_only", "full"):
                continue

            # Ensure pickings receive the latest info immediately.
            order._sync_to_outgoing_pickings()

            if order.delivery_mode_applied == "full":
                order._ensure_expedition_and_tasks_for_outgoing_pickings()

        return res

    def _ensure_expedition_and_tasks_for_outgoing_pickings(self):
        """
        Create/append expedition document for driver+date and create expedition lines for outgoing pickings.

        Requirement: expedition is created automatically on SO confirmation only if driver and delivery_date are set.
        """
        self.ensure_one()

        if not self.delivery_driver_id or not self.delivery_date:
            return

        outgoing_pickings = self.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state != "cancel"
        )
        if not outgoing_pickings:
            return

        # Only create/find an expedition if we actually have pickings that still need an expedition line.
        pickings_to_add = outgoing_pickings.filtered(
            lambda p: not self.env["delivery.expedition.line"]
            .sudo()
            .search_count([("picking_id", "=", p.id)])
        )

        if not pickings_to_add:
            return

        Expedition = self.env["delivery.expedition"].sudo()
        expedition = Expedition.search(
            [
                ("company_id", "=", self.company_id.id),
                ("date", "=", self.delivery_date),
                ("driver_id", "=", self.delivery_driver_id.id),
            ],
            limit=1,
        )

        if not expedition:
            expedition = Expedition.create(
                {
                    "date": self.delivery_date,
                    "driver_id": self.delivery_driver_id.id,
                    "company_id": self.company_id.id,
                }
            )

        for picking in pickings_to_add:
            # Avoid duplicates: one picking => one expedition line.
            if (
                self.env["delivery.expedition.line"]
                .sudo()
                .search_count([("picking_id", "=", picking.id)])
            ):
                continue

            line = (
                self.env["delivery.expedition.line"]
                .sudo()
                .create(
                    {
                        "expedition_id": expedition.id,
                        "picking_id": picking.id,
                        # Default participants: the expedition driver
                        "participant_driver_ids": [(6, 0, [expedition.driver_id.id])],
                        # Default vehicle at line-level (can be overridden later)
                        "vehicle_id": (
                            expedition.default_vehicle_id.id
                            if expedition.default_vehicle_id
                            else False
                        ),
                    }
                )
            )

            # Link back for navigation / traceability
            picking.sudo().with_context(delivery_logistics_skip_sync=True).write(
                {
                    "expedition_line_id": line.id,
                }
            )

            # Create tasks (per participant) if required
            line._ensure_driver_tasks()

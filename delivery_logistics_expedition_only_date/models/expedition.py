# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from datetime import datetime, timedelta

from .delivery_constants import EXPEDITION_STATE_SELECTION, EXPEDITION_LOCKED_STATES


class DeliveryExpedition(models.Model):
    _name = "delivery.expedition"
    _description = "Delivery Expedition"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date desc, name desc"

    name = fields.Char(
        string="Expedition", required=True, copy=False, default="New", tracking=True
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )

    date = fields.Date(string="Delivery Date", required=True, tracking=True)
    driver_id = fields.Many2one(
        "res.users", string="Main Driver", required=True, tracking=True
    )

    state = fields.Selection(
        EXPEDITION_STATE_SELECTION, string="Status", default="planned", tracking=True
    )

    default_vehicle_id = fields.Many2one(
        "fleet.vehicle",
        string="Default Vehicle",
        help="Proposed from main driver's default vehicle; can be overridden.",
    )

    note = fields.Text(string="Notes")

    line_ids = fields.One2many(
        "delivery.expedition.line", "expedition_id", string="Deliveries"
    )

    delivery_count = fields.Integer(
        string="Delivery Count", compute="_compute_totals", store=True
    )
    total_boxes = fields.Float(
        string="Total Boxes", compute="_compute_totals", store=True
    )
    total_weight = fields.Float(
        string="Total Weight (kg)", compute="_compute_totals", store=True
    )

    is_locked = fields.Boolean(
        string="Locked",
        compute="_compute_is_locked",
        store=True,
        help="Technical field: True when expedition is in loaded/dispatched/delivered/done.",
    )

    # --- Issue / Hold details ---
    issue_kind = fields.Selection(
        [
            ("hold", "Задържано"),
            ("problem", "Проблем"),
        ],
        string="Issue Type",
        tracking=True,
        default=False,
        help="Sub-type when expedition is put on Hold / Problem.",
    )

    issue_note = fields.Text(
        string="Issue Note",
        tracking=True,
        help="Last hold/problem reason.",
    )

    issue_last_user_id = fields.Many2one(
        "res.users",
        string="Issue By",
        readonly=True,
        tracking=True,
    )

    issue_last_date = fields.Datetime(
        string="Issue Date",
        readonly=True,
        tracking=True,
    )

    previous_state = fields.Selection(
        EXPEDITION_STATE_SELECTION,
        string="Previous State",
        readonly=True,
        help="Technical: previous state before going into Hold / Problem.",
    )

    show_extra_columns = fields.Boolean(
        string="Show Extra Columns",
        default=False,
        help="UI helper: toggles optional columns in the Deliveries list.",
    )

    @api.constrains("company_id", "date", "driver_id")
    def _check_uniq_driver_date_company(self):
        """Ensure at most one expedition per company, date and driver."""
        for rec in self:
            if not rec.date or not rec.driver_id:
                continue
            other = self.search(
                [
                    ("company_id", "=", rec.company_id.id),
                    ("date", "=", rec.date),
                    ("driver_id", "=", rec.driver_id.id),
                    ("id", "!=", rec.id),
                ],
                limit=1,
            )
            if other:
                raise ValidationError(
                    _("An expedition for this driver and date already exists.")
                )

    def _format_dt_user_tz(self, dt):
        """
        Format datetime in the current user's timezone for chatter logging.
        """
        self.ensure_one()
        if not dt:
            return ""
        # context_timestamp converts server UTC to user's timezone
        local_dt = fields.Datetime.context_timestamp(self, dt)
        return local_dt.strftime("%d.%m.%Y %H:%M:%S")

    @api.depends("state")
    def _compute_is_locked(self):
        for rec in self:
            rec.is_locked = rec.state in EXPEDITION_LOCKED_STATES

    @api.depends("line_ids", "line_ids.total_boxes", "line_ids.total_weight")
    def _compute_totals(self):
        for expedition in self:
            expedition.delivery_count = len(expedition.line_ids)
            expedition.total_boxes = sum(expedition.line_ids.mapped("total_boxes"))
            expedition.total_weight = sum(expedition.line_ids.mapped("total_weight"))

    @api.model_create_multi
    def create(self, vals_list):
        """
        Assign sequence number and propose default vehicle from driver.
        """
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("delivery.expedition") or "New"
                )

        expeditions = super().create(vals_list)

        for exp in expeditions:
            # Default vehicle from driver's profile (if any)
            if (
                not exp.default_vehicle_id
                and exp.driver_id
                and getattr(exp.driver_id, "default_vehicle_id", False)
            ):
                exp.default_vehicle_id = exp.driver_id.default_vehicle_id.id

        return expeditions

    def _is_dispatcher(self):
        """
        Dispatcher group can override lock restrictions (as required).
        """
        return self.env.user.has_group(
            "delivery_logistics_expedition.group_delivery_logistics_dispatcher"
        )

    def write(self, vals):
        """
        Prevent edits of critical fields after expedition is loaded/dispatched unless dispatcher.
        """
        # IMPORTANT: when the expedition main driver changes we must know the *old* driver.
        # The sync runs after super().write(vals), so reading self.driver_id after the write
        # gives only the new driver and we lose the ability to replace old->new in participants.
        old_driver_by_exp = {}
        if "driver_id" in vals:
            for exp in self:
                old_driver_by_exp[exp.id] = exp.driver_id

        if any(exp.is_locked for exp in self) and not self._is_dispatcher():
            forbidden = {"driver_id", "default_vehicle_id"}
            if forbidden.intersection(vals.keys()):
                raise UserError(
                    _(
                        "This expedition is already Loaded/Dispatched. "
                        "Changing driver/vehicle is restricted to dispatchers."
                    )
                )
        res = super().write(vals)

        # If driver changed (dispatcher case), we must sync down to lines/tasks/sale/picking/invoice.
        if "driver_id" in vals:
            for exp in self:
                old_driver = old_driver_by_exp.get(exp.id)
                exp._sync_driver_change_to_related_documents(old_driver=old_driver)

        # If default vehicle changed, update lines/tasks where vehicle was falling back.
        if "default_vehicle_id" in vals:
            for exp in self:
                exp.line_ids._update_tasks_vehicle()

        return res

    def _sync_driver_change_to_related_documents(self, old_driver=None):
        """
        When expedition main driver changes, keep consistency:
        - Replace participant driver (old->new) on all lines
        - Sync driver back to SO, picking and tasks (invoice only if draft)
        """
        self.ensure_one()

        # Compute old driver by reading tracking? Not available here. So we rely on current values only.
        # In practice, this method is called after write; we treat expedition.driver_id as the new driver.
        new_driver = self.driver_id
        old_driver = old_driver or self.env["res.users"]

        # Nothing to do / no previous value available.
        if not old_driver or old_driver == new_driver:
            return

        for line in self.line_ids:
            # Replace the "primary driver" participant with the new driver
            line._replace_primary_driver(old_driver=old_driver, new_driver=new_driver)

        # Propose default vehicle if missing
        if not self.default_vehicle_id and getattr(
            new_driver, "default_vehicle_id", False
        ):
            self.default_vehicle_id = new_driver.default_vehicle_id.id

    # ---------------------------
    # Status transitions (buttons)
    # ---------------------------
    def action_set_state_preparing(self):
        for exp in self:
            exp._set_state("preparing")

    def action_set_state_ready(self):
        for exp in self:
            exp._set_state("ready")

    def action_set_state_loaded(self):
        for exp in self:
            exp._validate_before_loaded()
            exp._set_state("loaded")

    def action_set_state_dispatched(self):
        for exp in self:
            exp._set_state("dispatched")

    def action_set_state_delivered(self):
        for exp in self:
            exp._set_state("delivered")

    def action_set_state_done(self):
        for exp in self:
            exp._set_state("done")

    def _log_state_change(self, old_state, new_state, when_dt=None):
        """
        Post an explicit chatter note with who/when for the state change.
        This complements the default tracking entry.
        """
        self.ensure_one()
        when_dt = when_dt or fields.Datetime.now()
        when_str = self._format_dt_user_tz(when_dt)

        self.message_post(
            body=_(
                "Статус сменен: <b>%s</b> → <b>%s</b><br/>"
                "Променен от: <b>%s</b><br/>"
                "Кога: <b>%s</b>"
            )
            % (old_state, new_state, self.env.user.name, when_str),
            body_is_html=True,
            subtype_xmlid="mail.mt_note",
        )

    def _validate_before_loaded(self):
        """
        Requirement: expedition cannot become 'loaded' if there is a line with >1 participant
        and allocations are not filled for ALL participants.

        We interpret "filled" as: an allocation row exists for each participant AND
        (boxes > 0 or weight > 0) for each participant.
        """
        self.ensure_one()
        for line in self.line_ids:
            if len(line.participant_driver_ids) <= 1:
                continue
            # Must have allocation for each participant
            alloc_by_driver = {a.driver_id.id: a for a in line.allocation_ids}
            missing = line.participant_driver_ids.filtered(
                lambda u: u.id not in alloc_by_driver
            )
            if missing:
                raise UserError(
                    _(
                        "Cannot set expedition to Loaded.\n"
                        "Line for delivery %(picking)s has multiple drivers but missing allocations for: %(drivers)s"
                    )
                    % {
                        "picking": line.picking_id.display_name,
                        "drivers": ", ".join(missing.mapped("name")),
                    }
                )
            # Must be filled (not all zero)
            not_filled = []
            for driver in line.participant_driver_ids:
                alloc = alloc_by_driver[driver.id]
                if (alloc.boxes or 0.0) <= 0.0 and (alloc.weight_kg or 0.0) <= 0.0:
                    not_filled.append(driver.name)
            if not_filled:
                raise UserError(
                    _(
                        "Cannot set expedition to Loaded.\n"
                        "Line for delivery %(picking)s has allocations with zero boxes and zero weight for: %(drivers)s"
                    )
                    % {
                        "picking": line.picking_id.display_name,
                        "drivers": ", ".join(not_filled),
                    }
                )

    def _state_flow(self):
        """
        Defines the forward flow for the expedition lifecycle.
        Used for step-back navigation.
        """
        return [
            "planned",
            "preparing",
            "ready",
            "loaded",
            "dispatched",
            "delivered",
            "done",
        ]

    def _post_state_change_note(self, old_state, new_state):
        """
        Posts an explicit chatter note for state transitions (in addition to field tracking).
        This gives a clear log: who clicked what.
        """
        self.ensure_one()
        self.message_post(
            body=_("Status changed from <b>%s</b> to <b>%s</b> by <b>%s</b>.")
            % (old_state, new_state, self.env.user.name),
            body_is_html=True,
            subtype_xmlid="mail.mt_comment",
        )

    def _set_state(self, new_state):
        """
        Centralized state setter to keep all transitions consistent and logged.
        """
        self.ensure_one()
        old_state = self.state
        if old_state == new_state:
            return
        self.write({"state": new_state})
        self._log_state_change(old_state, new_state)

    def _resequence_lines(self):
        """Force route sequence to be 1..N (step 1) for each expedition."""
        Line = self.env["delivery.expedition.line"].sudo()

        for exp in self:
            lines = Line.search([("expedition_id", "=", exp.id)], order="sequence, id")
            seq = 1
            for l in lines:
                if l.sequence != seq:
                    l.with_context(skip_resequence=True).write({"sequence": seq})
                seq += 1

    def action_step_back(self):
        """
        Step back one state in the flow.
        - If current is 'hold', return to previous_state if available.
        - If current is first state ('planned'), do nothing.
        """
        for exp in self:
            if exp.state == "planned":
                continue

            if exp.state == "hold":
                target = exp.previous_state or "planned"
                exp._set_state(target)
                return

            flow = exp._state_flow()
            if exp.state not in flow:
                # Safety fallback
                exp._set_state("planned")
                continue

            idx = flow.index(exp.state)
            if idx <= 0:
                continue
            exp._set_state(flow[idx - 1])

    def action_reset_to_planned(self):
        """
        Reset expedition back to Planned (start over).
        This is useful after resolving a hold/problem.
        Logs the reset in chatter.
        """
        for exp in self:
            old = exp.state
            exp.write(
                {
                    "state": "planned",
                    "previous_state": False,
                    "issue_kind": False,
                    "issue_note": False,
                    "issue_last_user_id": False,
                    "issue_last_date": False,
                }
            )
            exp.message_post(
                body=_(
                    "Expedition reset from <b>%s</b> to <b>planned</b> by <b>%s</b>."
                )
                % (old, self.env.user.name),
                body_is_html=True,
                subtype_xmlid="mail.mt_comment",
            )

    def action_open_issue_wizard(self):
        """
        Open wizard to choose between Hold / Problem and provide a reason.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Problem / Hold"),
            "res_model": "delivery.expedition.issue.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_expedition_id": self.id,
            },
        }

    def action_toggle_extra_columns(self):
        self.ensure_one()
        self.show_extra_columns = not self.show_extra_columns
        return True


class DeliveryExpeditionLine(models.Model):
    _name = "delivery.expedition.line"
    _description = "Delivery Expedition Line"
    _order = "sequence, id"

    expedition_id = fields.Many2one(
        "delivery.expedition", required=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="expedition_id.company_id", store=True, readonly=True
    )

    sequence = fields.Integer(string="Route Sequence", default=1)

    picking_id = fields.Many2one(
        "stock.picking",
        string="Delivery Order",
        required=True,
        ondelete="restrict",
        domain=[("picking_type_code", "=", "outgoing")],
    )

    # Display fields (related) to satisfy the "expedition sheet" data needs
    customer_id = fields.Many2one(
        related="picking_id.sale_id.partner_id", store=True, readonly=True
    )
    delivery_partner_id = fields.Many2one(
        related="picking_id.partner_id", store=True, readonly=True
    )

    delivery_address = fields.Char(
        string="Address", compute="_compute_delivery_address", store=False
    )

    delivery_date = fields.Date(related="picking_id.delivery_date", readonly=True)
    delivery_region = fields.Char(related="picking_id.delivery_region", readonly=True)
    delivery_window_from = fields.Float(
        related="picking_id.delivery_window_from", readonly=True
    )
    delivery_window_to = fields.Float(
        related="picking_id.delivery_window_to", readonly=True
    )
    delivery_contact_name = fields.Char(
        related="picking_id.delivery_contact_name", readonly=True
    )
    delivery_contact_phone = fields.Char(
        related="picking_id.delivery_contact_phone", readonly=True
    )
    delivery_instructions = fields.Text(
        related="picking_id.delivery_instructions", readonly=True
    )
    delivery_priority = fields.Selection(
        related="picking_id.delivery_priority", readonly=True
    )

    salesperson_user_id = fields.Many2one(
        "res.users",
        string="Търговец",
        compute="_compute_salesperson_user",
        store=False,
        readonly=True,
    )

    invoice_refs = fields.Char(
        string="Номер фактура",
        compute="_compute_invoice_refs",
        store=False,
        readonly=True,
    )

    # Per line vehicle override (used as fallback when allocation doesn't specify vehicle)
    vehicle_id = fields.Many2one(
        "fleet.vehicle", string="Vehicle (Line)", default=False
    )

    # Participants (drivers)
    participant_driver_ids = fields.Many2many(
        "res.users",
        string="Drivers",
        help="Drivers participating in this delivery (picking stays single; participants can be many).",
    )

    allocation_ids = fields.One2many(
        "delivery.expedition.allocation",
        "line_id",
        string="Allocations by Driver",
    )

    total_boxes = fields.Float(
        string="Boxes (Total)", compute="_compute_totals", store=True
    )
    total_weight = fields.Float(
        string="Weight (Total kg)", compute="_compute_totals", store=True
    )

    @api.model_create_multi
    def create(self, vals_list):
        """
        Create expedition lines with:
        - automatic sequence assignment (start 1, step 1) if not provided
        - automatic resequencing to 1..N
        - allocations/tasks sync
        """
        # 1) Assign missing sequence per expedition (last+1)
        next_seq = {}

        for vals in vals_list:
            if vals.get("sequence") not in (None, False):
                continue

            expedition_id = vals.get("expedition_id") or self.env.context.get(
                "default_expedition_id"
            )
            if not expedition_id:
                continue

            if expedition_id not in next_seq:
                last_line = self.search(
                    [("expedition_id", "=", expedition_id)],
                    order="sequence desc, id desc",
                    limit=1,
                )
                next_seq[expedition_id] = (last_line.sequence if last_line else 0) + 1

            vals["sequence"] = next_seq[expedition_id]
            next_seq[expedition_id] += 1

        # 2) Create
        lines = super().create(vals_list)

        # 3) Normalize to 1..N for affected expeditions
        expeditions = lines.mapped("expedition_id")
        expeditions._resequence_lines()

        # 4) Keep allocations/tasks consistent
        lines._sync_allocations_with_participants()
        lines._ensure_driver_tasks()

        return lines

    @api.depends("delivery_partner_id")
    def _compute_delivery_address(self):
        for line in self:
            line.delivery_address = (
                line.delivery_partner_id.contact_address
                if line.delivery_partner_id
                else ""
            )

    @api.depends("allocation_ids.boxes", "allocation_ids.weight_kg")
    def _compute_totals(self):
        for line in self:
            line.total_boxes = sum(line.allocation_ids.mapped("boxes"))
            line.total_weight = sum(line.allocation_ids.mapped("weight_kg"))

    def _is_dispatcher(self):
        return self.env.user.has_group(
            "delivery_logistics_expedition.group_delivery_logistics_dispatcher"
        )

    def _check_locked_edit(self, vals):
        """
        Block edits after expedition is locked unless dispatcher.
        This implements stabilization after 'loaded'/'dispatched'.
        """
        self.ensure_one()
        if not self.expedition_id.is_locked:
            return
        if self._is_dispatcher():
            return

        restricted = {"participant_driver_ids", "allocation_ids", "vehicle_id"}
        if restricted.intersection(vals.keys()):
            raise UserError(
                _(
                    "This expedition is already Loaded/Dispatched. "
                    "Editing drivers/allocations/vehicle is restricted to dispatchers."
                )
            )

    def _split_extra_drivers_to_separate_expeditions(self):
        """
        Requirement #5:
        If user selects more than 1 driver on a line, we DO NOT keep multi-driver line.
        Instead:
          - Keep 1 driver on this line
          - For every additional driver:
              * create/get expedition for same company+date+driver
              * create a new expedition line for the same picking, with that single driver
        """
        self.ensure_one()

        if len(self.participant_driver_ids) <= 1:
            return self

        # Decide which driver stays on the current line:
        # Prefer the expedition main driver if present, else first selected.
        main_driver = self.expedition_id.driver_id
        if main_driver and main_driver in self.participant_driver_ids:
            keep_driver = main_driver
        else:
            keep_driver = self.participant_driver_ids[:1]

        extra_drivers = self.participant_driver_ids - keep_driver

        # Ensure current line keeps only keep_driver
        self.with_context(skip_split_drivers=True).write(
            {"participant_driver_ids": [(6, 0, keep_driver.ids)]}
        )

        DeliveryExpedition = self.env["delivery.expedition"].sudo()
        NewLine = self.env["delivery.expedition.line"].sudo()

        created_lines = self.env["delivery.expedition.line"]

        for drv in extra_drivers:
            # 1) Get or create expedition for this driver/date/company
            expedition = DeliveryExpedition.search(
                [
                    ("company_id", "=", self.expedition_id.company_id.id),
                    ("date", "=", self.expedition_id.date),
                    ("driver_id", "=", drv.id),
                ],
                limit=1,
            )
            if not expedition:
                expedition = DeliveryExpedition.create(
                    {
                        "company_id": self.expedition_id.company_id.id,
                        "date": self.expedition_id.date,
                        "driver_id": drv.id,
                        # default_vehicle_id will be proposed by expedition.create() if driver has one
                    }
                )

            # 2) Avoid duplicate line for same picking in that expedition
            existing = NewLine.search(
                [
                    ("expedition_id", "=", expedition.id),
                    ("picking_id", "=", self.picking_id.id),
                ],
                limit=1,
            )
            if existing:
                # Ensure it has the correct single driver
                if (
                    drv not in existing.participant_driver_ids
                    or len(existing.participant_driver_ids) != 1
                ):
                    existing.with_context(skip_split_drivers=True).write(
                        {"participant_driver_ids": [(6, 0, [drv.id])]}
                    )
                created_lines |= existing
                continue

            # 3) Create new line (single driver)
            nl = NewLine.create(
                {
                    "expedition_id": expedition.id,
                    "picking_id": self.picking_id.id,
                    "participant_driver_ids": [(6, 0, [drv.id])],
                    "vehicle_id": self.vehicle_id.id or False,
                }
            )
            created_lines |= nl

        # Resequence both expeditions (if method exists)
        exps = self.expedition_id | created_lines.mapped("expedition_id")
        if hasattr(exps, "_resequence_lines"):
            exps._resequence_lines()

        return created_lines

    def write(self, vals):
        # Lock rules
        for rec in self:
            rec._check_locked_edit(vals)

        # Remember expeditions BEFORE write (for expedition_id moves)
        exps_before = self.mapped("expedition_id")

        res = super().write(vals)

        # Split drivers if user selected multiple (Requirement #5)
        if "participant_driver_ids" in vals and not self.env.context.get(
            "skip_split_drivers"
        ):
            for line in self:
                if len(line.participant_driver_ids) > 1:
                    line._split_extra_drivers_to_separate_expeditions()

        # If participants changed, keep allocations and tasks consistent
        if "participant_driver_ids" in vals:
            self._sync_allocations_with_participants()
            self._ensure_driver_tasks()

        # Vehicle change affects task vehicle display
        if "vehicle_id" in vals:
            self._update_tasks_vehicle()

        # Resequence when needed (avoid recursion)
        if not self.env.context.get("skip_resequence"):
            if "sequence" in vals or "expedition_id" in vals:
                exps_after = self.mapped("expedition_id")
                (exps_before | exps_after)._resequence_lines()

        return res

    def unlink(self):
        exps = self.mapped("expedition_id")
        res = super().unlink()
        if exps and not self.env.context.get("skip_resequence"):
            exps._resequence_lines()
        return res

    def _sync_allocations_with_participants(self):
        """
        Keep allocation table aligned to participant_driver_ids:
        - Create missing allocation rows for newly added participants
        - Remove allocations for removed participants
        - Prevent duplicates
        """
        Allocation = self.env["delivery.expedition.allocation"].sudo()

        for line in self:
            participants = line.participant_driver_ids

            # Remove allocations for users no longer participating
            to_remove = line.allocation_ids.filtered(
                lambda a: a.driver_id not in participants
            )
            if to_remove:
                to_remove.unlink()

            # Create missing allocation lines
            existing_driver_ids = set(line.allocation_ids.mapped("driver_id").ids)
            for driver in participants:
                if driver.id in existing_driver_ids:
                    continue

                # Vehicle fallback chain: allocation.vehicle -> line.vehicle -> expedition.default -> user.default
                vehicle_id = (
                    line.vehicle_id.id
                    or line.expedition_id.default_vehicle_id.id
                    or getattr(driver, "default_vehicle_id", False)
                    and driver.default_vehicle_id.id
                    or False
                )

                Allocation.create(
                    {
                        "line_id": line.id,
                        "driver_id": driver.id,
                        "vehicle_id": vehicle_id,
                        "boxes": 0.0,
                        "weight_kg": 0.0,
                    }
                )

    def _get_or_create_fsm_project(self):
        """
        Find an existing suitable project to host driver tasks.
        If none exists, create one.

        We keep it robust across databases:
        - If project.project has an 'is_fsm' field, we prefer is_fsm=True projects.
        """
        Project = self.env["project.project"].sudo()

        domain = [("company_id", "in", [False, self.company_id.id])]
        if "is_fsm" in Project._fields:
            domain.append(("is_fsm", "=", True))

        project = Project.search(domain, limit=1)
        if project:
            return project

        create_vals = {
            "name": _("Deliveries"),
            "company_id": self.company_id.id,
        }
        if "is_fsm" in Project._fields:
            create_vals["is_fsm"] = True

        return Project.create(create_vals)

    def _ensure_driver_tasks(self):
        """
        Create (or update) one task per participating driver for this picking.

        Requirement:
        - For one picking and one driver there must not be two tasks.
        - Each driver should see their own task in "My Tasks".
        """
        Task = self.env["project.task"].sudo()

        for line in self:
            # Only in full logistics mode (as per requirements).
            # When called from SO confirmation, we are already in mode 'full'.
            # For safety, we *only* skip when there is an SO explicitly in
            # another mode; if picking has no sale_id we still allow tasks.
            sale = line.picking_id.sale_id
            if sale and getattr(sale, "delivery_mode_applied", "disabled") != "full":
                continue

            project = line._get_or_create_fsm_project()

            # Existing tasks for this picking+line
            # existing = Task.search(
            #     [
            #         ("logistics_picking_id", "=", line.picking_id.id),
            #         ("logistics_expedition_line_id", "=", line.id),
            #     ]
            # )

            existing = Task.with_context(active_test=False).search(
                [
                    ("logistics_picking_id", "=", line.picking_id.id),
                    ("logistics_driver_id", "!=", False),
                ]
            )

            existing_by_driver = {
                t.logistics_driver_id.id: t for t in existing if t.logistics_driver_id
            }

            planned_start, planned_end = line._compute_planned_datetimes()

            for driver in line.participant_driver_ids:
                task = existing_by_driver.get(driver.id)

                # Determine vehicle for this driver
                alloc = line.allocation_ids.filtered(lambda a: a.driver_id == driver)[
                    :1
                ]
                vehicle_id = (
                    (alloc.vehicle_id.id if alloc and alloc.vehicle_id else False)
                    or line.vehicle_id.id
                    or line.expedition_id.default_vehicle_id.id
                    or getattr(driver, "default_vehicle_id", False)
                    and driver.default_vehicle_id.id
                    or line._get_driver_vehicle_id(driver)
                    or False
                )

                task_vals = {
                    "name": _("Delivery %(picking)s - %(partner)s")
                    % {
                        "picking": line.picking_id.name,
                        "partner": (line.delivery_partner_id.display_name or ""),
                    },
                    "project_id": project.id,
                    "partner_id": (
                        line.delivery_partner_id.id
                        if line.delivery_partner_id
                        else False
                    ),
                    "company_id": line.company_id.id,
                    "active": True,
                    "logistics_picking_id": line.picking_id.id,
                    "logistics_expedition_line_id": line.id,
                    "logistics_driver_id": driver.id,
                    "logistics_vehicle_id": vehicle_id,
                    "logistics_is_primary": (driver == line.expedition_id.driver_id),
                    "description": line._build_task_description(driver=driver),
                }

                # Planned date range for Field Service
                if planned_start and "planned_date_begin" in Task._fields:
                    task_vals["planned_date_begin"] = planned_start
                if planned_end and "planned_date_end" in Task._fields:
                    task_vals["planned_date_end"] = planned_end
                # Some Odoo versions expose the end of the range as date_deadline
                if planned_end and "date_deadline" in Task._fields:
                    task_vals["date_deadline"] = planned_end
                if "planned_delivery_date" in Task._fields:
                    task_vals["planned_delivery_date"] = line.expedition_id.date

                phone = line.delivery_contact_phone
                if phone:
                    if "contact_number" in Task._fields:
                        task_vals["contact_number"] = phone
                    elif "partner_phone" in Task._fields:
                        task_vals["partner_phone"] = phone
                    elif "phone" in Task._fields:
                        task_vals["phone"] = phone

                # Before create: re-check for existing task (same picking+driver) so we
                # always update instead of duplicate when e.g. same delivery is added
                # to existing expedition or method is called multiple times.
                if not task:
                    task = Task.with_context(active_test=False).search(
                        [
                            ("logistics_picking_id", "=", line.picking_id.id),
                            ("logistics_driver_id", "=", driver.id),
                        ],
                        limit=1,
                    )
                if task:
                    # Update existing task (same delivery / same company → same task)
                    task.with_context(delivery_logistics_skip_sync=True).write(
                        task_vals
                    )
                    task._set_assignee_driver(driver)
                else:
                    # Create new only when no task exists for this picking+driver
                    new_task = Task.create(task_vals)
                    new_task._set_assignee_driver(driver)

            # Archive tasks for drivers no longer in participants
            remaining_driver_ids = set(line.participant_driver_ids.ids)
            for task in existing:
                if (
                    task.logistics_driver_id
                    and task.logistics_driver_id.id not in remaining_driver_ids
                ):
                    # Keep record but remove from active worklist
                    task.write({"active": False})

    def _build_task_description(self, driver):
        """
        Build human-readable description with the key operational data that drivers need.
        """
        self.ensure_one()

        def fmt_time(v):
            if v is False or v is None:
                return "-"
            # float_time, keep simple HH:MM
            hours = int(v)
            minutes = int(round((v - hours) * 60))
            return f"{hours:02d}:{minutes:02d}"

        addr = (
            self.delivery_partner_id.contact_address if self.delivery_partner_id else ""
        )
        window = f"{fmt_time(self.delivery_window_from)} - {fmt_time(self.delivery_window_to)}"
        contact = f"{self.delivery_contact_name or ''} {self.delivery_contact_phone or ''}".strip()

        return _(
            "Delivery Order: %(picking)s\n"
            "Customer: %(customer)s\n"
            "Delivery Address: %(addr)s\n"
            "Time Window: %(window)s\n"
            "Contact: %(contact)s\n"
            "Region: %(region)s\n"
            "Priority: %(priority)s\n"
            "\n"
            "Instructions:\n"
            "%(instr)s\n"
        ) % {
            "picking": self.picking_id.name,
            "customer": self.customer_id.display_name if self.customer_id else "",
            "addr": addr,
            "window": window,
            "contact": contact,
            "region": self.delivery_region or "",
            "priority": self.delivery_priority or "",
            "instr": self.delivery_instructions or "",
        }

    def _update_tasks_vehicle(self):
        """
        Update vehicles on tasks when vehicle fallback chain changes.
        """
        Task = self.env["project.task"].sudo()
        for line in self:
            tasks = Task.search(
                [
                    ("logistics_expedition_line_id", "=", line.id),
                    ("logistics_picking_id", "=", line.picking_id.id),
                    ("active", "in", [True, False]),
                ]
            )
            for task in tasks:
                driver = task.logistics_driver_id
                if not driver:
                    continue
                alloc = line.allocation_ids.filtered(lambda a: a.driver_id == driver)[
                    :1
                ]
                vehicle_id = (
                    (alloc.vehicle_id.id if alloc and alloc.vehicle_id else False)
                    or line.vehicle_id.id
                    or line.expedition_id.default_vehicle_id.id
                    or getattr(driver, "default_vehicle_id", False)
                    and driver.default_vehicle_id.id
                    or False
                )
                task.write({"logistics_vehicle_id": vehicle_id})

    def _replace_primary_driver(self, old_driver, new_driver):
        """
        Replace expedition main driver participant on this line with new_driver.
        Also sync driver on SO/picking/invoice when applicable.
        """
        self.ensure_one()
        if not old_driver or old_driver == new_driver:
            return

        # If old expedition driver is in participants, replace it
        if old_driver in self.participant_driver_ids:
            new_participants = (self.participant_driver_ids - old_driver) | new_driver
            self.participant_driver_ids = [(6, 0, new_participants.ids)]
        else:
            # Ensure new driver is included
            if new_driver not in self.participant_driver_ids:
                self.participant_driver_ids = [(4, new_driver.id)]

        # Also update allocations (old -> new)
        old_alloc = self.allocation_ids.filtered(lambda a: a.driver_id == old_driver)[
            :1
        ]
        if old_alloc:
            # If there is already allocation for new, merge values, then remove old
            new_alloc = self.allocation_ids.filtered(
                lambda a: a.driver_id == new_driver
            )[:1]
            if new_alloc:
                new_alloc.write(
                    {
                        "boxes": (new_alloc.boxes or 0.0) + (old_alloc.boxes or 0.0),
                        "weight_kg": (new_alloc.weight_kg or 0.0)
                        + (old_alloc.weight_kg or 0.0),
                    }
                )
                old_alloc.unlink()
            else:
                old_alloc.write({"driver_id": new_driver.id})

        # Sync picking + SO driver (invoice only if draft)
        self._sync_driver_to_documents(new_driver=new_driver)

        # Update tasks
        self._ensure_driver_tasks()

    def _sync_driver_to_documents(self, new_driver):
        """
        Maintain one 'responsible driver' in SO/picking/invoice:
        - picking.delivery_driver_id
        - sale.order.delivery_driver_id
        - draft invoices.delivery_driver_id
        """
        self.ensure_one()

        picking = self.picking_id
        sale = picking.sale_id
        invoices = sale.invoice_ids if sale else self.env["account.move"]

        if picking.state in ("done", "cancel"):
            raise UserError(_("Cannot update driver on a completed/cancelled picking."))

        picking.with_context(delivery_logistics_skip_sync=True).write(
            {
                "delivery_driver_id": new_driver.id,
            }
        )
        if sale:
            sale.with_context(delivery_logistics_skip_sync=True).write(
                {
                    "delivery_driver_id": new_driver.id,
                }
            )
        for inv in invoices.filtered(lambda m: m.state == "draft"):
            inv.with_context(delivery_logistics_skip_sync=True).write(
                {
                    "delivery_driver_id": new_driver.id,
                }
            )

    def _on_task_reassigned(self, task, old_driver, new_driver):
        """
        Called when a linked driver task is reassigned in Field Service / Tasks.

        Requirement: reassignment is considered a transfer of execution and must sync back
        to expedition + sales + picking (and invoice if draft) with consistency.

        Logic:
        - If the task is primary (main driver task) => move this line to new driver's expedition for same date
          and sync responsible driver across documents.
        - Else => replace participant (old->new) on this line.
        """
        self.ensure_one()

        if self.expedition_id.is_locked and not self._is_dispatcher():
            raise UserError(
                _(
                    "Cannot reassign driver task because the expedition is already Loaded/Dispatched. "
                    "Please contact a dispatcher."
                )
            )

        if task.logistics_is_primary:
            self._transfer_line_to_driver_expedition(new_driver=new_driver)
            return

        # Non-primary task => participant swap on the same line
        if old_driver in self.participant_driver_ids:
            participants = (self.participant_driver_ids - old_driver) | new_driver
            self.participant_driver_ids = [(6, 0, participants.ids)]

        # Update allocation row similarly
        alloc_old = self.allocation_ids.filtered(lambda a: a.driver_id == old_driver)[
            :1
        ]
        if alloc_old:
            alloc_new = self.allocation_ids.filtered(
                lambda a: a.driver_id == new_driver
            )[:1]
            if alloc_new:
                alloc_new.write(
                    {
                        "boxes": (alloc_new.boxes or 0.0) + (alloc_old.boxes or 0.0),
                        "weight_kg": (alloc_new.weight_kg or 0.0)
                        + (alloc_old.weight_kg or 0.0),
                    }
                )
                alloc_old.unlink()
            else:
                alloc_old.write({"driver_id": new_driver.id})

        self._ensure_driver_tasks()

    def _transfer_line_to_driver_expedition(self, new_driver):
        """
        Transfer this delivery line to the expedition of new_driver on the same date.
        This is the most consistent way to reflect 'responsible driver' change for a single delivery,
        without changing other deliveries in the old expedition.
        """
        self.ensure_one()

        Expedition = self.env["delivery.expedition"].sudo()

        target = Expedition.search(
            [
                ("company_id", "=", self.company_id.id),
                ("date", "=", self.expedition_id.date),
                ("driver_id", "=", new_driver.id),
            ],
            limit=1,
        )

        if not target:
            target = Expedition.create(
                {
                    "date": self.expedition_id.date,
                    "driver_id": new_driver.id,
                    "company_id": self.company_id.id,
                }
            )

        # Move line
        self.write({"expedition_id": target.id})

        # Ensure participants include the new expedition driver
        if new_driver not in self.participant_driver_ids:
            self.participant_driver_ids = [(4, new_driver.id)]

        # Sync responsible driver to docs
        self._sync_driver_to_documents(new_driver=new_driver)

        # Ensure allocations/tasks updated
        self._sync_allocations_with_participants()
        self._ensure_driver_tasks()

    def _float_to_hm(self, v):
        if v is None or v is False:
            return (0, 0)
        h = int(v)
        m = int(round((v - h) * 60))
        if m == 60:
            h += 1
            m = 0
        return (h, m)

    def _compute_planned_datetimes(self):
        # Източник: "Logistics Delivery Date" = expedition.date (fallback: line.delivery_date)
        d = self.expedition_id.date or self.delivery_date
        if not d:
            return (False, False)

        fh, fm = self._float_to_hm(self.delivery_window_from)
        th, tm = self._float_to_hm(self.delivery_window_to)

        start_dt = datetime.combine(d, datetime.min.time()).replace(
            hour=fh, minute=fm, second=0, microsecond=0
        )
        end_dt = datetime.combine(d, datetime.min.time()).replace(
            hour=th, minute=tm, second=0, microsecond=0
        )

        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)

        return (start_dt, end_dt)

    def _compute_salesperson_user(self):
        for line in self:
            sale = line.picking_id.sale_id if line.picking_id else False
            line.salesperson_user_id = (
                sale.user_id.id if sale and sale.user_id else False
            )

    def _compute_invoice_refs(self):
        for line in self:
            sale = line.picking_id.sale_id if line.picking_id else False
            if not sale or not sale.invoice_ids:
                line.invoice_refs = False
                continue

            invoices = sale.invoice_ids
            posted = invoices.filtered(lambda m: m.state == "posted")
            others = invoices - posted

            def _label(inv):
                return inv.name if inv.name and inv.name != "/" else inv.display_name

            line.invoice_refs = ", ".join(_label(inv) for inv in (posted + others))

    def _get_driver_vehicle_id(self, driver):
        # 1) explicit default
        if getattr(driver, "default_vehicle_id", False):
            return driver.default_vehicle_id.id

        # 2) any vehicle assigned to driver's partner (standard fleet.vehicle.driver_id = res.partner)
        Vehicle = self.env["fleet.vehicle"].sudo()
        partner = driver.partner_id
        if partner and "driver_id" in Vehicle._fields:
            v = Vehicle.search([("driver_id", "=", partner.id)], limit=1)
            if v:
                return v.id

        return False


class DeliveryExpeditionAllocation(models.Model):
    _name = "delivery.expedition.allocation"
    _description = "Expedition Allocation by Driver"
    _order = "id"

    line_id = fields.Many2one(
        "delivery.expedition.line", required=True, ondelete="cascade"
    )
    expedition_id = fields.Many2one(
        related="line_id.expedition_id", store=True, readonly=True
    )

    driver_id = fields.Many2one("res.users", string="Driver", required=True)
    vehicle_id = fields.Many2one("fleet.vehicle", string="Vehicle", default=False)

    boxes = fields.Float(string="Boxes", default=0.0)
    weight_kg = fields.Float(string="Weight (kg)", default=0.0)

    _uniq_driver_date_company = models.Constraint(
        "UNIQUE(line_id, driver_id)",
        "An expedition for this driver and date already exists.",
    )

    @api.constrains("boxes", "weight_kg")
    def _check_non_negative(self):
        """
        Validators: no negative numbers for boxes/weight.
        """
        for rec in self:
            if rec.boxes < 0:
                raise ValidationError(_("Boxes cannot be negative."))
            if rec.weight_kg < 0:
                raise ValidationError(_("Weight cannot be negative."))

    @api.constrains("driver_id")
    def _check_driver_is_participant(self):
        """
        Allocation driver must be listed among participants.
        """
        for rec in self:
            if rec.driver_id not in rec.line_id.participant_driver_ids:
                raise ValidationError(
                    _("Allocation driver must be one of the participants on the line.")
                )

    def write(self, vals):
        """
        Restrict edits after expedition is locked unless dispatcher.
        """
        for rec in self:
            if rec.expedition_id.is_locked and not rec.line_id._is_dispatcher():
                raise UserError(
                    _(
                        "This expedition is already Loaded/Dispatched. "
                        "Editing allocations is restricted to dispatchers."
                    )
                )
        res = super().write(vals)

        # Vehicle allocation changes should reflect in tasks
        if "vehicle_id" in vals:
            self.mapped("line_id")._update_tasks_vehicle()

        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.mapped("line_id")._update_tasks_vehicle()
        return records

    def unlink(self):
        for rec in self:
            if rec.expedition_id.is_locked and not rec.line_id._is_dispatcher():
                raise UserError(
                    _(
                        "This expedition is already Loaded/Dispatched. "
                        "Deleting allocations is restricted to dispatchers."
                    )
                )
        return super().unlink()

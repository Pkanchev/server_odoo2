# -*- coding: utf-8 -*-
from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    default_vehicle_id = fields.Many2one(
        "fleet.vehicle",
        string="Default Vehicle",
        help="Default vehicle used for expeditions/tasks when none is specified at line/allocation level.",
    )

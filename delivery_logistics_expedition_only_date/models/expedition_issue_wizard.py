# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class DeliveryExpeditionIssueWizard(models.TransientModel):
    """
    Wizard for marking an expedition as Hold/Problem with a mandatory note.
    It logs the action in chatter and stores last issue metadata on the expedition.
    """
    _name = "delivery.expedition.issue.wizard"
    _description = "Expedition Issue Wizard"

    expedition_id = fields.Many2one(
        "delivery.expedition",
        string="Expedition",
        required=True,
        readonly=True,
    )

    issue_kind = fields.Selection(
        [
            ("hold", "Hold"),
            ("problem", "Problem"),
        ],
        string="Type",
        required=True,
        default="hold",
    )

    note = fields.Text(string="Reason / Description", required=True)

    def action_apply(self):
        """
        Apply the selected issue kind to expedition:
        - Remember previous state (for possible back/restore)
        - Move expedition to 'hold'
        - Store issue fields
        - Post a chatter message
        """
        self.ensure_one()
        expedition = self.expedition_id

        if not self.note or not self.note.strip():
            raise UserError(_("Please enter a reason/description."))

        # Remember where we came from, so we can step back if needed
        prev_state = expedition.state

        expedition.write({
            "previous_state": prev_state,
            "issue_kind": self.issue_kind,
            "issue_note": self.note,
            "issue_last_user_id": self.env.user.id,
            "issue_last_date": fields.Datetime.now(),
            # "state": "hold",
        })

        expedition._set_state("hold")
        # Log in chatter (explicit message + state tracking will also log)
        label = dict(self._fields["issue_kind"].selection).get(self.issue_kind)
        expedition.message_post(
            body=_(
                "<b>%s</b> by <b>%s</b><br/>Reason:<br/><pre>%s</pre>"
            ) % (label, self.env.user.name, self.note),
            body_is_html=True,
            subtype_xmlid="mail.mt_note",
        )

        return {"type": "ir.actions.act_window_close"}

# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pos_auto_invoice_enabled = fields.Boolean(
        related='pos_config_id.auto_invoice_enabled', readonly=False)
    pos_auto_invoice_percent = fields.Float(
        related='pos_config_id.auto_invoice_percent', readonly=False)
    pos_auto_invoice_journal_id = fields.Many2one(
        related='pos_config_id.auto_invoice_journal_id', readonly=False)
    pos_auto_invoice_secondary_journal_id = fields.Many2one(
        related='pos_config_id.auto_invoice_secondary_journal_id', readonly=False)
    pos_auto_invoice_partner_id = fields.Many2one(
        related='pos_config_id.auto_invoice_partner_id', readonly=False)
    pos_auto_invoice_frequency = fields.Integer(
        related='pos_config_id.auto_invoice_frequency', readonly=False)
    pos_auto_invoice_max_per_run = fields.Integer(
        related='pos_config_id.auto_invoice_max_per_run', readonly=False)

    def action_pos_auto_invoice_simulate(self):
        self.ensure_one()
        return self.pos_config_id.action_simulate_auto_invoice()

    def action_pos_auto_invoice_run_now(self):
        self.ensure_one()
        return self.pos_config_id.action_run_auto_invoice_now()

# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class PosConfig(models.Model):
    _inherit = 'pos.config'

    auto_invoice_enabled = fields.Boolean(
        string='Facturación automática',
        help='Factura automáticamente un porcentaje de las ventas del día '
             '(pedidos que quedaron como recibo) a Consumidor Final.')
    auto_invoice_percent = fields.Float(
        string='Porcentaje a facturar (%)', default=0.0,
        help='Porcentaje del total vendido en el día que debe quedar facturado. '
             'Se descuenta lo ya facturado manualmente desde el POS.')
    auto_invoice_journal_id = fields.Many2one(
        'account.journal', string='Diario AFIP (para el porcentaje)',
        domain=[('type', '=', 'sale')], check_company=True,
        help='Diario conectado a AFIP donde se factura el porcentaje configurado. '
             'Si está vacío, se usa el Diario de Facturas de este punto de venta.')
    auto_invoice_secondary_journal_id = fields.Many2one(
        'account.journal', string='Diario para el resto de las ventas',
        domain=[('type', '=', 'sale')], check_company=True,
        help='Diario SIN conexión a AFIP donde se factura el resto de las ventas '
             'del día (lo que excede el porcentaje). Si está vacío, el resto '
             'queda como recibo.')
    auto_invoice_partner_id = fields.Many2one(
        'res.partner', string='Cliente Consumidor Final',
        help='Cliente que se asigna a los pedidos sin cliente al facturarlos '
             'automáticamente. Los pedidos que ya tienen cliente lo conservan.')
    auto_invoice_frequency = fields.Integer(
        string='Frecuencia (veces al día)', default=2,
        help='Cantidad de ejecuciones diarias del proceso automático. '
             'Cada ejecución factura el porcentaje de lo vendido hasta el momento.')
    auto_invoice_max_per_run = fields.Integer(
        string='Máximo de facturas por ejecución', default=10,
        help='Tope de facturas por corrida. Con diarios AFIP cada validación '
             'puede tardar hasta 60 segundos; el tope evita corridas eternas.')

    _auto_invoice_percent_range = models.Constraint(
        'CHECK(auto_invoice_percent >= 0 AND auto_invoice_percent <= 100)',
        'El porcentaje a facturar debe estar entre 0 y 100.')
    _auto_invoice_frequency_range = models.Constraint(
        'CHECK(auto_invoice_frequency >= 1 AND auto_invoice_frequency <= 24)',
        'La frecuencia debe estar entre 1 y 24 veces al día.')
    _auto_invoice_max_per_run_range = models.Constraint(
        'CHECK(auto_invoice_max_per_run >= 1 AND auto_invoice_max_per_run <= 100)',
        'El máximo por ejecución debe estar entre 1 y 100.')

    @api.constrains('auto_invoice_secondary_journal_id')
    def _check_secondary_journal_not_afip(self):
        for config in self:
            journal = config.auto_invoice_secondary_journal_id
            if journal and journal._fields.get('l10n_ar_afip_ws') and journal.l10n_ar_afip_ws:
                raise ValidationError(_(
                    'El diario para el resto de las ventas no puede estar conectado '
                    'a AFIP (%s). Elegí un diario manual/electrónico sin web service.',
                    journal.name))

    def write(self, vals):
        res = super().write(vals)
        if 'auto_invoice_frequency' in vals:
            cron = self.env.ref(
                'pos_auto_invoice_cafetres.ir_cron_pos_auto_invoice',
                raise_if_not_found=False)
            if cron:
                frequency = max(1, min(24, int(vals.get('auto_invoice_frequency') or 1)))
                cron.sudo().write({
                    'interval_number': max(1, round(24 / frequency)),
                    'interval_type': 'hours',
                })
        return res

    def action_simulate_auto_invoice(self):
        self.ensure_one()
        log = self.env['pos.order']._auto_invoice_for_config(self, dry_run=True)
        return log.get_formview_action()

    def action_run_auto_invoice_now(self):
        self.ensure_one()
        log = self.env['pos.order']._auto_invoice_for_config(self, dry_run=False)
        return log.get_formview_action()
